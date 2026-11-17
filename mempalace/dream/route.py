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
is written only when git, run in the very folder opened for the write and with no inherited
``GIT_*`` variable, says it is outside every work tree or ignores the inbox path. The write
goes through descriptors opened from ``/`` without following links; the project and
``scratch/`` must be this user's and writable by no one else (a group of this user alone
counts as no one else); the inbox must be a regular file with one link, owned by this user,
and is kept at mode 0600; new directory entries and the inbox are synced. A proposal id already on a complete line of the inbox is never
appended again, so a run that stops between the append and the ledger save repeats nothing,
and a torn last line is closed before anything is appended after it.

The routing is recorded in the verdict ledger (``judged.json``) under :data:`KEY`: for each
routed id, its kind, where it went and whether it has been acted on, which it has once it is
decided on the review page, or once a night whose producer for its kind ran complete no
longer proposes it (a proposal proposed again with no decision on it is open again).
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
_UNSAFE_LANE = re.compile(r"[^A-Za-z0-9._+-]")
# A complete inbox line; only these count as delivered.
_ENTRY = re.compile(
    r"^- \[[ xX]\] [A-Za-z0-9._+-]+ · ([A-Za-z0-9]{1,64}) · .+ \(dream \d{4}-\d{2}-\d{2}, [a-z-]+\)$"
)
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


def lane_text(name: str) -> str:
    """A lane name safe inside one inbox line: letters, digits and ``._+-`` only."""
    return _UNSAFE_LANE.sub("-", str(name))[:64] or "-"


def ask(proposal: Mapping) -> str:
    """The proposal's first sentence, on one line, at most :data:`MAX_ASK` characters."""
    text = " ".join(str(proposal.get("summary", "")).split())
    m = _SENTENCE.match(text)
    text = m.group(1) if m else text
    if len(text) > MAX_ASK:
        text = text[: MAX_ASK - 1] + "…"
    return text.replace("·", "-")


# git's own "searched and found none" answers. A repository past a filesystem boundary is one
# git never uses (GIT_DISCOVERY_ACROSS_FILESYSTEM is dropped with every GIT_* variable), so it
# can never track the inbox either. Anything else, such as "not a git repository: <gitdir>"
# from a broken pointer, is not an answer.
_GIT_OUTSIDE = (
    "not a git repository (or any of the parent directories)",
    "not a git repository (or any parent up to mount point ",
)


def _git_env() -> Dict[str, str]:
    """The environment for git: no inherited variable that picks a repository or work tree."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    return env


def _writable(pfd: int) -> Optional[str]:
    """None when git will never track the inbox of the folder open on ``pfd``, else why not.
    Git runs in that very folder (``/proc/self/fd/<pfd>``), so what it judges is what is
    written; any answer but a positive one is a no."""
    here = f"/proc/self/fd/{pfd}"
    if not os.path.isdir(here):
        return "cannot run git in the opened folder (no /proc/self/fd)"
    run = dict(capture_output=True, text=True, timeout=10, env=_git_env(), pass_fds=(pfd,))
    try:
        inside = subprocess.run(["git", "-C", here, "rev-parse", "--is-inside-work-tree"], **run)
        if inside.returncode != 0:
            if any(m in inside.stderr for m in _GIT_OUTSIDE):
                return None  # git searched every parent it would use and found no repository
            return "git could not tell whether it is in a work tree"
        if inside.stdout.strip() != "true":
            return "inside a git directory"
        ignored = subprocess.run(["git", "-C", here, "check-ignore", "-q", INBOX], **run)
    except (OSError, subprocess.SubprocessError):
        return "git could not be asked"
    if ignored.returncode == 0:
        return None
    if ignored.returncode == 1:
        return "the inbox path is not git-ignored"
    return "git could not tell whether the inbox is ignored"


class Unsafe(Exception):
    """The inbox's target is not one this writer will write through."""


def _private_group(gid: int) -> bool:
    """True for this user's own group with no other member: none listed in the group, and no
    other account with it as its primary group (the user-private-group scheme, where a 775
    folder is still writable by this user alone). Any doubt is False."""
    if gid != os.getgid():
        return False
    try:
        import grp
        import pwd

        if grp.getgrgid(gid).gr_mem:
            return False
        me = os.getuid()
        return not any(u.pw_gid == gid and u.pw_uid != me for u in pwd.getpwall())
    except (ImportError, KeyError, OSError):
        return False


def _safe_dir(fd: int, what: str) -> None:
    st = os.fstat(fd)
    others = st.st_mode & stat.S_IWOTH
    group = st.st_mode & stat.S_IWGRP and not _private_group(st.st_gid)
    if st.st_uid != os.getuid() or others or group:
        raise Unsafe(f"{what} is not this user's or others can write it")


def _walk(path: Path, flags: int) -> int:
    """A directory descriptor for absolute ``path``, every component opened from ``/``
    without following a link."""
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            nfd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_inbox(project: Path) -> int:
    """A read-write append descriptor for the project's inbox. ``project`` is a real path;
    every component is opened without following a link; the project and ``scratch/`` must be
    this user's (see :func:`_safe_dir`); git is asked about the folder actually opened; and
    each new directory entry is synced. Raises :class:`Unsafe` with the reason when the
    target is not one to write."""
    if _NOFOLLOW is None or _DIRECTORY is None:
        raise Unsafe("this platform cannot open without following links")
    dflags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
    pfd = _walk(project, dflags)
    try:
        _safe_dir(pfd, "the project folder")
        why = _writable(pfd)
        if why:
            raise Unsafe(why)
        try:
            os.mkdir("scratch", 0o700, dir_fd=pfd)
        except FileExistsError:
            pass
        os.fsync(pfd)  # the scratch/ entry is durable, also when a stopped run made it
        sfd = os.open("scratch", dflags, dir_fd=pfd)
    finally:
        os.close(pfd)
    try:
        _safe_dir(sfd, "scratch/")
        name = os.path.basename(INBOX)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=sfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
                raise Unsafe("inbox is not a regular single-link file of this user")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            os.fsync(sfd)  # the inbox's entry is durable
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(sfd)
    return fd


def _append(project: Path, lines: Sequence[tuple]) -> tuple:
    """Append ``(pid, line)`` pairs whose id is not already on a complete line of the inbox,
    and sync it. Returns (the ids now in the inbox, None) or (set(), why not)."""
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
        delivered = {
            m.group(1) for m in map(_ENTRY.match, have.split("\n")[:-1]) if m is not None
        }  # complete lines only: a torn last line (no newline) is never a delivery
        todo = [ln for pid, ln in lines if pid not in delivered]
        lead = HEADER if not have else ("" if have.endswith("\n") else "\n")
        text = lead + "".join(f"{ln}\n" for ln in todo) if todo or not have else ""
        data = text.encode("utf-8")
        while data:
            n = os.write(fd, data)
            data = data[n:]
        os.fsync(fd)  # always: lines a stopped run left unsynced are durable before the ledger
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
            lane = lane_text(lanes.get(p.name, p.name))
            line = f"- [ ] {lane} · {pid} · {ask(prop)} (dream {day}, {prop.get('kind')})"
            per_project.setdefault(p, []).append((pid, line))
            lane_of[(pid, str(p))] = lane
    skipped, new = [], set()
    for p, lines in sorted(per_project.items()):
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
        d = decisions.get(pid)
        decided = isinstance(d, Mapping) and d.get("decision") in ("accept", "reject")
        if pid in tonight and not decided:
            entry["acted"] = None  # proposed again with no decision on it: open
            continue
        if entry.get("acted"):
            continue
        if decided:
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
