"""Rule candidates: user lines that read as standing rules, listed for review (#584).

A rule said once in a session ("we should use the good dragon voices") is in
the palace verbatim, but nobody searches for it until it has already been
broken. At mining time the convo miner hands every newly filed chunk, and the
user's messages as :func:`mempalace.normalize.collect_user_messages` gathered
them, to :func:`flag_chunks`; each line of a user message that reads like a
standing rule is appended, verbatim and with the id of the drawer holding it,
to a per-day review list ``rule-candidates-YYYY-MM-DD.md``. A reviewer turns
each one into a memory plus a mechanism, or ticks it off. ``mempalace rules
--since YYYY-MM-DD`` prints the lists.

The check runs on the messages, not on the rendered chunk text, because the
transcript marks only the first line of a user message with ``>``: a rule on
its third line sits next to the assistant's reply with nothing to tell them
apart, and an assistant blockquote starts with ``>`` without being the
user's. A format with no role record (a plain ``>``-quoted text file) falls
back to the ``>`` lines of the chunks.

Nothing here touches the palace: the list is a local markdown file, and any
failure is logged and swallowed so it can never fail a mine.

Environment:

* ``MEMPALACE_RULE_CANDIDATES=0`` turns flagging off.
* ``MEMPALACE_RULE_CANDIDATES_DIR`` is where the lists go
  (default ``~/.mempalace/rule_candidates``).
* ``MEMPALACE_RULE_MEMORY_GLOB`` is where existing memories live
  (default ``~/.claude/projects/*/memory/*.md``); a candidate whose words
  cover an existing memory's ``name:`` slug is dropped as already a rule.
"""

from __future__ import annotations

import difflib
import glob
import hashlib
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Phrasings that read as a standing rule. Word-bounded so "every" does not
# match "everything" and "never" does not match "nevertheless".
RULE_PATTERN = re.compile(
    r"\b(?:always|never|every|we should use|standing rule|from now on|i thought we)\b",
    re.IGNORECASE,
)

FILE_PREFIX = "rule-candidates-"
SEEN_FILE = ".seen"
_SLUG_TYPE_WORDS = {"feedback", "project", "user", "reference", "memory"}
_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
_WORD_RE = re.compile(r"[a-z0-9]+")


