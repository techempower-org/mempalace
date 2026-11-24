"""Rule candidates: user lines that read as standing rules, listed for review (#584).

A rule said once in a session ("we should use the good dragon voices") is in
the palace verbatim, but nobody searches for it until it has already been
broken. At mining time the convo miner hands every newly filed chunk to
:func:`flag_chunks`; each user line (the ``>`` turn of an exchange) that
reads like a standing rule is appended, verbatim and with its drawer id, to a
per-day review list ``rule-candidates-YYYY-MM-DD.md``. A reviewer turns each
one into a memory plus a mechanism, or ticks it off. ``mempalace rules
--since YYYY-MM-DD`` prints the lists.

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


def candidate_lines(content: str) -> list:
    """Return the user lines of ``content`` that read as a standing rule, verbatim.

    Only ``>`` lines are user turns in the normalized transcript format.
    Harness-injected turns (``<task-notification>``, ``<system-reminder>``,
    ``[Request interrupted ...]``) are not the user's words and are skipped.
    """
    out = []
    for line in content.splitlines():
        if not line.startswith(">"):
            continue
        text = line[1:].strip()
        if not text or text[0] in "<[":
            continue
        if RULE_PATTERN.search(text):
            out.append(line)
    return out


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


def flag_chunks(items, wing: str, source_file: str, today: Optional[date] = None) -> int:
    """Append rule candidates from ``items`` (``(chunk, room, drawer_id)``) to today's list.

    Returns the number of entries written. Never raises.
    """
    try:
        if not enabled() or "/subagents/" in str(source_file).replace("\\", "/"):
            # A subagent's ``>`` turns are its orchestrator's prompt, not the user's.
            return 0
        found = []
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
