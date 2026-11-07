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
report); for a provider token, a long random string, it recovers nothing. A
match of a configured pattern, which may be a guessable value, is
``[redacted custom]`` with no hash. Existing placeholders are
never matched again, so a write is redacted the same way however many layers
pass it through.

Patterns are regular expressions: :data:`DEFAULT_PATTERNS`, keyed by kind,
plus any listed under ``secret_patterns`` in the palace config (kind
``custom``). They match a credential's shape, never a value, so the config
never holds one. Every write checks the patterns, even one with nothing to
scan: a config that cannot be read, or a pattern that does not compile,
raises :class:`SecretPatternsUnavailable` and the write fails rather than
store text unscanned.

Known limits. Shapes catch tokens with a recognisable prefix. A bare
password or a provider token without one is caught only beside a secret-ish
name (``NEXTAUTH_SECRET=…``, ``"password": "…"``) or as a URL's password
(``postgres://user:…@``), by :func:`_redact_assignments`, which keeps the
name and writes ``[redacted assignment]``; one printed alone passes through.
A quoted value, or one after a tight ``=``, is replaced whatever it looks
like; a bare value after ``: `` or `` = `` (YAML, prose, code) only when it is
8+ characters of two or more kinds, so ``password: hunter2`` stays readable.
A quote that never closes leaves only its first word redacted. Embeddings passed in by
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
_PLACEHOLDER = r"\[redacted (?:[a-z]+ sha256:[0-9a-f]{8}|custom|assignment)\]"

Pass = Tuple[re.Pattern, List[str]]

# "\1".."\99" not itself escaped, or a numbered conditional "(?(1)…)" in any spelling an older
# Python accepts ("(?(+1)", "(?( 1)", a non-ASCII digit; ``\d`` is Unicode here).
# "(?i)", "(?ms)" and the like: a flag group closed with ")" rather than scoped with ":".
_GLOBAL_INLINE_FLAG = re.compile(r"(?<!\\)(?:\\\\)*\(\?[aiLmsux]+\)")

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
        if _GLOBAL_INLINE_FLAG.search(pattern):
            # Python 3.10 only warns about one inside a group; 3.11+ refuses it. Refuse it on all.
            raise SecretPatternsUnavailable(
                f"secret pattern {pattern!r} uses a global inline flag; scope it as (?i:...)"
            )
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
    if kind == "custom":
        # A configured pattern may catch a guessable value (a password); an unkeyed hash of it
        # would let any reader test guesses, so it gets none.
        return "[redacted custom]"
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
    clean = _redact_assignments(clean)
    return text if clean == text else clean


# --- Values named as secrets -------------------------------------------------------------------
# A bare password or a random key has no shape to match, but it is usually printed beside its
# name: an ``env`` dump, a ``.env`` file, a JS or JSON config, a connection URL (2026-11-07: a
# session printed an attacker's dump of a calcom env, a database URL password and NEXTAUTH_SECRET,
# and no shape above matched either). This pass keeps the name and replaces only the value with
# ``[redacted assignment]``, with no hash because a password may be guessable.

# The key's last word names a secret: PASSWORD, NEXTAUTH_SECRET, apiKey, client_secret, DB_PASS,
# GOOGLE_API_CREDENTIALS, CALENDSO_ENCRYPTION_KEY. The word starts the key, follows "_", "." or
# "-", or starts a camelCase hump (case-sensitive), and ends it; so "bypass", "max_tokens" and
# "SECRET_FILE" are not secret names.
_SECRET_NAME = re.compile(
    r"(?:^|(?<=[_.\-])|(?<=[a-z0-9])(?=[A-Z]))"
    r"(?i:pass(?:word|wd|phrase)?|pwd|secret|token|credentials?|"
    r"(?:api|access|private|secret|encryption|signing|auth|master|client)[_\-]?key)$"
)

# A key and its "=" or ":": KEY=, KEY: , "key": , key=', a JSON-escaped \"key\": in a raw JSONL
# line, --password=. Only the key is matched here, never its value, so a key that names no secret
# cannot swallow one that follows it ("user: password=...").
_KEY = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,80})(?:\\?[\"'])?(?P<sep>[ \t]*[=:][ \t]*)"
)
# Bare after a tight "=" (KEY=value: env files, CLI flags, kwargs): everything to whitespace, a
# quote or a backslash (a raw JSONL "\n" escape ends the line).
_BARE_TIGHT = re.compile(r"[^\s\"'\\]+")
# Bare after ": " or " = " (YAML, prose, code): stops at punctuation that ends an expression.
_BARE_LOOSE = re.compile(r"(?P<v>[^\s\"'\\,;)}\]\[<>(]+)(?P<after>[(\[]?)")
# YAML block scalar: "password: |" then lines indented deeper than the key.
_BLOCK = re.compile(r"[|>][-+]?[ \t]*(?=\n|$)")

# scheme://user:password@host: the password, whatever its name. The scheme is bounded and may not
# start mid-word, so a long dotted run is not rescanned from every dot. A user may hold "'".
_URL_USERINFO = re.compile(
    r"(?P<head>(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,30}://[^\s:/@\"\\]+:)(?P<pw>[^\s@/\"\\]+)(?=@)",
    re.I,
)

# A whole value that is a reference or a mask, not a secret: $VAR, ${VAR}, $(cmd), %(name)s,
# {{ x }}, <your key>, a placeholder this module or a person wrote ([redacted …], [REDACTED]), ***,
# xxxx, ..., and a few words. Every part is bounded, so checking a long value stays linear.
_NOT_A_VALUE = re.compile(
    r"(?:\$[A-Za-z_][A-Za-z0-9_]{0,80}|\$\{[^{}\n]{1,200}\}|\$\([^()\n]{1,200}\)"
    r"|%\([A-Za-z_][A-Za-z0-9_]{0,80}\)s|\{\{[^{}\n]{1,200}\}\}|<[^<>\n]{1,100}>"
    r"|\[(?i:redacted)[^\[\]\n]{0,60}\]|\*{1,64}|[xX]{3,64}|\.{3,8}|\u2026"
    r"|(?i:changeme|example|redacted|none|null|nil|true|false|undefined|placeholder|dummy"
    r"|secret|password|your[_\-][A-Za-z0-9_\-]{0,40}))"
)
# The same, at the start of a tight bare value, for a reference that holds spaces
# ("$(bw get password db)", "<your key>", "{{ vault_db }}", "[redacted github sha256:…]"). It is
# kept only when the value ends right after it; "${VAR}secret" is redacted, reference and all.
_LEADING_REFERENCE = re.compile(
    r"\$\{[^{}\n]{1,200}\}|\$\([^()\n]{1,200}\)|\{\{[^{}\n]{1,200}\}\}|<[^<>\n]{1,100}>"
    r"|\[(?i:redacted)[^\[\]\n]{0,60}\]"
)
# The same terminators as _BARE_TIGHT: "${VAR},secret" is one value, not a reference and a tail.
_VALUE_ENDS = re.compile(r"[\s\"'\\]|$")
_DOTTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")

ASSIGNMENT_PLACEHOLDER = "[redacted assignment]"


def _placeholder_value(value: str) -> bool:
    return _NOT_A_VALUE.fullmatch(value) is not None


def _mixed(value: str) -> bool:
    """For a bare value after ": " or " = ", where prose and code live: at least 8 characters of
    at least two kinds (lower, upper, digit, symbol), so "rotated", "compromised",
    "authenticationrequired" and "default_credentials_provider" stay readable."""
    kinds = (
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() and c not in "_.-" for c in value),
    )
    return len(value) >= 8 and sum(kinds) >= 2