def enabled() -> bool:
    value = os.environ.get("MEMPALACE_RULE_CANDIDATES", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def candidates_dir() -> Path:
    raw = os.environ.get("MEMPALACE_RULE_CANDIDATES_DIR") or "~/.mempalace/rule_candidates"
    return Path(raw).expanduser()


def _reads_as_rule(text: str) -> bool:
    """Whether one line of the user's reads as a standing rule.

    Harness-injected turns (``<task-notification>``, ``<system-reminder>``,
    ``[Request interrupted ...]``) are not the user's words and never match.
    """
    text = text.strip()
    if not text or text[0] in "<[":
        return False
    return bool(RULE_PATTERN.search(text))


def candidate_lines(content: str) -> list:
    """Return the ``>`` lines of ``content`` that read as a standing rule, verbatim.

    The fallback for a transcript with no role record. Only the first line
    of a user turn carries the ``>`` marker, so a rule on a later line of a
    multi-line message is missed here, and an assistant blockquote is taken
    for the user's. When the user's messages are known, :func:`rule_lines`
    checks every line of them instead.
    """
    return [
        line for line in content.splitlines() if line.startswith(">") and _reads_as_rule(line[1:])
    ]


def rule_lines(user_messages: Iterable[str]) -> list:
    """Return every line of the user's messages that reads as a standing rule.

    Each line is returned as it stands in the transcript: the first line of a
    message carries the ``> `` marker, the later lines are bare.
    """
    out = []
    for text in user_messages:
        for i, raw in enumerate(str(text).splitlines()):
            if _reads_as_rule(raw):
                out.append(f"> {raw}" if i == 0 else raw)
    return out


def _locate(line: str, items) -> Optional[tuple]:
    """The ``(room, drawer_id)`` of the first chunk in ``items`` that holds ``line``.

    A chunk holds the line when it is one of the chunk's lines, or (a line
    the chunker joined onto another) when its text appears in the chunk.
    ``None`` when no chunk holds it: the drawer is unchanged this pass, was
    planned out as sensitive, or the chunker cut the line in two.
    """
    text = line.strip()
    for chunk, room, drawer_id in items:
        content = chunk.get("content") or ""
        if line in content.splitlines() or text in content:
            return room, drawer_id
    return None


def _slug_words(slug: str) -> list:
    words = _WORD_RE.findall(slug.lower())
    return [w for w in words if len(w) >= 3 and w not in _SLUG_TYPE_WORDS]


def memory_slugs(pattern: Optional[str] = None) -> list:
    """The ``name:`` slug of every memory file matching ``pattern``."""
    pattern = (
        pattern
        or os.environ.get("MEMPALACE_RULE_MEMORY_GLOB")
        or "~/.claude/projects/*/memory/*.md"
    )
    slugs = []
    for path in glob.glob(os.path.expanduser(pattern)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(2048)
        except OSError:
            continue
        m = _NAME_RE.search(head)
        if m:
            slugs.append(m.group(1).strip("'\""))
    return slugs


def matching_slug(line: str, slugs: Iterable[str]) -> Optional[str]:
    """The first slug whose words (two or more) all appear, fuzzily, in ``line``."""
    words = set(_WORD_RE.findall(line.lower()))
    if not words:
        return None
    for slug in slugs:
        need = _slug_words(slug)
        if len(need) < 2:
            continue
        if all(w in words or difflib.get_close_matches(w, words, n=1, cutoff=0.8) for w in need):
            return slug
    return None


def _key(line: str) -> str:
    norm = " ".join(line.lstrip(">").split()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def flag_chunks(
    items,
    wing: str,
    source_file: str,
    today: Optional[date] = None,
    user_messages: Optional[Iterable[str]] = None,
) -> int:
    """Append rule candidates from ``items`` (``(chunk, room, drawer_id)``) to today's list.

    ``user_messages`` is what :func:`mempalace.normalize.collect_user_messages`
    gathered while the file was normalized. With it, every line of every user
    message is checked and an assistant blockquote is never taken for the
    user's; a candidate is listed only when one of ``items`` holds it, so an
    unchanged drawer, or a sensitive chunk planned out of the pass, is never
    listed. Empty or ``None`` (a format with no role record) falls back to
    the ``>`` lines of the chunks.

    Returns the number of entries written. Never raises.
    """
    try:
        if not enabled() or "/subagents/" in str(source_file).replace("\\", "/"):
            # A subagent's user turns are its orchestrator's prompt, not the user's.
            return 0
        found = []
        if user_messages:
            items = list(items)
            for line in rule_lines(user_messages):
                where = _locate(line, items)
                if where is not None:
                    found.append((line, where[0], where[1]))
        else:
            for chunk, room, drawer_id in items:
                for line in candidate_lines(chunk.get("content") or ""):
                    found.append((line, room, drawer_id))
        if not found:
            return 0
        return _write(found, wing, source_file, today or date.today())
    except Exception:
        logger.debug("rule-candidate flagging failed for %s", source_file, exc_info=True)
        return 0


def _write(found, wing, source_file, today) -> int:
    out_dir = candidates_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_path = out_dir / SEEN_FILE
    try:
        seen = set(seen_path.read_text(encoding="utf-8").split())
    except OSError:
        seen = set()
    slugs = None
    entries = []
    keys = []
    for line, room, drawer_id in found:
        key = _key(line)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
        if slugs is None:
            slugs = memory_slugs()
        if matching_slug(line, slugs):
            continue  # already a memory: remembered as seen, not listed
        entries.append(
            f"- [ ] `{drawer_id}` · {wing}/{room} · {Path(str(source_file)).name}\n  {line}\n"
        )
    if entries:
        day_path = out_dir / f"{FILE_PREFIX}{today.isoformat()}.md"
        new = not day_path.exists()
        with open(day_path, "a", encoding="utf-8") as fh:
            if new:
                fh.write(f"# Rule candidates {today.isoformat()}\n\n")
            fh.write("".join(entries))
    if keys:
        with open(seen_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(keys) + "\n")
    return len(entries)


def list_files(since: Optional[date] = None) -> list:
    """Per-day list files dated on or after ``since``, oldest first."""
    files = []
    for path in sorted(candidates_dir().glob(f"{FILE_PREFIX}*.md")):
        try:
            day = date.fromisoformat(path.stem[len(FILE_PREFIX) :])
        except ValueError:
            continue
        if since is None or day >= since:
            files.append(path)
    return files


def run_list(since: Optional[str]) -> int:
    """CLI: print every rule-candidate list dated on or after ``since`` (YYYY-MM-DD)."""
    try:
        since_day = date.fromisoformat(since) if since else None
    except ValueError:
        print(f"--since wants YYYY-MM-DD, got {since!r}", file=sys.stderr)
        return 2
    files = list_files(since_day)
    if not files:
        print(f"No rule candidates since {since} in {candidates_dir()}", file=sys.stderr)
        return 0
    for path in files:
        sys.stdout.write(path.read_text(encoding="utf-8"))
        sys.stdout.write("\n")
    return 0
