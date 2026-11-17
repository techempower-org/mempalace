"""Route each dream proposal that names a project to that project's inbox, one line each.

A proposal names a project through its sources: a memory folder under the memory root whose
name is a project folder's path with every character but letters and digits turned into ``-``
(how the harness names them), or a path inside a project folder under a projects root. A
project folder that is a symlink counts as its real folder, and only a folder owned by this
user is ever a destination. A memory-folder name two different folders encode to is ambiguous
and names neither. A proposal whose sources name no project (the global CLAUDE.md) is left on
the review page.

Each project gets ``scratch/dream-inbox.md``, appended to, one line per proposal::

    - [ ] <lane> · <proposal id> · <one-sentence ask> (dream <day>, <kind>)

``lane`` is the project's lane from an optional map, else the project folder's name. A folder
is written only when it is outside every git work tree or git ignores the inbox path. The
write goes through descriptors opened without following links (the project, ``scratch/``,
the inbox), and the inbox must then be a regular file with one link, owned by this user; it
is kept at mode 0600. A proposal id already in the inbox is never appended again, so a run
that stops between the append and the ledger save repeats nothing.

The routing is recorded in the verdict ledger (``judged.json``) under :data:`KEY`: for each
routed id, its kind, where it went and whether it has been acted on, which it has once it is
decided on the review page, or once a night whose producer for its kind ran complete no
longer proposes it (a proposal that comes back is open again).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Collection, Dict, List, Mapping, Optional, Sequence

KEY = "_routing"
INBOX = "scratch/dream-inbox.md"
MAX_ASK = 240
GONE = "no longer proposed"
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
_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_DIRECTORY = getattr(os, "O_DIRECTORY", None)


def _encoded(path) -> str:
    return _ENC.sub("-", str(path))


def _real_dir(p: Path) -> Optional[Path]:
    """``p``'s real folder when it is a directory owned by this user, else None."""
    try:
        real = p.resolve(strict=True)
        st = real.stat()
    except (OSError, RuntimeError):
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return None
    return real


def _index(roots: Sequence[Path]) -> Dict[str, Optional[Path]]:
    """Encoded name -> real project folder, for every child of the roots under both its own
    path and its real path. A name two different real folders share maps to None."""
    found: Dict[str, Optional[Path]] = {}
    for root in roots:
        try:
            children = list(Path(root).absolute().iterdir())
        except OSError:
            continue
        for p in children:
            if p.name.startswith("."):
                continue
            real = _real_dir(p)
            if real is None:
                continue
            for name in {_encoded(p), _encoded(real)}:
                if name in found and found[name] != real:
                    found[name] = None
                else:
                    found[name] = real
    return found


def project_of(source: str, roots: Sequence[Path], _idx: Optional[Dict] = None) -> Optional[Path]:
    """The real project folder a source names, or None."""
    index = _idx if _idx is not None else _index(roots)
    src = _LINE.sub("", str(source))
    m = _MEMORY.search(src)
    if m and m.group(1) in index:
        return index[m.group(1)]
    p = Path(os.path.normpath(src))
    if not p.is_absolute():
        return None
    for root in roots:
        base = Path(os.path.normpath(Path(root).absolute()))
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] not in ("..", ".") and not rel.parts[0].startswith("."):
            return _real_dir(base / rel.parts[0])
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
    """None when git will never track the project's inbox, else why not."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None  # not in any work tree
        ignored = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "-q", INBOX],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "git could not be asked"
    return None if ignored.returncode == 0 else "the inbox path is not git-ignored"


class Unsafe(Exception):
    """The inbox's target is not one this writer will write through."""


