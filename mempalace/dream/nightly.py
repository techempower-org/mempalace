"""The nightly dream: inventory and contradictions in one run, published for review.

The run writes one JSON document (``schema: dreams/v1``) to a review folder, as
``YYYY-MM-DD.json`` and ``latest.json``, each written atomically. A review page reads it and
records each decision in ``decisions.json`` in the same folder (``{id: {"decision": "accept" |
"reject", ...}}``). Every proposal has a stable id, so the same finding keeps its id from night
to night even when the lines around it move. A decided id is never proposed again: a rejected
one is settled, and an accepted one is applied by the next run when that is mechanical (see
:func:`apply_decisions`), recorded in ``applied`` and the ``applied.json`` ledger; otherwise it is
listed under ``run.accepted_waiting`` with the reason, for a lane.

The inputs are private, and so is the document: point ``--publish-dir`` at a folder that is
never in a repository. The host's memory is checked before every model call; under the floor
the contradictions pass stops and says so.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import contradictions, inventory

SCHEMA = "dreams/v1"
MIN_AVAILABLE_MB = 6144
# The review page's limits (agreed with the plan lane 2026-11-11): over any of them it shows
# "unreadable" rather than a partial page, so the writer keeps every document inside them.
MAX_BYTES = 2 * 1024 * 1024
MAX_SUMMARY = 1200
MAX_SOURCES = 10
MAX_DETAIL_KEYS = 12
KEEP_DAYS = 30
# Kinds in the order they are kept when the file would be too big: the last go first.
KIND_PRIORITY = ["contradiction", "near-duplicate", "oversized-index", "no-mechanism"]
SEEN = ".seen.json"
LOCK = ".nightly.lock"
APPLIED = "applied.json"
_DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_LINE = re.compile(r":\d+$")


def proposal_id(p: inventory.Proposal) -> str:
    """Stable across nights: kind, the source paths without line numbers, and the rule lead or
    memory names that say what the finding is about."""
    paths = sorted(_LINE.sub("", s) for s in p.sources)
    anchor = str(p.detail.get("lead", "")) if p.kind == "no-mechanism" else ""
    if p.kind == "contradiction":
        anchor = str(p.detail.get("rule", ""))
    raw = "\n".join([p.kind, *paths, anchor])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class DecisionsMissing(RuntimeError):
    """The decisions store is gone after a first run: publishing would re-propose rejections."""


def load_decisions(path: Path, first_run: bool) -> Dict[str, dict]:
    """The review page's decisions. On a first run (no ledger yet) a missing store is created
    empty, once, exclusively; after that a missing, unreadable or malformed one is an error, so
    a deleted or half-written file can never bring a rejection back."""
    if first_run and not os.path.lexists(path):
        private_dir(path.parent)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("{}\n")
    if not path.is_file():
        raise DecisionsMissing(f"{path}: decisions store missing after a first run")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: decisions must be a JSON object")
    return data


def memory_guard(min_available_mb: int = MIN_AVAILABLE_MB, meminfo: str = "/proc/meminfo"):
    """A guard for the model pass: a reason to stop when MemAvailable is under the floor."""

    def guard() -> Optional[str]:
        try:
            with open(meminfo, encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        mb = int(line.split()[1]) // 1024
                        if mb < min_available_mb:
                            return f"MemAvailable {mb} MB < {min_available_mb} MB"
                        return None
        except OSError as exc:
            return f"cannot read {meminfo}: {exc}"
        return f"no MemAvailable in {meminfo}"

    return guard


def _version() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        from .. import __version__

        return str(__version__)
    except ImportError:
        return "unknown"


def published_proposals(publish_dir: Path) -> Dict[str, dict]:
    """Every proposal the folder's published nights still show, by id (newest night wins), so
    a decision on a proposal that no longer recurs can still be applied."""
    known: Dict[str, dict] = {}
    if not publish_dir.is_dir():
        return known
    names = sorted(p.name for p in publish_dir.iterdir() if _DAY_FILE.match(p.name))
    for name in [*names, "latest.json"]:
        try:
            doc = json.loads((publish_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for prop in doc.get("proposals", []) if isinstance(doc, dict) else []:
            if isinstance(prop, dict) and isinstance(prop.get("id"), str):
                known[prop["id"]] = prop
    return known


def _same_file(sources: Sequence[str]) -> Optional[str]:
    """For two sources that are one file reached through two names of ONE memory folder (the
    alias :func:`inventory.memory_folders` now reads once), the real path; else None. Two real
    folders that each hold a link to a shared file are still read twice, so they never count."""
    if len(sources) != 2:
        return None
    try:
        paths = [Path(_LINE.sub("", s)) for s in sources]
        a, b = (q.resolve(strict=True) for q in paths)
        fa, fb = (q.parent.resolve(strict=True) for q in paths)
    except OSError:
        return None
    if a != b or fa != fb or paths[0].parent == paths[1].parent:
        return None
    if a.parent != fa or not any(q.parent.is_symlink() for q in paths):
        return None
    return str(a)


def apply_decisions(
    decisions: Dict[str, dict],
    applied: Sequence[dict],
    known: Dict[str, dict],
    now: Optional[str] = None,
):
    """(the applied ledger with tonight's additions, the accepted ids still waiting). Only what
    needs no judgement is applied here: a near-duplicate whose two sources are one file read
    through a symlinked folder (nothing to merge; the inventory now reads each folder once).
    Every other accepted id waits, with its reason, for a lane; a rejected id needs nothing."""
    now = now or _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    ledger = [a for a in applied if isinstance(a, dict)]
    done = {a.get("id") for a in ledger}
    waiting = []
    for pid, d in sorted(decisions.items()):
        if not isinstance(d, dict) or d.get("decision") != "accept" or pid in done:
            continue
        prop = known.get(pid)
        if prop is None:
            waiting.append({"id": pid, "reason": "not in any published night"})
            continue
        real = _same_file(prop.get("sources", [])) if prop.get("kind") == "near-duplicate" else None
        if real is None:
            waiting.append(
                {"id": pid, "kind": prop.get("kind"), "reason": "needs a lane: not mechanical"}
            )
            continue
        ledger.append(
            {
                "id": pid,
                "kind": prop.get("kind"),
                "decision": "accept",
                "by": d.get("by"),
                "at": d.get("at"),
                "applied_at": now,
                "sources": list(prop.get("sources", []))[:MAX_SOURCES],
                "result": (
                    f"one file, {real}, read through a symlinked memory folder: nothing to merge; "
                    "the inventory now reads each real folder once, so the pair is gone"
                ),
            }
        )
    return ledger, waiting


def run(
    memory_root: Path,
    claude_md: Sequence[Path],
    embed: inventory.Embedder,
    judge: Optional[contradictions.Judge],
    decisions: Dict[str, dict],
    guard: Optional[Callable[[], Optional[str]]] = None,
    day: Optional[str] = None,
    seen: Optional[Dict[str, str]] = None,
    applied: Optional[List[dict]] = None,
    global_dirs: Sequence[Path] = (),
    known: Optional[Dict[str, dict]] = None,
):
    """(the night's document, the updated first-seen ledger). ``judge`` None skips the model
    pass (recorded in ``run``). ``seen`` maps each id to the night it first appeared."""
    steps: Dict[str, dict] = {}
    proposals: List[inventory.Proposal] = inventory.run(memory_root, claude_md, embed)
    steps["inventory"] = {"ok": True, "proposals": len(proposals)}
    if judge is not None:
        found, stats = contradictions.contradictions(
            inventory.read_memories(memory_root),
            contradictions.read_rules(claude_md),
            embed,
            judge,
            guard=guard,
            global_dirs=global_dirs,
        )
        proposals += found
        steps["contradictions"] = stats
    else:
        steps["contradictions"] = {"skipped": True}
    day = check_day(day) if day is not None else _dt.date.today().isoformat()
    seen = dict(seen or {})
    out, dropped = [], 0
    for p in proposals:
        pid = proposal_id(p)
        if pid in decisions:
            dropped += 1
            continue
        seen.setdefault(pid, day)
        out.append(fit({"id": pid, **p.as_dict(), "first_seen": seen[pid]}))
    out.sort(key=lambda d: KIND_PRIORITY.index(d["kind"]) if d["kind"] in KIND_PRIORITY else 99)
    ledger, waiting = apply_decisions(decisions, applied or [], known or {})
    run_info = {"mempalace": _version(), "steps": steps, "decided_and_dropped": dropped}
    if waiting:
        run_info["accepted_waiting"] = waiting
    doc = {
        "schema": SCHEMA,
        "day": day,
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run": run_info,
        "applied": ledger,
        "proposals": out,
    }
    return within_size(doc), seen


def fit(proposal: dict) -> dict:
    """One proposal inside the page's limits: summary cut to its length, at most ten sources,
    a flat detail of at most twelve string, number or boolean values."""
    summary = proposal["summary"]
    if len(summary) > MAX_SUMMARY:
        summary = summary[: MAX_SUMMARY - 1] + "\u2026"
    detail = {
        k: v
        for k, v in proposal.get("detail", {}).items()
        if isinstance(v, (str, int, float, bool)) and not isinstance(v, type(None))
    }
    detail = dict(list(detail.items())[:MAX_DETAIL_KEYS])
    for k, v in detail.items():
        if isinstance(v, str) and len(v) > MAX_SUMMARY:
            detail[k] = v[: MAX_SUMMARY - 1] + "\u2026"
    return {
        **proposal,
        "summary": summary,
        "sources": [str(s) for s in proposal["sources"][:MAX_SOURCES]],
        "detail": detail,
    }


def _size(doc: dict) -> int:
    return len((json.dumps(doc, indent=2) + "\n").encode())


class TooLarge(ValueError):
    """The document is over the page's limit even with no proposals left."""


def within_size(doc: dict) -> dict:
    """Drop the lowest-priority proposals (they are sorted) until the final document, its
    ``run.truncated`` count included, fits; refuse when the rest alone is over the limit."""
    if _size(doc) <= MAX_BYTES:
        return doc
    props, cut = list(doc["proposals"]), 0

    def final():
        return {**doc, "run": {**doc["run"], "truncated": cut}, "proposals": props}

    while props and _size(final()) > MAX_BYTES:
        drop = max(1, len(props) // 20)
        props, cut = props[:-drop], cut + drop
    out = final()
    if _size(out) > MAX_BYTES:
        raise TooLarge(f"{_size(out)} bytes without proposals; limit {MAX_BYTES}")
    return out


def single_writer(publish_dir: Path):
    """An exclusive, non-blocking lock on ``<publish_dir>/.nightly.lock``, held while the
    returned file object stays open (the process's life); None when another run holds it."""
    import fcntl

    f = open(publish_dir / LOCK, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def private_dir(path: Path) -> Path:
    """``path`` as a 0700 folder (created if needed) before anything private goes into it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def check_day(day: str) -> str:
    """A canonical YYYY-MM-DD, so a day can only ever name a file directly in the folder."""
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")
    _dt.date.fromisoformat(day)
    return day


def _atomic_write(path: Path, text: str, mode: int = 0o640) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path, default):
    """A ledger this writer or the lane keeps; ``default`` when it does not exist yet."""
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def publish(doc: dict, publish_dir: Path, seen: Optional[Dict[str, str]] = None) -> Path:
    """Write ``<day>.json`` and ``latest.json`` (0640) atomically into ``publish_dir`` (0700),
    save the first-seen ledger, and prune day files older than :data:`KEEP_DAYS`."""
    name = check_day(doc["day"])
    text = json.dumps(doc, indent=2) + "\n"
    if len(text.encode()) > MAX_BYTES:
        raise TooLarge(f"{len(text.encode())} bytes; limit {MAX_BYTES}")
    private_dir(publish_dir)
    day = publish_dir / f"{name}.json"
    # The ledger first and latest.json last: a run that stops part way never shows a document
    # whose first-seen nights were not saved.
    if seen is not None:
        _atomic_write(publish_dir / SEEN, json.dumps(seen, indent=0, sort_keys=True) + "\n")
    if "applied" in doc:
        _atomic_write(publish_dir / APPLIED, json.dumps(doc["applied"], indent=2) + "\n")
    _atomic_write(day, text)
    _atomic_write(publish_dir / "latest.json", text)
    prune(publish_dir, name)
    return day


def prune(publish_dir: Path, today: str, keep_days: int = KEEP_DAYS) -> List[Path]:
    """Remove ``YYYY-MM-DD.json`` files more than ``keep_days`` before ``today``; nothing else."""
    cutoff = _dt.date.fromisoformat(today) - _dt.timedelta(days=keep_days)
    gone = []
    for path in publish_dir.iterdir():
        if not _DAY_FILE.match(path.name):
            continue
        try:
            when = _dt.date.fromisoformat(path.name[:10])
        except ValueError:
            continue
        if when < cutoff:
            path.unlink()
            gone.append(path)
    return gone