def _plain_quote_end(text: str, i: int, quote: str):
    """Index of the quote closing a value that starts at ``i``, honouring backslash escapes and
    spanning lines; None when the text ends first."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == quote:
            return i
        else:
            i += 1
    return None


def _escaped_quote_end(text: str, i: int):
    """For a value inside a JSON string that is itself inside a JSON string (a raw JSONL line:
    {\"password\":\"…\"}), the index of the \" closing it. Each raw escape is decoded first, then
    the inner string's own escapes are honoured, so \\\" inside the value does not close it."""
    n, inner_escaped = len(text), False
    while i < n:
        c = text[i]
        if c == '"':
            return None  # the outer string ended: not the shape we thought
        if c != "\\":
            inner_escaped, i = False, i + 1
            continue
        if i + 1 >= n:
            return None
        u = text[i + 1]
        if inner_escaped:
            inner_escaped = False
        elif u == "\\":
            inner_escaped = True
        elif u == '"':
            return i
        i += 2
    return None


def _block_span(text: str, key_start: int, at: int):
    """The lines of a YAML block scalar after "key: |": those indented deeper than the key."""
    line_start = text.rfind("\n", 0, key_start) + 1
    indent = len(text[line_start:key_start]) - len(text[line_start:key_start].lstrip(" "))
    end = text.find("\n", at)
    if end == -1:
        return None
    first = pos = end + 1
    last = None
    while pos < len(text):
        nl = text.find("\n", pos)
        line = text[pos : nl if nl != -1 else len(text)]
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        if line.strip():
            last = pos + len(line)
        if nl == -1:
            break
        pos = nl + 1
    return (first, last) if last else None


def _quoted(text: str, at: int):
    """(start, end) of a quoted value at ``at``; None when no quote opens there; False when one
    opens and never closes. A failed search is not repeated for a later key: that key's own
    opening quote would have closed it, so at most one search per quote kind runs to the end."""
    if text.startswith('\\"', at):
        start = at + 2
        end = _escaped_quote_end(text, start)
    elif at < len(text) and text[at] in "\"'":
        start = at + 1
        end = _plain_quote_end(text, start, text[at])
    else:
        return None
    if end is None:
        return False
    return (start, end) if end > start else None


def _value_span(text: str, key: re.Match):
    """Where the secret value after a secret-named key sits, or None to leave it."""
    at = key.end()
    q = _quoted(text, at)
    if q:
        return None if _placeholder_value(text[q[0] : q[1]]) else q
    if q is False:
        at += 2 if text.startswith('\\"', at) else 1  # an unclosed quote: the rest is bare
    if key.group("sep") == "=" or q is False:
        lead = _LEADING_REFERENCE.match(text, at)
        if lead:
            if _VALUE_ENDS.match(text, lead.end()):
                return None
            rest = _BARE_TIGHT.match(text, lead.end())
            return (at, rest.end() if rest else lead.end())
        v = _BARE_TIGHT.match(text, at)
        if v is None or _placeholder_value(v.group(0)):
            return None
        return v.span()
    if ":" in key.group("sep") and _BLOCK.match(text, at):
        return _block_span(text, key.start(), at)
    v = _BARE_LOOSE.match(text, at)
    if v is None:
        return None
    value = v.group("v")
    # "password = getpass()", "token = os.environ[...]", "secret = self.secret": code, not a value.
    if v.group("after") or _DOTTED_IDENTIFIER.match(value) or _placeholder_value(value):
        return None
    return v.span("v") if _mixed(value) else None


def _url_password(m: re.Match) -> str:
    if _placeholder_value(m.group("pw")):
        return m.group(0)
    return m.group("head") + ASSIGNMENT_PLACEHOLDER


def _redact_assignments(text: str) -> str:
    if "=" not in text and ":" not in text:
        return text
    text = _URL_USERINFO.sub(_url_password, text)
    out, pos = [], 0
    for k in _KEY.finditer(text):
        if k.start() < pos or not _SECRET_NAME.search(k.group("name")):
            continue
        span = _value_span(text, k)
        if span is None:
            continue
        out.append(text[pos : span[0]])
        out.append(ASSIGNMENT_PLACEHOLDER)
        pos = span[1]
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


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
