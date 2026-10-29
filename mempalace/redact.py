"""Secrets never reach a drawer: every write replaces them with a placeholder first.

A session transcript can print a credential: a shell expansion, an ``env``
dump, a config file read for debugging. Mined as is, the value lands in a
drawer that any agent can search, and in the private collection it would
still be a stored copy (#596). So every backend collection's ``add``,
``upsert`` and ``update`` pass their documents and the string values of their
metadata through :func:`redact` before the backend sees them; see
``BaseCollection.__init_subclass__``. A secret becomes
``[redacted <kind> sha256:<first 8 hex>]``. The short hash lets someone who
already holds a candidate check it against the drawer (a vault item, a leak
report); for a provider token it recovers nothing. Existing placeholders are
never matched again, so a write is redacted the same way however many layers
pass it through.

Patterns are regular expressions: :data:`DEFAULT_PATTERNS`, keyed by kind,
plus any listed under ``secret_patterns`` in the palace config (kind
``custom``). They match a credential's shape, never a value, so the config
never holds one. Every write checks the patterns, even one with nothing to
scan: a config that cannot be read, or a pattern that does not compile,
raises :class:`SecretPatternsUnavailable` and the write fails rather than
store text unscanned.

Known limits. Shapes catch tokens with a recognisable prefix; a bare
password or a provider without one passes through. Embeddings passed in by
the caller were computed from the text before redaction. Configured patterns
are trusted like the rest of the config: one with nested quantifiers can
backtrack badly on long text, so keep them to a prefix and a character class.
Metadata keys are not scanned, only values. A configured pattern is scanned
inside a named group, so it must not use numbered group references, must scope
any inline flag (``(?i:…)``, not ``(?i)``), and must not name a group
starting ``_mpr``; one that breaks these fails closed.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

# Kind -> pattern. Each matches a credential's shape only.
DEFAULT_PATTERNS: Dict[str, str] = {
    "anthropic": r"sk-ant-[A-Za-z0-9_-]{30,}",
    "openai": r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}",
    "github": r"(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}",
    "slack": r"xox[abposr]-[0-9]+-[A-Za-z0-9-]{20,}",
    "aws": r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    "google": r"AIza[0-9A-Za-z_-]{35}",
    # No word boundary: a raw JSONL line holds a literal "\n" escape right before the token.
    "notion": r"ntn_[A-Za-z0-9]{40,}|secret_[A-Za-z0-9]{43}(?![A-Za-z0-9])",
}


class SecretPatternsUnavailable(RuntimeError):
    """The configured secret patterns could not be read or compiled. Writing without them could
    store a credential they would have caught, so the write stops instead."""


# A placeholder this module wrote. Every pass matches it first and keeps it as it is, so a later
# pass never rewrites an earlier one's work, and redaction is idempotent even when a configured
# pattern would match its hex.
_PLACEHOLDER = r"\[redacted [a-z]+ sha256:[0-9a-f]{8}\]"

Pass = Tuple[re.Pattern, List[str]]

# "\1".."\99" not itself escaped, or a numbered conditional "(?(1)…)" in any spelling an older
# Python accepts ("(?(+1)", "(?( 1)", a non-ASCII digit; ``\d`` is Unicode here).
_NUMBERED_GROUP_REF = re.compile(r"(?<!\\)(?:\\\\)*\\[1-9]|\(\?\(\s*[+-]?\d")


def _pass(pairs: List[Tuple[str, str]]) -> Pass:
    body = "|".join(f"(?P<_mpr{i}>{pattern})" for i, (_kind, pattern) in enumerate(pairs))
    try:
        return re.compile(f"(?P<_mprkeep>{_PLACEHOLDER})|{body}"), [k for k, _p in pairs]
    except re.error as exc:
        # A pattern with a global inline flag ("(?i)…") compiles alone but not inside a group;
        # scope it instead ("(?i:…)"). Group names starting "_mpr" are reserved.
        raise SecretPatternsUnavailable(f"secret pattern does not combine: {exc}") from exc


def _compile(extra: Tuple[str, ...]) -> List[Pass]:
    """The passes, in order: every built-in shape in one, then one per configured pattern.
    Built-ins go first so a configured pattern that overlaps a provider token cannot take only
    part of it; one pass per configured pattern keeps their group names apart."""
    for pattern in list(DEFAULT_PATTERNS.values()) + list(extra):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SecretPatternsUnavailable(f"invalid secret pattern {pattern!r}: {exc}") from exc
        if _NUMBERED_GROUP_REF.search(pattern):
            # Wrapped in a group, "\2" would point at another group and quietly stop matching.
            raise SecretPatternsUnavailable(
                f"secret pattern {pattern!r} uses a numbered group reference; name the group"
            )
    return [_pass(list(DEFAULT_PATTERNS.items()))] + [_pass([("custom", p)]) for p in extra]


_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "patterns": None}


def _config_signature() -> Tuple[str, Optional[Tuple[int, ...]]]:
    from .config import _default_config_dir

    path = os.path.join(str(_default_config_dir()), "config.json")
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return path, None
    except OSError:
        # Present and unreachable: MempalaceConfig marks it "unread" and _load raises.
        return path, (-1,)
    # ctime moves on a chmod or a rename over the file, which mtime and size do not.
    return path, (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _load() -> List[Tuple[str, re.Pattern]]:
    from .config import MempalaceConfig

    config = MempalaceConfig()
    if getattr(config, "_file_config_state", "read") in ("unread", "unparsed"):
        raise SecretPatternsUnavailable("palace config unreadable; secret_patterns unknown")
    raw = config._file_config.get("secret_patterns", [])
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise SecretPatternsUnavailable("secret_patterns must be a list of non-empty strings")
    return _compile(tuple(raw))


def patterns() -> List[Pass]:
    """The compiled patterns, re-read only when the config file changes."""
    key = _config_signature()
    with _lock:
        if _cache["key"] != key:
            _cache["patterns"] = _load()
            _cache["key"] = key
        return _cache["patterns"]


def _placeholder(kind: str, value: str) -> str:
    return f"[redacted {kind} sha256:{hashlib.sha256(value.encode()).hexdigest()[:8]}]"


def redact(text: str) -> str:
    """``text`` with every secret-shaped run replaced by its placeholder."""
    passes = patterns()
    if not text:
        return text
    clean = text
    for combined, kinds in passes:

        def replace(m: re.Match, kinds=kinds) -> str:
            if m.group("_mprkeep") is not None:
                return m.group(0)
            kind = next(k for i, k in enumerate(kinds) if m.group(f"_mpr{i}") is not None)
            return _placeholder(kind, m.group(0))

        clean = combined.sub(replace, clean)
    return text if clean == text else clean


def redact_documents(documents):
    """Documents as a write passes them (a list, a single string, or None), redacted."""
    patterns()  # a write with nothing to scan still fails on a broken config
    if documents is None:
        return None
    if isinstance(documents, str):
        return redact(documents)
    return [redact(d) if isinstance(d, str) else d for d in documents]


def _redact_value(value):
    """``value`` with its strings redacted, at any depth; the same object when nothing changed."""
    if isinstance(value, str):
        clean = redact(value)
        return value if clean == value else clean
    if isinstance(value, dict):
        out = {k: _redact_value(v) for k, v in value.items()}
        return value if all(out[k] is value[k] for k in value) else out
    if isinstance(value, (list, tuple)):
        out = [_redact_value(v) for v in value]
        if all(a is b for a, b in zip(out, value)):
            return value
        # A changed tuple comes back as a plain one: a subclass (a namedtuple) may not rebuild
        # from a single iterable.
        return tuple(out) if isinstance(value, tuple) else out
    return value


def _redact_metadata(meta):
    return _redact_value(meta) if isinstance(meta, dict) else meta


def redact_metadatas(metadatas):
    """Metadata as a write passes it (a list of dicts, one dict, or None), its string values
    redacted. The caller's dicts are never changed in place."""
    patterns()
    if metadatas is None:
        return None
    if isinstance(metadatas, dict):
        return _redact_metadata(metadatas)
    return [_redact_metadata(m) for m in metadatas]