def _open_inbox(project: Path) -> int:
    """A descriptor for appending to the project's inbox, every component opened without
    following a link; raises :class:`Unsafe` with the reason when the target is unsafe."""
    if _NOFOLLOW is None or _DIRECTORY is None:
        raise Unsafe("this platform cannot open without following links")
    dflags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
    pfd = os.open(str(project), dflags)
    try:
        try:
            os.mkdir("scratch", 0o700, dir_fd=pfd)
        except FileExistsError:
            pass
        sfd = os.open("scratch", dflags, dir_fd=pfd)
    finally:
        os.close(pfd)
    try:
        name = os.path.basename(INBOX)
        fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o600, dir_fd=sfd)
    finally:
        os.close(sfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
        os.close(fd)
        raise Unsafe("inbox is not a regular single-link file of this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.fchmod(fd, 0o600)
    return fd


def _append(project: Path, lines: Sequence[tuple]) -> tuple:
    """Append ``(pid, line)`` pairs whose id is not already in the inbox. Returns (the ids now
    in the inbox, None) or (set(), why not)."""
    try:
        fd = _open_inbox(project)
    except Unsafe as exc:
        return set(), str(exc)
    except OSError as exc:
        return set(), f"cannot open the inbox ({type(exc).__name__})"
    try:
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="replace") as r:
            r.seek(0)
            have = r.read()
        present = {pid for pid, _ln in lines if f" · {pid} · " in have}
        todo = [ln for pid, ln in lines if pid not in present]
        text = ("" if have else HEADER) + "".join(f"{ln}\n" for ln in todo)
        if text:
            data = text.encode("utf-8")
            while data:
                n = os.write(fd, data)
                data = data[n:]
            os.fsync(fd)
    except OSError as exc:
        return set(), f"cannot write the inbox ({type(exc).__name__})"
    finally:
        os.close(fd)
    return {pid for pid, _ln in lines}, None


def route(
    proposals: Sequence[Mapping],
    roots: Sequence[Path],
    ledger: Dict[str, dict],
    day: str,
    lanes: Optional[Mapping[str, str]] = None,
    decisions: Optional[Mapping[str, Mapping]] = None,
    complete_kinds: Optional[Collection[str]] = None,
) -> dict:
    """Route tonight's proposals and update the record in ``ledger`` (in place). Only kinds in
    ``complete_kinds`` (default: every kind seen tonight) can be marked "no longer proposed".
    Returns the figures: routed and acted (all nights), new (tonight), open, unrouted, skipped.
    Never raises for one project: its reason goes to ``skipped`` and the rest go on."""
    lanes = lanes or {}
    decisions = decisions or {}
    record: Dict[str, dict] = ledger.setdefault(KEY, {})
    index = _index(roots)
    tonight = {str(p.get("id")) for p in proposals}
    complete = (
        set(complete_kinds)
        if complete_kinds is not None
        else {str(p.get("kind")) for p in proposals}
    )
    per_project: Dict[Path, List[tuple]] = {}
    lane_of: Dict[tuple, str] = {}
    kind_of: Dict[str, str] = {}
    unrouted = 0
    for prop in proposals:
        pid = str(prop.get("id"))
        kind_of[pid] = str(prop.get("kind"))
        projects: List[Path] = []
        for s in prop.get("sources", []):
            p = project_of(s, roots, index)
            if p is not None and p not in projects:
                projects.append(p)
        if not projects:
            unrouted += 1
            continue
        done = {t.get("path") for t in record.get(pid, {}).get("to", [])}
        for p in projects:
            if str(p) in done:
                continue
            lane = lanes.get(p.name, p.name)
            line = f"- [ ] {lane} · {pid} · {ask(prop)} (dream {day}, {prop.get('kind')})"
            per_project.setdefault(p, []).append((pid, line))
            lane_of[(pid, str(p))] = lane
    skipped, new = [], set()
    for p, lines in sorted(per_project.items()):
        why = _writable(p)
        written = set()
        if why is None:
            written, why = _append(p, lines)
        if why:
            skipped.append(f"{p.name}: {why}")
        for pid in sorted(written):
            entry = record.setdefault(pid, {"kind": kind_of.get(pid), "to": [], "acted": None})
            entry["to"].append(
                {"project": p.name, "path": str(p), "lane": lane_of[(pid, str(p))], "day": day}
            )
            new.add(pid)
    for pid, entry in record.items():
        acted = entry.get("acted")
        if pid in tonight:
            if acted and acted.get("how") == GONE:
                entry["acted"] = None  # it came back
            continue
        if acted:
            continue
        d = decisions.get(pid)
        if isinstance(d, Mapping) and d.get("decision") in ("accept", "reject"):
            entry["acted"] = {"how": f"decided: {d['decision']}", "day": day}
        elif entry.get("kind") in complete:
            entry["acted"] = {"how": GONE, "day": day}
    acted_n = sum(1 for e in record.values() if e.get("acted"))
    return {
        "routed": len(record),
        "new": len(new),
        "acted": acted_n,
        "open": len(record) - acted_n,
        "unrouted": unrouted,
        "skipped": skipped,
    }


def summary(ledger: Mapping) -> str:
    """The lead's one line: ``dreams: N proposals routed, M acted``."""
    record = ledger.get(KEY) if isinstance(ledger, Mapping) else None
    record = record if isinstance(record, Mapping) else {}
    acted = sum(1 for e in record.values() if isinstance(e, Mapping) and e.get("acted"))
    return f"dreams: {len(record)} proposals routed, {acted} acted"
