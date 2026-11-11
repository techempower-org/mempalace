"""The nightly dream: inventory and contradictions in one run, published for review.

The run writes one JSON document (``schema: dreams/v1``) to a review folder, as
``YYYY-MM-DD.json`` and ``latest.json``, each written atomically. A review page reads it and
records each decision in ``decisions.json`` in the same folder (``{id: {"decision": "accept" |
"reject", ...}}``). Every proposal has a stable id, so the same finding keeps its id from night
to night even when the lines around it move. A decided id is never proposed again: a rejected
one is settled, and an accepted one is the lane's to apply.

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
APPLIED = "applied.json"
_DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
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


def load_decisions(path: Optional[Path]) -> Dict[str, dict]:
    """The review page's decisions; empty when there are none yet. A file that cannot be read
    is an error: publishing without it could re-propose what was rejected."""
    if path is None or not path.exists():
        return {}
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
    day = day or _dt.date.today().isoformat()
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
    doc = {
        "schema": SCHEMA,
        "day": day,
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run": {"mempalace": _version(), "steps": steps, "decided_and_dropped": dropped},
        "applied": list(applied or []),
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


def within_size(doc: dict) -> dict:
    """Drop the lowest-priority proposals (they are sorted) until the document fits, and say
    how many went in ``run.truncated``."""
    if _size(doc) <= MAX_BYTES:
        return doc
    props = list(doc["proposals"])
    cut = 0
    while props and _size({**doc, "proposals": props}) > MAX_BYTES:
        drop = max(1, len(props) // 20)
        props, cut = props[:-drop], cut + drop
    return {**doc, "run": {**doc["run"], "truncated": cut}, "proposals": props}


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
    publish_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(publish_dir, 0o700)
    text = json.dumps(doc, indent=2) + "\n"
    day = publish_dir / f"{doc['day']}.json"
    _atomic_write(day, text)
    _atomic_write(publish_dir / "latest.json", text)
    if seen is not None:
        _atomic_write(publish_dir / SEEN, json.dumps(seen, indent=0, sort_keys=True) + "\n")
    prune(publish_dir, doc["day"])
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
