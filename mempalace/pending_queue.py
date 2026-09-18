"""Append-only journal for mine requests that couldn't reach the daemon.

When ``PALACE_DAEMON_URL`` is set but the daemon (or its backend) is
unreachable, ``_post_daemon_mine`` would previously log the failure and
drop the request. With daemon-strict mode in effect, hooks don't fall
back to a local mine — so a stale daemon means silent write loss for
the duration of the outage.

This module captures the dropped requests as JSONL lines under
``~/.mempalace/pending/YYYY-MM-DD.jsonl`` and exposes a ``replay``
function that re-issues them when the daemon recovers. Lines that
succeed are removed from the file; lines that fail stay for the next
attempt. Atomic rewrite via ``tempfile + os.replace`` prevents partial
file corruption on crash mid-drain.

The request payload is tiny — ``{dir, wing, mode, ts}`` — because the
transcript file the daemon actually mines lives on disk and survives
the outage. We don't archive conversation content here; the file on
disk is the durable source.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

PENDING_DIR = Path.home() / ".mempalace" / "pending"


@dataclass(frozen=True)
class ReplayReport:
    """Outcome of a replay sweep."""

    attempted: int
    succeeded: int
    failed: int
    files_drained: int
    dropped: int = 0  # legacy whole-directory requests discarded (see _is_legacy_dir_request)

    @property
    def is_empty(self) -> bool:
        return self.attempted == 0 and self.dropped == 0


def enqueue(request: dict, *, now: datetime | None = None) -> Path:
    """Append a pending mine request to today's queue file.

    ``request`` must contain at minimum ``dir``, ``wing``, ``mode``. A
    UTC ``ts`` field is added automatically. Writes are flushed and
    fsynced before return so a crash immediately after enqueue cannot
    lose the line.
    """
    for required in ("dir", "wing", "mode"):
        if required not in request:
            raise ValueError(f"pending queue request missing field: {required!r}")

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    ts_now = now if now is not None else datetime.now(tz=timezone.utc)
    line_obj = {**request, "ts": ts_now.isoformat()}
    path = PENDING_DIR / f"{ts_now.strftime('%Y-%m-%d')}.jsonl"
    line = json.dumps(line_obj, sort_keys=True, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def pending_count(directory: Path | None = None) -> int:
    """Return the number of queued (not-yet-replayed) requests.

    Useful for the session-start warning. Cheap: counts non-empty
    lines without parsing JSON.
    """
    base = directory if directory is not None else PENDING_DIR
    if not base.is_dir():
        return 0
    total = 0
    for path in base.glob("*.jsonl"):
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if raw.strip():
                        total += 1
        except OSError:
            continue
    return total


def _dedupe(lines: Iterable[str]) -> list[str]:
    """Drop duplicate (dir, wing, mode) requests, keeping the newest ts.

    During a long outage the same (dir, wing, mode) tuple may be
    enqueued dozens of times (one per Stop hook fire). Replaying all
    of them is wasteful — the daemon-side mine is idempotent but each
    call still costs a round-trip. Dedupe on the way out keeps replay
    proportional to the number of *distinct* targets, not the number
    of fires.

    Malformed lines (bad JSON) are dropped silently — they were
    unreplayable anyway.
    """
    seen: dict[tuple[str, str, str], dict] = {}
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        key = (obj.get("dir", ""), obj.get("wing", ""), obj.get("mode", ""))
        prev = seen.get(key)
        if prev is None or obj.get("ts", "") >= prev.get("ts", ""):
            seen[key] = obj
    # Preserve oldest-first ordering so older targets are tried first.
    return [
        json.dumps(obj, sort_keys=True, ensure_ascii=False)
        for obj in sorted(seen.values(), key=lambda o: o.get("ts", ""))
    ]


def _is_legacy_dir_request(request: dict) -> bool:
    """A pre-2026-09-03 journal entry: a convos mine of a whole project directory.

    Hooks used to journal ``{"dir": ~/.claude/projects/<proj>, "mode": "convos"}``
    when the daemon call timed out. Replaying one re-mines every session in
    the project (measured: 6h holding the palace write lock) and, because it
    times out again, it stayed queued forever — 559 such entries were found
    and archived. Since #431/#433 hooks post a single ``.jsonl`` transcript, so
    anything convos-shaped that is NOT a ``.jsonl`` file is legacy and is
    dropped. ``projects``/``session`` mode requests are untouched.
    """
    if not isinstance(request, dict):
        return False
    if str(request.get("mode", "convos")) != "convos":
        return False
    target = str(request.get("dir", "") or "").replace("\\", "/")
    if target.lower().endswith(".jsonl"):
        return False
    # Only the Claude Code transcript-dir shape is known-legacy; other convos
    # directories (exports, other harnesses) keep their existing behaviour.
    return "/.claude/projects/" in target


def peek(directory: Optional[Path] = None) -> list:
    """Every request a :func:`replay` would POST, without touching the queue.

    Read-only on purpose and by construction: ``replay`` claims each file by
    renaming it before it reads a line, so using it with a no-op ``post_fn``
    to preview a drain would still mutate the queue — a dry run that is not
    dry.

    It reproduces replay's filter chain rather than counting lines, because
    the two must agree. Replay de-duplicates per file, skips unparseable
    lines, and DROPS legacy whole-directory requests without posting them
    (the multi-hour lock-holder of #414/#426). A preview over raw lines would
    promise twelve and post nine — the same "report disagrees with what
    happened" defect ``pending drain`` was added to remove (#498).

    Not deadline-aware: a plan describes the whole queue, while a real sweep
    may stop early. That difference is one-sided and safe — the plan can
    over-state what a time-boxed run achieves, never under-state what an
    unbounded one will touch.
    """
    base = directory if directory is not None else PENDING_DIR
    if not base.is_dir():
        return []
    planned: list = []
    for path in sorted(base.glob("*.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                raw_lines = [line.rstrip("\n") for line in f if line.strip()]
        except OSError:
            continue
        for line in _dedupe(raw_lines):
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_legacy_dir_request(request):
                continue
            planned.append(request)
    return planned


def replay(
    post_fn: Callable[[dict], bool],
    *,
    directory: Optional[Path] = None,
    deadline: Optional[float] = None,
) -> ReplayReport:
    """Drain the queue by re-issuing each request via ``post_fn``.

    ``post_fn(request) -> bool`` is the caller's hook to actually
    transmit one request. ``True`` means the daemon accepted it and
    the line should be consumed; ``False`` means keep it for next
    time. Anything that raises is treated as ``False`` — replay is a
    best-effort background sweep and must not abort partway just
    because one request blew up.

    **Concurrency model (Gemini PR #104 review):** atomic-rewrite via
    ``tempfile + os.replace`` is NOT safe against concurrent
    ``enqueue`` calls: any line appended while we hold the file in
    memory would be silently lost when we replace. We instead
    "claim" each file by renaming it to ``<name>.replay-<pid>`` before
    reading. ``enqueue`` always writes to ``YYYY-MM-DD.jsonl``, so any
    concurrent appends land in a fresh file rather than the one we're
    draining. Failed lines are appended back to the live file (not
    rewritten) so they merge with any newly-enqueued entries.

    ``deadline`` is a ``time.monotonic()`` timestamp at which the
    sweep stops processing more lines and returns whatever has been
    drained so far. Used by ``hook_session_start`` to cap total replay
    cost on the hot path. ``None`` (default) means no time limit.
    """
    import time

    base = directory if directory is not None else PENDING_DIR
    if not base.is_dir():
        return ReplayReport(0, 0, 0, 0)

    attempted = succeeded = failed = files_drained = dropped = 0
    pid = os.getpid()

    for path in sorted(base.glob("*.jsonl")):
        if deadline is not None and time.monotonic() >= deadline:
            break

        # Claim the file by renaming. Any enqueue racing us will write to
        # the original name (creating a fresh file or appending to one we
        # already drained) — no data loss either way.
        claimed = path.with_name(path.name + f".replay-{pid}")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            # Another replay process beat us to it.
            continue
        except OSError:
            continue

        try:
            with open(claimed, encoding="utf-8") as f:
                raw_lines = [line.rstrip("\n") for line in f if line.strip()]
        except OSError:
            # Couldn't read claimed file; leave it for manual recovery.
            continue

        if not raw_lines:
            try:
                claimed.unlink()
                files_drained += 1
            except OSError:
                pass
            continue

        unique_lines = _dedupe(raw_lines)
        remaining: list[str] = []
        for line in unique_lines:
            if deadline is not None and time.monotonic() >= deadline:
                # Time's up — preserve unprocessed lines so they replay next.
                remaining.append(line)
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                attempted += 1
                failed += 1
                continue
            if _is_legacy_dir_request(request):
                # Consume, don't replay: a whole-project convos mine is the
                # multi-hour lock-holder that #414/#426 describe, and every
                # transcript it would cover is re-mined per checkpoint anyway.
                dropped += 1
                continue
            attempted += 1
            try:
                ok = bool(post_fn(request))
            except Exception:
                ok = False
            if ok:
                succeeded += 1
            else:
                failed += 1
                remaining.append(line)

        if not remaining:
            try:
                claimed.unlink()
                files_drained += 1
            except OSError:
                pass
        else:
            # Append failed lines back to the live queue file. This merges
            # with any concurrent enqueue activity; the daemon's per-drawer
            # idempotency handles any resulting duplicates.
            try:
                with open(path, "a", encoding="utf-8") as live:
                    live.write("\n".join(remaining) + "\n")
                    live.flush()
                    os.fsync(live.fileno())
                try:
                    claimed.unlink()
                except OSError:
                    pass
            except OSError:
                # If append failed, keep claimed file in place so a future
                # replay can pick it up.
                pass

    return ReplayReport(attempted, succeeded, failed, files_drained, dropped)
