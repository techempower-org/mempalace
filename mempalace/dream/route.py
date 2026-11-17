"""Route each dream proposal that names a project to that project's inbox, one line each.

A proposal names a project through its sources: a memory folder under the memory root whose
name is a project folder's path with every character but letters and digits turned into ``-``
(how the harness names them), or a path inside a project folder under a projects root. A
proposal whose sources name no project (the global CLAUDE.md) is left on the review page.

Each project gets ``scratch/dream-inbox.md``, appended to, one line per proposal::

    - [ ] <lane> · <proposal id> · <one-sentence ask> (dream <day>, <kind>)

``lane`` is the project's lane from an optional map, else the project folder's name. A folder
is written only when it is not a git repository or its ``scratch/`` is git-ignored, and the
inbox must be a regular file (a symlink is never followed). The routing is recorded in the
verdict ledger (``judged.json``) under :data:`KEY`: for each routed id, where it went and
whether it has been acted on, which it has once it is decided on the review page or is no
longer proposed.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

KEY = "_routing"
INBOX = "scratch/dream-inbox.md"
MAX_ASK = 240
HEADER = (
    "# Dream inbox\n\n"
    "One line per proposal from the nightly dream (`python -m mempalace.dream nightly --route`)\n"
    "that names this project. Act on it, then accept or reject it on the review page; the next\n"
    "night records it as acted. Lines are only ever appended.\n\n"
)
_LINE = re.compile(r":\d+$")
_ENC = re.compile(r"[^A-Za-z0-9]")
_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s|$)")
_MEMORY = re.compile(r"/([^/]+)/memory/")


def _encoded(path: Path) -> str:
    return _ENC.sub("-", str(path))


def _children(roots: Sequence[Path]) -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    for root in roots:
        try:
            for p in Path(root).iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    found.setdefault(_encoded(p.absolute()), p.absolute())
        except OSError:
            continue
    return found


def project_of(source: str, roots: Sequence[Path], _index: Optional[Dict[str, Path]] = None):
    """The project folder a source names, or None."""
    index = _index if _index is not None else _children(roots)
    src = _LINE.sub("", str(source))
    m = _MEMORY.search(src)
    if m and m.group(1) in index:
        return index[m.group(1)]
    p = Path(src)
    for root in roots:
        try:
            rel = p.relative_to(Path(root).absolute())
        except ValueError:
            continue
        if rel.parts:
            cand = Path(root).absolute() / rel.parts[0]
            if cand.is_dir():
                return cand
    return None


def ask(proposal: Mapping) -> str:
    """The proposal's first sentence, on one line, at most :data:`MAX_ASK` characters."""
    text = " ".join(str(proposal.get("summary", "")).split())
    m = _SENTENCE.match(text)
    text = m.group(1) if m else text
    if len(text) > MAX_ASK:
        text = text[: MAX_ASK - 1] + "…"
    return text.replace("·", "-")


def _writable(project: Path) -> Optional[str]:
    """None when the project's inbox may be written, else why not."""
    if not (project / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "-q", INBOX],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "git check-ignore failed"
    return None if r.returncode == 0 else "scratch/ is not git-ignored"


def _append(project: Path, lines: Iterable[str]) -> Optional[str]:
    """Append to the project's inbox (created 0600 with its header); None or why not."""
    inbox = project / INBOX
    if os.path.lexists(inbox) and (inbox.is_symlink() or not inbox.is_file()):
        return "inbox is not a regular file"
    scratch = inbox.parent
    if scratch.is_symlink():
        return "scratch/ is a symlink"
    scratch.mkdir(exist_ok=True)
    new = not os.path.lexists(inbox)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(inbox, flags, 0o600)
    except OSError as exc:
        return f"cannot open the inbox ({type(exc).__name__})"
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        if new:
            f.write(HEADER)
        f.write("".join(f"{ln}\n" for ln in lines))
    return None


def route(
    proposals: Sequence[Mapping],
    roots: Sequence[Path],
    ledger: Dict[str, dict],
    day: str,
    lanes: Optional[Mapping[str, str]] = None,
    decisions: Optional[Mapping[str, Mapping]] = None,
) -> dict:
    """Route tonight's proposals and update the record in ``ledger`` (in place). Returns the
    night's figures: routed and acted (all nights), new (tonight), open, unrouted, skipped."""
    lanes = lanes or {}
    decisions = decisions or {}
    record: Dict[str, dict] = ledger.setdefault(KEY, {})
    index = _children(roots)
    tonight = {str(p.get("id")) for p in proposals}
    per_project: Dict[Path, List[str]] = {}
    pending: Dict[Path, List[tuple]] = {}
    unrouted = 0
    for prop in proposals:
        pid = str(prop.get("id"))
        projects = []
        for s in prop.get("sources", []):
            p = project_of(s, roots, index)
            if p is not None and p not in projects:
                projects.append(p)
        if not projects:
            unrouted += 1
            continue
        done = {t["project"] for t in record.get(pid, {}).get("to", [])}
        for p in projects:
            if p.name in done:
                continue
            lane = lanes.get(p.name, p.name)
            line = f"- [ ] {lane} · {pid} · {ask(prop)} (dream {day}, {prop.get('kind')})"
            per_project.setdefault(p, []).append(line)
            pending.setdefault(p, []).append((pid, lane))
    skipped, new = [], set()
    for p, lines in sorted(per_project.items()):
        why = _writable(p) or _append(p, lines)
        if why:
            skipped.append(f"{p.name}: {why}")
            continue
        for pid, lane in pending[p]:
            entry = record.setdefault(pid, {"to": [], "acted": None})
            entry["to"].append({"project": p.name, "lane": lane, "day": day})
            new.add(pid)
    for pid, entry in record.items():
        if entry.get("acted"):
            continue
        d = decisions.get(pid)
        if isinstance(d, Mapping) and d.get("decision") in ("accept", "reject"):
            entry["acted"] = {"how": f"decided: {d['decision']}", "day": day}
        elif pid not in tonight:
            entry["acted"] = {"how": "no longer proposed", "day": day}
    acted = sum(1 for e in record.values() if e.get("acted"))
    return {
        "routed": len(record),
        "new": len(new),
        "acted": acted,
        "open": len(record) - acted,
        "unrouted": unrouted,
        "skipped": skipped,
    }


def summary(ledger: Mapping) -> str:
    """The lead's one line: ``dreams: N proposals routed, M acted``."""
    record = ledger.get(KEY) if isinstance(ledger, Mapping) else None
    record = record if isinstance(record, Mapping) else {}
    acted = sum(1 for e in record.values() if isinstance(e, Mapping) and e.get("acted"))
    return f"dreams: {len(record)} proposals routed, {acted} acted"
