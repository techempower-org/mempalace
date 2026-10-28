"""livekit-voice call-record source adapter.

Ingests the phone calls the livekit-voice agent makes and takes: one JSON
record per call at ``~/.local/share/livekit-voice/calls/<call_id>.json``,
written atomically by ``lkvoice.records``. Each record's ``transcript`` is a
list of ``{"role", "t", "text"}`` turns, ``user`` being the other party and
``assistant`` the agent.

A call holds an outside party's words and, often, benefits or health
details, so the adapter's privacy class is ``sensitive`` (RFC 002 §6) and
core files its drawers in the private collection that search, status and
wake-up never open. Drawer content is only the spoken words: the
model-written ``summary`` is not verbatim and is dropped, ``metrics`` carry
timings, not content, and the ``task`` the call was placed for is kept as
``call_task`` metadata rather than as something said. No
audio is stored in a record. Records still in progress, and anything under
``~/private``, are skipped.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from ..convo_miner import chunk_exchanges, detect_convo_room
from .base import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    RouteHint,
    SourceNotFoundError,
    SourceRef,
    SourceSummary,
)
from .context import PalaceContext

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "~/.local/share/livekit-voice/calls"
# Never read, whatever path is passed: JP keeps transcripts that must stay out of the palace here.
_EXCLUDED_ROOTS: Tuple[str, ...] = ("~/private",)
# lkvoice.records.FINAL: a call in any other status is still being written.
_FINAL_STATUSES = frozenset({"completed", "no-answer", "busy", "failed", "denied"})
_WING = "private_calls"


def _excluded(path: Path) -> bool:
    resolved = path.resolve()
    for root in _EXCLUDED_ROOTS:
        base = Path(root).expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return True
    return False


def _find_records(base_dir: Path) -> List[Path]:
    """Regular files only: lkvoice.records writes each record in place, so a symlink among them
    is not a call record and could point anywhere, ``~/private`` included."""
    if not base_dir.is_dir():
        return []
    return sorted(
        p
        for p in base_dir.glob("*.json")
        if not p.is_symlink() and p.is_file() and not _excluded(p)
    )


def _read_record(path: Path) -> Tuple[object, float]:
    """Read without following a symlink swapped in after enumeration (O_NOFOLLOW), and recheck
    the exclusion against the path as it resolves now."""
    if _excluded(path):
        raise OSError(f"{path.name} now resolves under an excluded root")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        mtime = os.fstat(f.fileno()).st_mtime
        return json.loads(f.read().decode("utf-8")), mtime


def _iso(ts) -> Optional[str]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):  # NaN, Infinity or out of range
        return None


def _call_transcript(record: dict) -> Optional[str]:
    """The call's spoken turns as an exchange transcript, the other party's turns quoted with
    ``> `` so ``chunk_exchanges`` pairs each with the agent's reply. None when nothing was said."""
    turns = record.get("transcript")
    if not isinstance(turns, list):
        return None
    lines: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = turn.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        text = " ".join(text.split())
        role = turn.get("role")
        if role == "user":
            lines.append(f"> {text}")
        elif role == "assistant":
            lines.append(text)
            lines.append("")
        # any other role (system, tool, none) was not spoken on the call
    if not lines:
        return None
    header = [f"Phone call {record.get('id', '?')} ({record.get('direction', 'unknown')})"]
    started = _iso(record.get("started"))
    if started:
        header.append(f"Started: {started}")
    return "\n".join(header) + "\n\n" + "\n".join(lines).rstrip() + "\n"


class LivekitCallsSourceAdapter(BaseSourceAdapter):
    """Ingest livekit-voice call records into the private collection."""

    name = "livekit-calls"
    adapter_version = "1.0.0"
    supported_modes = frozenset({"chunked_content"})
    declared_transformations = frozenset({"chunk_exchanges", "livekit_call_record_parse"})
    default_privacy_class = "sensitive"

    def _base_dir(self, source: SourceRef) -> Path:
        base = Path(source.local_path or _DEFAULT_PATH).expanduser()
        if _excluded(base):
            raise SourceNotFoundError(f"livekit-calls: {base} is under an excluded root; not read")
        if not base.is_dir():
            raise SourceNotFoundError(f"livekit-calls: no call records directory at {base}")
        return base

    def ingest(
        self,
        *,
        source: SourceRef,
        palace: PalaceContext,
    ) -> Iterator[DrawerRecord]:
        for path in _find_records(self._base_dir(source)):
            try:
                record, mtime = _read_record(path)
            except (OSError, ValueError, RecursionError) as exc:
                logger.warning("livekit-calls: skipping unreadable %s: %s", path.name, exc)
                continue
            if not isinstance(record, dict):
                continue
            status = record.get("status")
            if not isinstance(status, str) or status not in _FINAL_STATUSES:
                continue
            transcript = _call_transcript(record)
            if transcript is None:
                continue
            room = detect_convo_room(transcript)
            # upsert_drawer files metadata as given and does not read route_hint, so the wing and
            # room the backend indexes on are set here.
            meta = {
                "wing": _WING,
                "room": room,
                "call_id": str(record.get("id", path.stem)),
                "direction": str(record.get("direction", "")),
                "call_status": str(record.get("status", "")),
                "call_started": _iso(record.get("started")) or "",
                # the request the call was placed for: context, not something said on the call
                "call_task": " ".join(task.split())
                if isinstance(task := record.get("task"), str)
                else "",
                "source_mtime": mtime,
                "privacy_class": self.default_privacy_class,
            }
            for i, chunk in enumerate(chunk_exchanges(transcript)):
                content = chunk if isinstance(chunk, str) else chunk.get("content", str(chunk))
                yield DrawerRecord(
                    content=content,
                    source_file=str(path),
                    chunk_index=i,
                    metadata=dict(meta),
                    route_hint=RouteHint(wing=_WING, room=room),
                )

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(
            fields={
                "source_file": FieldSpec(
                    type="string",
                    required=True,
                    description="absolute path to the call record JSON",
                    indexed=True,
                ),
                "call_id": FieldSpec(type="string", required=True, description="call id"),
                "direction": FieldSpec(
                    type="string", required=True, description="inbound or outbound"
                ),
                "call_status": FieldSpec(
                    type="string", required=True, description="final status of the call"
                ),
                "call_started": FieldSpec(
                    type="string", required=False, description="call start, ISO 8601 UTC"
                ),
                "privacy_class": FieldSpec(
                    type="string", required=True, description="RFC 002 §6 class: sensitive"
                ),
            },
            version=self.adapter_version,
        )

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        try:
            base = self._base_dir(source)
        except SourceNotFoundError as exc:
            return SourceSummary(description=str(exc))
        return SourceSummary(
            description=f"livekit-calls: {base} (private collection)",
            item_count=len(_find_records(base)),
        )
