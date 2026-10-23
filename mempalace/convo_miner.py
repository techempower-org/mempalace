#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import errno
import os
import sys
import json
import hashlib
import logging
import stat
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from .backends import PalaceNotFoundError
from .mine_phase import report as report_phase
from .collision_scan import assert_no_collisions
from .ids import (
    ID_RECIPE,
    make_convo_drawer_id,
    make_convo_sentinel_id,
    make_exchange_drawer_id,
)
from .normalize import UnparsedCodexTranscriptError, normalize_conversations
from .source_identity import identity_metadata, source_directory_identity
from .entities import entities_metadata
from .palace import (
    CONVO_CHUNKER_VERSION,
    NORMALIZE_VERSION,
    SKIP_DIRS,
    _metadata_matches_extract_mode,
    _validate_palace_fts5_after_mine,
    file_already_mined,
    get_collection,
    mine_lock,
    mine_palace_lock,
    mine_yield_point,
    prefetch_content_hashes,
    prefetch_mined_set,
)

logger = logging.getLogger("mempalace_mcp")


# Cached hall keywords — avoids re-reading config per drawer
_HALL_KEYWORDS_CACHE = None


def _detect_hall_cached(content: str) -> str:
    """Route content to a hall using cached keywords. Same logic as miner.detect_hall."""
    global _HALL_KEYWORDS_CACHE
    if _HALL_KEYWORDS_CACHE is None:
        from .config import MempalaceConfig

        _HALL_KEYWORDS_CACHE = MempalaceConfig().hall_keywords
    content_lower = content[:3000].lower()
    scores = {}
    for hall, keywords in _HALL_KEYWORDS_CACHE.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[hall] = score
    return max(scores, key=scores.get) if scores else "general"


def file_conversation_exchange(
    collection,
    *,
    wing: str,
    room: str,
    text: str,
    source_file: str,
    agent: str,
    authored_at: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
) -> Optional[str]:
    """File one verbatim conversation exchange as a single drawer.

    Canonical write path for live agent integrations (e.g. Hermes) and
    their backfills — both must route here so routing, normalization,
    and metadata conventions stay identical between live and historical
    ingest. Builds the same metadata the convo miner writes so hallway
    traversal, entity search, and since/before date filters see
    integration drawers exactly like mined ones.

    ``wing`` and ``room`` are validated with the same ``sanitize_name``
    rules the MCP write tools apply, but a failed name falls back
    (``wing_general`` / ``conversations``) instead of erroring: this
    path files *live* turns, and dropping a turn over a config typo
    would break the verbatim / 100%-recall promise. The fallback is
    logged at warning level so the misconfiguration is visible.

    ``extra_metadata`` lets callers append integration-specific fields
    (e.g. ``source`` / ``session_id``); keys that collide with the
    canonical fields are ignored, so it cannot be used to overwrite or
    drop them. Returns the drawer id, or None when ``text`` is empty
    after stripping.
    """
    from .config import sanitize_name

    text = (text or "").strip()
    if not text:
        return None
    try:
        wing = sanitize_name(wing, "wing")
    except ValueError:
        logger.warning(
            "file_conversation_exchange: invalid wing %r — filing under wing_general", wing
        )
        wing = "wing_general"
    try:
        room = sanitize_name(room, "room")
    except ValueError:
        logger.warning(
            "file_conversation_exchange: invalid room %r — filing under conversations", room
        )
        room = "conversations"
    filed_at = datetime.now().isoformat()
    drawer_id = make_exchange_drawer_id(wing, room, source_file, filed_at, text)
    metadata = {
        "wing": wing,
        "room": room,
        "hall": _detect_hall_cached(text),
        "source_file": source_file,
        "chunk_index": 0,
        "added_by": agent,
        "filed_at": filed_at,
        "entities": entities_metadata(text),
        "authored_at": authored_at if authored_at is not None else filed_at,
        "ingest_mode": "convos",
        "extract_mode": "exchange",
        "normalize_version": NORMALIZE_VERSION,
        # Not produced by the chunker. Stamped so the version checks treat a
        # live row exactly as they did before the field existed; the separate
        # source_mtime rule is unchanged.
        "convo_chunker_version": CONVO_CHUNKER_VERSION,
        "id_recipe": ID_RECIPE,
        # Same directory identity a mined drawer carries, so ``sync`` decides
        # a live exchange and a historical one by the same rule (#2320).
        **identity_metadata(source_file),
    }
    if extra_metadata:
        for key, value in extra_metadata.items():
            metadata.setdefault(key, value)
    collection.upsert(ids=[drawer_id], documents=[text], metadatas=[metadata])
    return drawer_id


# File types that might contain conversations
CONVO_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
}

# Directories inside conversation sources that never hold conversations.
# ``tool-results``: Claude Code pages large tool outputs to
# ``<session>/tool-results/*.txt`` inside ``~/.claude/projects/<slug>/``.
# They are raw machine dumps referenced from the transcript JSONL — mining
# them stores megabytes of command output as "memories" (field measurement:
# 12.8k drawers from tool-results files on one palace; a single file
# produced 3.6k). Extends the generic SKIP_DIRS set for the convo scanner
# only — project mining semantics are unchanged.
CONVO_SKIP_DIRS = SKIP_DIRS | {"tool-results"}

MIN_CHUNK_SIZE = 30
CHUNK_SIZE = 800  # chars per drawer — align with miner.py
_LINE_GROUP_SIZE = 25  # lines per fallback group when no paragraph breaks
_LINE_FALLBACK_MIN_NEWLINES = 20  # trigger line-group fallback above this newline count
DRAWER_UPSERT_BATCH_SIZE = 1000
# A mine of at most this many files skips the whole-palace prefetch scan for
# per-file and per-wing lookups (#548). On the postgres palace a per-file
# lookup is a ~24 s sequential scan and the whole scan ~9 min, so past about
# twenty files one pass is cheaper; ten keeps a margin. A drain mine is one.
SCOPED_PREFETCH_MAX_FILES = 10
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB — skip files larger than this.
# Matches miner.py at 500 MB. Long Claude Code sessions, multi-year
# ChatGPT exports, and lifetime Slack dumps routinely exceed 10 MB; the
# cap at that level silently dropped them with `continue`. Per-drawer
# size is bounded by CHUNK_SIZE, but larger source files still produce
# more drawers and therefore more embedding/storage work — and content
# is normalized and loaded fully into memory before chunking, so memory
# use also scales with source size.


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_regular_source_file(filepath: Path, root: Path) -> bool:
    if not _path_within_root(filepath, root):
        return False
    # O_NONBLOCK keeps the S_ISREG verdict below reachable: a blocking open
    # of a FIFO waits in the kernel for a writer, so a named pipe called
    # ``session.jsonl`` would hang this check instead of failing it. See the
    # matching comment in ``miner._read_text_no_follow``, including why the
    # EAGAIN branch re-checks the type and then opens without the flag.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        try:
            fd = os.open(filepath, flags)
        except OSError as exc:
            if exc.errno != errno.EAGAIN or not stat.S_ISREG(os.lstat(filepath).st_mode):
                raise
            fd = os.open(filepath, flags & ~getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        return stat.S_ISREG(st.st_mode) and st.st_size <= MAX_FILE_SIZE
    except OSError:
        return False
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


def _register_file(
    collection,
    source_file: str,
    wing: str,
    agent: str,
    extract_mode: str,
    content_hash: Optional[str] = None,
):
    """Write a sentinel so file_already_mined() returns True for 0-chunk files.

    Without this, files that normalize to nothing or produce zero chunks are
    re-read and re-processed on every mine run because nothing was written to
    ChromaDB on the first pass.

    Stamps source_mtime like every real drawer does, so a file that later
    grows past the min-chunk-size floor (e.g. a short session that gets
    extended) is correctly detected as changed on the next mine instead of
    being skipped forever by this sentinel.

    Also used to register a file recognized as a content-duplicate of an
    already-mined transcript under a different path (see
    ``prefetch_content_hashes``) — stamping it here means the next run skips
    it via the cheap mtime check instead of re-normalizing and re-hashing it.
    """
    try:
        source_mtime = os.path.getmtime(source_file)
    except OSError:
        source_mtime = None
    sentinel_id = make_convo_sentinel_id(source_file, extract_mode)
    meta = {
        "wing": wing,
        "room": "_registry",
        "source_file": source_file,
        "added_by": agent,
        "filed_at": datetime.now().isoformat(),
        "ingest_mode": "registry",
        "extract_mode": extract_mode,
        "normalize_version": NORMALIZE_VERSION,
        "id_recipe": ID_RECIPE,
    }
    if extract_mode == "exchange":
        meta["convo_chunker_version"] = CONVO_CHUNKER_VERSION
    if source_mtime is not None:
        meta["source_mtime"] = source_mtime
    if content_hash is not None:
        meta["content_hash"] = content_hash
    collection.upsert(
        documents=[f"[registry] {source_file}"],
        ids=[sentinel_id],
        metadatas=[meta],
    )


def _iter_source_file_drawers(collection, source_file: str, extract_mode: str, include: list):
    """Yield ``(drawer_id, metadata, document)`` for one source file and
    extraction mode, paging through the backend 1000 rows at a time.

    ``document`` is ``None`` unless ``"documents"`` is in ``include``.

    Legacy conversation drawers did not carry extract_mode; treat those as
    exchange-mode rows so schema rebuilds can still clean them up without
    deleting newer general-mode drawers for the same transcript.
    """
    want_docs = "documents" in include
    offset = 0
    while True:
        batch = collection.get(
            where={"source_file": source_file},
            limit=1000,
            offset=offset,
            include=include,
        )
        batch_ids = batch.get("ids") or []
        metadatas = batch.get("metadatas") or []
        documents = (batch.get("documents") or []) if want_docs else []
        for idx, (drawer_id, meta) in enumerate(zip(batch_ids, metadatas)):
            if _metadata_matches_extract_mode(meta or {}, extract_mode):
                doc = documents[idx] if idx < len(documents) else None
                yield drawer_id, meta or {}, doc
        if not batch_ids:
            break
        offset += len(batch_ids)


def _source_file_delete_ids(collection, source_file: str, extract_mode: str) -> list[str]:
    """Collect drawer IDs for one source file and extraction mode."""
    return [
        drawer_id
        for drawer_id, _meta, _doc in _iter_source_file_drawers(
            collection, source_file, extract_mode, include=["metadatas"]
        )
    ]


def _source_file_stored_documents(
    collection, source_file: str, extract_mode: str
) -> dict[str, Optional[str]]:
    """Map ``drawer_id -> stored document`` for one source file and extraction
    mode — the input to the incremental re-mine's byte-identical skip (#414).

    Registry sentinels (``_reg_*``) are excluded: they are not chunk rows and
    the convo pass never rewrites them by id, so leaving them in the map would
    only get them purged as orphans on every incremental pass.
    """
    stored: dict[str, Optional[str]] = {}
    for drawer_id, meta, doc in _iter_source_file_drawers(
        collection, source_file, extract_mode, include=["metadatas", "documents"]
    ):
        if meta.get("ingest_mode") == "registry":
            continue
        stored[drawer_id] = doc
    return stored


def _chunk_matches_stored(content: str, stored: Optional[str]) -> bool:
    """True when ``content`` is byte-identical to what the palace already
    holds for this drawer id.

    Backends strip NUL bytes at the write boundary (pgvector: postgres text
    cannot hold NUL; chroma: FTS5 corruption guard), so a chunk that carried
    NUL in captured tool output is stored without it. Compare the stripped
    form too, otherwise those chunks would re-embed on every pass forever.
    Any other difference means the chunk genuinely changed and is re-filed.
    """
    if not isinstance(stored, str):
        return False
    if stored == content:
        return True
    return "\x00" in content and stored == content.replace("\x00", "")


# =============================================================================
# CHUNKING — exchange pairs for conversations
# =============================================================================


def chunk_exchanges(
    content: str,
    chunk_size: int = None,
    min_chunk_size: int = None,
) -> list:
    """
    Chunk by exchange pair: one > turn + AI response = one unit.
    Falls back to paragraph chunking if no > markers.

    Optional params override module-level defaults when provided.

    Raises ``ValueError`` if ``chunk_size`` is not a positive integer or
    ``min_chunk_size`` is negative. A non-positive ``chunk_size`` would
    cause ``_chunk_by_exchange`` below to loop forever — ``content[:0]``
    is empty, ``content[0:]`` is the whole string, and the remainder
    never shrinks.
    """
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if min_chunk_size is None:
        min_chunk_size = MIN_CHUNK_SIZE

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
    if min_chunk_size < 0:
        raise ValueError(f"min_chunk_size must be >= 0, got {min_chunk_size}")

    lines = content.split("\n")
    quote_lines = sum(1 for line in lines if line.strip().startswith(">"))

    if quote_lines >= 3:
        return _chunk_by_exchange(lines, chunk_size, min_chunk_size)
    else:
        return _chunk_by_paragraph(content, chunk_size, min_chunk_size)


def _chunk_by_exchange(lines: list, chunk_size: int, min_chunk_size: int) -> list:
    """One user turn (>) + the AI response that follows = one or more chunks.

    Every non-blank line lands in a chunk. A response runs until the next
    user turn — a ``---`` rule inside it is part of the response — and any
    text before the first user turn is filed as its own unit. When a unit
    exceeds chunk_size it is split across consecutive drawers.
    """
    chunks = []
    i = 0
    # Separator between the previous unit and the next one as it stood in the
    # source: the newline ending that unit plus the blank lines trimmed off it.
    # A unit small enough to join the previous drawer is joined with it.
    gap = "\n"

    preamble = []
    while i < len(lines) and not lines[i].strip().startswith(">"):
        preamble.append(lines[i])
        i += 1
    raw_preamble = "\n".join(preamble)
    _emit_bounded(chunks, raw_preamble.strip("\n"), chunk_size, min_chunk_size)
    gap = "\n" * (len(raw_preamble) - len(raw_preamble.rstrip("\n")) + 1)

    while i < len(lines):
        user_turn = lines[i].strip()
        i += 1

        ai_lines = []
        while i < len(lines) and not lines[i].strip().startswith(">"):
            # Preserve the line as-is — blank lines and indentation carry meaning
            # (paragraph breaks, list/code structure) and must survive verbatim.
            ai_lines.append(lines[i])
            i += 1

        # Join on newline (not space) so line structure, blank lines, and
        # indentation reach the drawer unchanged. Trim only trailing blank
        # lines produced by the loop stopping at the next `>` turn.
        raw_response = "\n".join(ai_lines)
        ai_response = raw_response.rstrip("\n")
        content = f"{user_turn}\n{ai_response}" if ai_response else user_turn

        _emit_bounded(chunks, content, chunk_size, min_chunk_size, joiner=gap)
        gap = "\n" * (len(raw_response) - len(ai_response) + 1)

    return chunks


def _emit_bounded(
    chunks: list,
    content: str,
    chunk_size: int,
    min_chunk_size: int,
    joiner: str = "\n",
) -> None:
    """Append ``content`` as one or more drawers, none exceeding ``chunk_size``.

    Nothing but whitespace is ever dropped. A unit whose stripped length is
    at or below ``min_chunk_size`` is too small to be a useful drawer on its
    own, so it is appended to the previous drawer (joined by ``joiner``,
    which callers pass as the separator that stood between the two units in
    the source) when that still fits in ``chunk_size``, and emitted as its
    own drawer otherwise.
    """
    if not content.strip():
        return
    if len(content.strip()) <= min_chunk_size and chunks:
        merged = chunks[-1]["content"] + joiner + content
        if len(merged) <= chunk_size:
            chunks[-1]["content"] = merged
            return
    for piece in _split_bounded(content, chunk_size):
        chunks.append({"content": piece, "chunk_index": len(chunks)})


def _split_bounded(content: str, chunk_size: int) -> list:
    """Split ``content`` into slices of at most ``chunk_size`` characters.

    The slices concatenate back to exactly ``content``. Each cut lands just
    after the last whitespace in the back half of its window, so words are
    not cut in two; a window with no whitespace there is cut at
    ``chunk_size``. Scanning one window at a time keeps this linear.
    """
    pieces = []
    start = 0
    while len(content) - start > chunk_size:
        limit = start + chunk_size
        floor = start + chunk_size // 2
        cut = max(content.rfind(ws, floor, limit) for ws in (" ", "\n", "\t"))
        cut = cut + 1 if cut != -1 else limit
        pieces.append(content[start:cut])
        start = cut
    pieces.append(content[start:])
    return pieces


def _chunk_by_paragraph(content: str, chunk_size: int, min_chunk_size: int) -> list:
    """Fallback: chunk by paragraph breaks."""
    chunks = []
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

    # If no paragraph breaks and long content, chunk by line groups
    if len(paragraphs) <= 1 and content.count("\n") > _LINE_FALLBACK_MIN_NEWLINES:
        lines = content.split("\n")
        for i in range(0, len(lines), _LINE_GROUP_SIZE):
            group = "\n".join(lines[i : i + _LINE_GROUP_SIZE]).strip()
            _emit_bounded(chunks, group, chunk_size, min_chunk_size)
        return chunks

    for para in paragraphs:
        _emit_bounded(chunks, para, chunk_size, min_chunk_size, joiner="\n\n")

    return chunks


# =============================================================================
# ROOM DETECTION — topic-based for conversations
# =============================================================================

# Canonical room taxonomy — emits one of the 7 canonical rooms enforced
# by the mempalace_canonical_rooms FK on the postgres backend. Per the
# 2026-05-14 hybrid-search/taxonomy spec:
#   - 'technical' is dropped: code/data content routes to 'references',
#     bug/error content routes to 'problems'.
#   - 'general' fallback is replaced with 'discoveries' (the spec's
#     catch-all canonical room).
#   - 'sessions' is added for conversation/diary/checkpoint-flavored
#     content so hook-triggered convos miner writes land in the
#     canonical session room.
#   - Per-installation overrides come from ~/.mempalace/config.yaml
#     (room_rules section) — see _load_room_rules.
TOPIC_KEYWORDS = {
    "problems": [
        "problem",
        "issue",
        "broken",
        "failed",
        "crash",
        "stuck",
        "workaround",
        "fix",
        "solved",
        "resolved",
        "bug",
        "error",
        "debug",
        "exception",
        "traceback",
    ],
    "architecture": [
        "architecture",
        "design",
        "pattern",
        "structure",
        "schema",
        "interface",
        "module",
        "component",
        "service",
        "layer",
    ],
    "planning": [
        "plan",
        "roadmap",
        "milestone",
        "deadline",
        "priority",
        "sprint",
        "backlog",
        "scope",
        "requirement",
        "spec",
        "todo",
    ],
    "decisions": [
        "decided",
        "chose",
        "picked",
        "switched",
        "migrated",
        "replaced",
        "trade-off",
        "alternative",
        "option",
        "approach",
        "selected",
    ],
    "sessions": [
        "session",
        "conversation",
        "chat",
        "diary",
        "checkpoint",
        "convo",
    ],
    "references": [
        "code",
        "python",
        "function",
        "api",
        "database",
        "server",
        "deploy",
        "git",
        "test",
        "refactor",
        "config",
        "documentation",
    ],
    "discoveries": [
        "discovered",
        "found",
        "learned",
        "insight",
        "finding",
        "note",
        "observed",
    ],
}

# Fallback when no keywords match. 'discoveries' is the spec's canonical
# catch-all — same role 'general' served in the legacy pre-canonical
# vocabulary, but FK-enforced and meaningful for the room taxonomy.
DEFAULT_ROOM = "discoveries"


def _load_room_rules():
    """Per-installation overrides from ~/.mempalace/config.yaml if present.

    Falls back to the baked-in TOPIC_KEYWORDS. The config file lets users
    add new canonical rooms (also registered via `mempalace rooms add`)
    and supply keyword rules without editing source.
    """
    try:
        import yaml
        from pathlib import Path

        cfg_path = Path.home() / ".mempalace" / "config.yaml"
        if not cfg_path.exists():
            return TOPIC_KEYWORDS
        with cfg_path.open() as f:
            cfg = yaml.safe_load(f) or {}
        overrides = cfg.get("room_rules")
        if isinstance(overrides, dict) and overrides:
            return overrides
    except Exception:
        pass
    return TOPIC_KEYWORDS


def detect_convo_room(content: str) -> str:
    """Score conversation content against the canonical room keyword rules.

    Returns one of the canonical 7 rooms (or whatever the per-installation
    config.yaml has registered). FK-safe: never returns a non-canonical
    room provided the config and DB lookup are in sync — which they are
    by default since both ship with the same seed set.
    """
    rules = _load_room_rules()
    content_lower = content[:3000].lower()
    scores = {}
    for room, keywords in rules.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)
    return DEFAULT_ROOM


# =============================================================================
# PALACE OPERATIONS
# =============================================================================


# =============================================================================
# SCAN FOR CONVERSATION FILES
# =============================================================================


def scan_convos(convo_dir: str, include_subagents: bool = False) -> list:
    """Find all potential conversation files.

    Skips symlinks and oversized files. Each skipped symlink is logged to
    ``sys.stderr`` with a ``  SKIP: <relative-path> (symlink)`` line so the
    caller can tell why an apparent conversation directory yielded no files.

    By default, directories named ``subagents`` are skipped: Claude Code
    records Explore/Plan/Grep subagent transcripts there, and on typical
    workspaces they outnumber main session files by one to two orders of
    magnitude. Pass ``include_subagents=True`` to mine them anyway.

    The match is case-insensitive on the directory name only (``subagents``
    or ``Subagents``), so directories like ``mysubagents`` or
    ``subagentsbackup`` are not affected.
    """
    # A direct conversation file is a valid source. For a file, feed only
    # its basename through the existing directory validation loop.
    requested_path = Path(convo_dir).expanduser()
    single_file = requested_path.is_file()
    convo_path = (requested_path.parent if single_file else requested_path).resolve()
    scan_entries = (
        [(str(convo_path), [], [requested_path.name])] if single_file else os.walk(convo_path)
    )
    files = []
    for root, dirs, filenames in scan_entries:
        dirs[:] = [
            d
            for d in dirs
            if d not in CONVO_SKIP_DIRS and (include_subagents or d.lower() != "subagents")
        ]
        for filename in filenames:
            if filename.endswith(".meta.json"):
                continue
            filepath = Path(root) / filename
            if filepath.suffix.lower() in CONVO_EXTENSIONS:
                # Skip symlinks and oversized files
                if filepath.is_symlink():
                    rel = filepath.relative_to(convo_path).as_posix()
                    try:
                        print(f"  SKIP: {rel} (symlink)", file=sys.stderr)
                    except OSError:
                        pass
                    continue
                # Skip files exceeding size limit, or those whose stat() raises
                # (permission denied, racing delete, broken symlink that
                # survived the earlier is_symlink check). Both branches log
                # to stderr to match the SKIP: (symlink) line above; silent
                # drops at this gate were the original #923 complaint.
                try:
                    file_stat = filepath.stat()
                    # Drop non-regular entries (FIFO, socket, device node)
                    # before any reader touches them — see the matching
                    # gate in ``miner.scan_project``.
                    if not stat.S_ISREG(file_stat.st_mode):
                        print(
                            f"  SKIP: {filepath.name} (not a regular file)",
                            file=sys.stderr,
                        )
                        continue
                    file_size = file_stat.st_size
                    if file_size > MAX_FILE_SIZE:
                        print(
                            f"  SKIP: {filepath.name} ({file_size / (1024 * 1024):.1f} MB)"
                            f" exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit",
                            file=sys.stderr,
                        )
                        continue
                except OSError as exc:
                    # Prefer ``exc.strerror`` so the path isn't duplicated in
                    # the output (see the matching comment in
                    # ``miner.scan_project``).
                    print(
                        f"  SKIP: {filepath.name} (stat error: {exc.strerror or exc})",
                        file=sys.stderr,
                    )
                    continue
                if not _is_regular_source_file(filepath, convo_path):
                    continue
                files.append(filepath)
    return files


# =============================================================================
# MINE CONVERSATIONS
# =============================================================================


def _extract_authored_at(filepath):
    """Most-recent message timestamp in a transcript, used as the drawer's authored date.

    Both Claude Code and Codex JSONL transcripts carry a top-level ISO-8601
    ``timestamp`` on each line. We take the max so ``authored_at`` reflects when the
    content was actually written, independent of when it was mined (``filed_at``).
    This restores chronology: a session from days ago keeps its real date even when
    re-mined today, instead of every drawer collapsing to ingest time. Returns None
    for formats without per-line timestamps (e.g. plain ``.md``).
    """
    path = Path(filepath)
    if path.suffix != ".jsonl":
        return None
    latest = None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get("timestamp")
                except (ValueError, TypeError, AttributeError):
                    continue
                # ISO-8601 timestamps are strings; guard against a non-string
                # ``timestamp`` so a malformed line can't raise TypeError on compare.
                if isinstance(ts, str) and (latest is None or ts > latest):
                    latest = ts
    except OSError:
        return None
    return latest


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    authored_at=None,
    content_hash=None,
    source_dir_ino=None,
):
    """Lock the source file, reconcile it against what the palace already
    holds, and upsert only the chunks that are new or changed.

    Combines the per-file serialization that prevents concurrent agents from
    duplicating work (via mine_lock) with the rebuild contract that fires on
    either a normalize-version bump OR a changed/grown source file (mtime
    differs from what's stored) -- transcripts are not assumed immutable,
    since a Claude Code session keeps appending to its own file while
    active and /compact or /clear can rewrite one in place.

    Incremental re-mine (#414). The stored drawers are the watermark: convo
    drawer ids are positional (``source_file|extract_mode|chunk_index``) and
    exchange chunking walks the transcript forward, so appending to a live
    session leaves every earlier chunk's id AND content untouched. Per pass:

      1. prefetch ``id -> document`` for this source file,
      2. plan this pass's ids; delete only stored ids the pass will not
         rewrite (orphans: a shrunk/rewritten file, a chunk routed to a
         different room),
      3. refresh ``source_mtime`` / ``chunk_total`` on byte-identical drawers
         with a metadata-only update — no embedding is computed or moved,
      4. embed + upsert only the chunks whose content differs or is new.

    Step 3 runs before step 4 on purpose: if the pass dies mid-way the
    surviving rows all carry the new mtime but fewer than ``chunk_total``
    rows, so ``file_already_mined`` / ``prefetch_mined_set`` treat the file
    as incomplete and the next pass picks it up again (#2183). Nothing
    stored is ever deleted to make room for a rewrite; a crash leaves the
    existing drawers in place.

    Fail-safe: if the prefetch fails, fall back to the pre-#414 full purge +
    rebuild; if the metadata refresh fails, the affected chunks are simply
    re-filed in full (wasteful, never lossy).

    Returns (drawers_added, room_counts_delta, skipped).
    """
    room_counts_delta: dict = defaultdict(int)
    drawers_added = 0
    with mine_lock(source_file):
        # Re-check after lock — another agent may have just finished this file
        # at the current schema/mtime. A stale hit here returns False, so we
        # still fall through to the reconcile path below.
        if file_already_mined(collection, source_file, check_mtime=True, extract_mode=extract_mode):
            return 0, room_counts_delta, True

        # Plan the pass: one (chunk, room, drawer_id) per chunk. Ids are
        # deterministic so this is pure hashing — no backend calls yet.
        planned: list = []
        for chunk in chunks:
            chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
            if extract_mode == "general":
                room_counts_delta[chunk_room] += 1
            drawer_id = make_convo_drawer_id(
                wing, chunk_room, source_file, extract_mode, chunk["chunk_index"]
            )
            planned.append((chunk, chunk_room, drawer_id))
        planned_ids = {drawer_id for _chunk, _room, drawer_id in planned}

        # Prefetch what the palace already holds for this file. On failure
        # fall back to the pre-#414 behaviour: purge everything, rebuild all.
        stored: Optional[dict] = None
        try:
            stored = _source_file_stored_documents(collection, source_file, extract_mode)
        except Exception:
            logger.debug(
                "Stored-document prefetch failed for %s; falling back to full rebuild",
                source_file,
                exc_info=True,
            )

        # Purge stale drawers: with a prefetch, only the ids this pass will
        # not rewrite; without one, every drawer of the file (legacy path).
        # Also fires on a normalize-schema bump, where file_already_mined()
        # returned False for pre-v2 drawers.
        #
        # A failed purge must abort this file's mine attempt rather than
        # fall through to upsert: proceeding on top of an unpurged (or
        # partially purged) set produces duplicate/stale drawers under
        # mixed schema versions, with no operator-visible signal beyond a
        # debug log (#105 — convo_miner's own instance of the same swallow
        # already fixed for miner.py at #23). Returning here leaves the old
        # drawers' stored mtime untouched, so the next mine still sees a
        # mismatch and retries.
        try:
            if stored is None:
                delete_ids = _source_file_delete_ids(collection, source_file, extract_mode)
                stored = {}
            else:
                delete_ids = [drawer_id for drawer_id in stored if drawer_id not in planned_ids]
            if delete_ids:
                collection.delete(ids=delete_ids)
        except Exception as exc:
            print(
                f"  ! [skip] stale-drawer purge failed for {source_file!r} "
                f"({exc!r}); leaving existing drawers untouched, will retry "
                f"on the next mine",
                file=sys.stderr,
            )
            logger.debug("Stale-drawer purge failed for %s", source_file, exc_info=True)
            return 0, room_counts_delta, True

        # Split the plan: byte-identical chunks get a metadata refresh only;
        # everything else is embedded and upserted.
        unchanged: list = []
        changed: list = []
        for item in planned:
            chunk, _chunk_room, drawer_id = item
            if _chunk_matches_stored(chunk["content"], stored.get(drawer_id)):
                unchanged.append(item)
            else:
                changed.append(item)

        # Keep one filed_at per source file so all transcript drawers written
        # by this pass share an ingest timestamp.
        #
        # Every drawer of this pass carries ``chunk_total`` so
        # ``file_already_mined`` / ``prefetch_mined_set`` can tell a complete
        # multi-batch mine from one that crashed mid-file (#2183). Without it
        # a stable mtime + any surviving drawer permanently skips the file
        # and the missing exchanges never come back.
        filed_at = datetime.now().isoformat()
        try:
            source_mtime = os.path.getmtime(source_file)
        except OSError:
            source_mtime = None
        chunk_total = len(chunks)

        # Metadata-only refresh of the unchanged drawers. ``update`` with no
        # documents never touches the embedding function; backends without a
        # native update merge metadata over the stored row. ``content_hash``
        # on chunk 0 tracks the whole normalized file, so it is refreshed
        # too — ``prefetch_content_hashes`` must see the same value a full
        # mine would have written. filed_at / entities / hall / novelty_tag
        # are left as they were: the drawer's content did not change.
        use_manifest, to_refresh = _unchanged_to_refresh(
            collection, unchanged, extract_mode, content_hash
        )
        if to_refresh:
            refreshed = _refresh_unchanged_drawers(
                collection,
                to_refresh,
                source_mtime=source_mtime,
                chunk_total=chunk_total,
                authored_at=authored_at,
                content_hash=content_hash,
            )
            if not refreshed:
                # Fall back to re-filing them in full — same result as a
                # pre-#414 rebuild for these rows, just slower.
                changed = list(planned)
                unchanged = []

        from .novelty_wiring import (
            DEFAULT_WINDOW_SIZE,
            compute_novelty_tag,
            fetch_recent_window,
        )

        # Pre-fetch novelty windows per room so batch processing avoids
        # N+1 DB queries — one fetch per distinct room instead of per chunk.
        window_cache: dict[str, list[str]] = {}

        # Batch chunks into bounded upserts so large transcripts keep most of
        # the embedding speedup without one huge Chroma/SQLite request.
        try:
            for batch_start in range(0, len(changed), DRAWER_UPSERT_BATCH_SIZE):
                batch_docs: list = []
                batch_ids: list = []
                batch_metas: list = []
                for chunk, chunk_room, drawer_id in changed[
                    batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE
                ]:
                    batch_docs.append(chunk["content"])
                    batch_ids.append(drawer_id)
                    meta = {
                        "wing": wing,
                        "room": chunk_room,
                        "hall": _detect_hall_cached(chunk["content"]),
                        "source_file": source_file,
                        "chunk_index": chunk["chunk_index"],
                        "added_by": agent,
                        "filed_at": filed_at,
                        "entities": entities_metadata(chunk["content"]),
                        "authored_at": authored_at if authored_at is not None else filed_at,
                        "ingest_mode": "convos",
                        "extract_mode": extract_mode,
                        "normalize_version": NORMALIZE_VERSION,
                        "id_recipe": ID_RECIPE,
                        "chunk_total": chunk_total,
                    }
                    if extract_mode == "exchange":
                        meta["convo_chunker_version"] = CONVO_CHUNKER_VERSION
                    if source_mtime is not None:
                        meta["source_mtime"] = source_mtime
                    if source_dir_ino:
                        # Which directory this transcript was read from, so
                        # ``sync`` can tell a neighbour in the same directory
                        # from one on a volume mounted there since (#2320).
                        meta["source_dir_ino"] = source_dir_ino
                    # Stamp content_hash only on chunk 0 so multi-conversation
                    # privacy-export hashes are not O(N²)-duplicated across every
                    # chunk row. ``prefetch_content_hashes`` still finds them —
                    # it scans all drawers and splits comma-joined hash fields.
                    if content_hash is not None and chunk.get("chunk_index", 0) == 0:
                        meta["content_hash"] = content_hash
                    if chunk_room not in window_cache:
                        window_cache[chunk_room] = fetch_recent_window(
                            collection, wing, chunk_room, DEFAULT_WINDOW_SIZE
                        )
                    novelty_tag = compute_novelty_tag(
                        collection,
                        wing,
                        chunk_room,
                        chunk["content"],
                        recent=window_cache[chunk_room],
                    )
                    if novelty_tag is not None:
                        meta["novelty_tag"] = novelty_tag
                    batch_metas.append(meta)
                assert_no_collisions(list(zip(batch_ids, batch_metas)), collection)
                try:
                    collection.upsert(
                        documents=batch_docs,
                        ids=batch_ids,
                        metadatas=batch_metas,
                    )
                    drawers_added += len(batch_docs)
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        raise
        except Exception:
            # A successful earlier batch has the source's current mtime and
            # chunk_total. Remove the rows this pass wrote so the next run
            # cannot treat the incomplete set as fully filed (#2183 / #2122).
            # The unchanged drawers stay. Without a manifest they already carry the
            # new mtime and chunk_total, and with the new rows gone the group is short
            # of chunk_total. With one, the manifest row was not written. Either way
            # the file is re-mined next pass.
            try:
                delete_ids = [drawer_id for _chunk, _room, drawer_id in changed]
                if delete_ids:
                    collection.delete(ids=delete_ids)
            except Exception:
                logger.warning(
                    "Failed to clean partial convo drawers after upsert error for %s",
                    source_file,
                    exc_info=True,
                )
            raise
        _write_source_manifest(
            use_manifest, collection, source_file, extract_mode, source_mtime, chunk_total
        )
        if unchanged:
            print(
                f"    = {len(unchanged)} unchanged drawer(s) kept, "
                f"{len(changed)} embedded ({Path(source_file).name[:50]})"
            )
    return drawers_added, room_counts_delta, False


def _unchanged_to_refresh(collection, unchanged: list, extract_mode, content_hash):
    """``(use_manifest, items)``: whether this pass records completeness in the source
    manifest, and which unchanged drawers still need a metadata refresh.

    With a manifest (postgres, #575) completeness lives in one row per source, so the
    unchanged drawers are left alone: re-stamping them was a non-HOT UPDATE per chunk
    that wrote every drawer index (familiar, 2026-10-21: 33k rows per pass of one
    transcript). Only chunk 0 is refreshed, because it carries the file-level
    content_hash that prefetch_content_hashes reads. Without a manifest, every
    unchanged drawer is refreshed, as before.
    """
    use_manifest = (
        extract_mode is not None and getattr(collection, "supports_source_manifest", False) is True
    )
    if not use_manifest:
        return False, unchanged
    if content_hash is None:
        return True, []
    return True, [item for item in unchanged if item[0].get("chunk_index", 0) == 0]


def _write_source_manifest(
    use_manifest, collection, source_file, extract_mode, source_mtime, chunk_total
):
    """Record this pass as complete (when the pass uses the manifest). Written last, after
    every upsert, so it is the pass's commit point; a failure only costs a redundant
    re-mine next time."""
    if not use_manifest:
        return
    try:
        collection.manifest_put(
            source_file,
            extract_mode,
            source_mtime,
            chunk_total,
            NORMALIZE_VERSION,
            convo_chunker_version=CONVO_CHUNKER_VERSION,
        )
    except Exception:
        logger.warning(
            "Source manifest write failed for %s; it will be re-mined", source_file, exc_info=True
        )


def _refresh_unchanged_drawers(
    collection,
    unchanged: list,
    *,
    source_mtime: Optional[float],
    chunk_total: int,
    authored_at: Optional[str],
    content_hash: Optional[str],
) -> bool:
    """Metadata-only refresh of drawers whose content matched the palace.

    Stamps the pass's ``source_mtime`` / ``chunk_total`` (and ``authored_at``
    / chunk-0 ``content_hash`` when known) so the completeness bookkeeping in
    ``prefetch_mined_set`` / ``file_already_mined`` sees one coherent group.
    No document is sent, so the embedding wrapper computes nothing.

    Returns True when every batch was applied, False on any failure — the
    caller then re-files those chunks in full instead.
    """
    for batch_start in range(0, len(unchanged), DRAWER_UPSERT_BATCH_SIZE):
        batch = unchanged[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]
        ids: list = []
        metas: list = []
        for chunk, _room, drawer_id in batch:
            meta: dict = {"chunk_total": chunk_total}
            if source_mtime is not None:
                meta["source_mtime"] = source_mtime
            if authored_at is not None:
                meta["authored_at"] = authored_at
            if content_hash is not None and chunk.get("chunk_index", 0) == 0:
                meta["content_hash"] = content_hash
            ids.append(drawer_id)
            metas.append(meta)
        try:
            collection.update(ids=ids, metadatas=metas)
        except Exception:
            logger.debug(
                "Metadata refresh of %d unchanged convo drawers failed; re-filing in full",
                len(ids),
                exc_info=True,
            )
            return False
    return True


def mine_sessions(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
):
    """Mine a directory of session/transcript files into per-session
    manifest drawers — ONE drawer per session, addressable by session_id.

    Restores the addressable-session-anchor semantic that the hook's
    legacy ``mempalace_diary_write`` call used to provide. The
    unified-write-path refactor moved hook checkpointing to ``mine_convos``
    which produces
    N chunked drawers; this complements it with a single manifest
    drawer per session that callers can grab as a navigation anchor
    ("did session X exist? show me a summary").

    Content of each manifest drawer (no LLM call required — pure
    structural extraction):

      Session manifest
      ─────────────────
      session_id:        <UUID from filename stem>
      started_at:        <first message timestamp>
      ended_at:          <last message timestamp>
      exchanges:         <count of user messages>
      first_user_msg:    <first 400 chars of first user message>
      last_user_msg:     <first 400 chars of last user message>
      cwd:               <cwd field if present>

    Drawer ID is stable: ``drawer_<wing>_sessions_<sha256(session_id)>``
    so re-running on the same session overwrites the existing manifest
    rather than duplicating.

    Wing defaults to the convo_dir basename per the spec's normalization
    rules. Room is hard-coded ``sessions`` (canonical).
    """
    import hashlib
    import json as _json

    from .config import normalize_wing_name

    if not wing:
        wing = normalize_wing_name(Path(convo_dir).expanduser().resolve().name)

    files = scan_convos(convo_dir)
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Sessions (manifest mode)")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    print("  Room:    sessions  (canonical, FK-enforced)")
    if dry_run:
        print("  DRY RUN — no writes")
    print()

    # Open palace via the configured backend (chroma or postgres). Same
    # path as mine_convos so we don't need a separate write surface.
    if not dry_run:
        from .mcp_server import _get_collection

        collection = _get_collection(create=True)

    written = 0
    skipped = 0
    for filepath in files:
        try:
            with filepath.open("r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            skipped += 1
            continue

        # Parse JSONL transcript (Claude Code shape). Tolerate non-JSONL
        # files by falling through to "single-blob" manifest.
        first_user_msg = ""
        last_user_msg = ""
        first_ts = ""
        last_ts = ""
        user_count = 0
        cwd = ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("cwd") and not cwd:
                cwd = str(entry["cwd"])
            ts = entry.get("timestamp") or ""
            msg = entry.get("message", {})
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Newer Claude transcripts use content blocks; concat the text blocks.
                    text = " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = str(content)
                text = text.strip()
                if not text or "<command-message>" in text:
                    continue
                user_count += 1
                if not first_user_msg:
                    first_user_msg = text[:400]
                    first_ts = ts
                last_user_msg = text[:400]
                last_ts = ts

        if user_count == 0:
            skipped += 1
            continue

        session_id = filepath.stem  # UUID from filename
        manifest_text = (
            f"Session manifest\n"
            f"─────────────────\n"
            f"session_id:     {session_id}\n"
            f"started_at:     {first_ts or '(unknown)'}\n"
            f"ended_at:       {last_ts or '(unknown)'}\n"
            f"exchanges:      {user_count}\n"
            f"cwd:            {cwd or '(unknown)'}\n"
            f"first_user_msg: {first_user_msg or '(empty)'}\n"
            f"last_user_msg:  {last_user_msg or '(empty)'}\n"
        )

        # Stable ID — re-running upserts rather than duplicates.
        sid_hash = hashlib.sha256(session_id.encode()).hexdigest()[:24]
        drawer_id = f"drawer_{wing}_sessions_{sid_hash}"
        metadata = {
            "session_id": session_id,
            "started_at": first_ts,
            "ended_at": last_ts,
            "exchanges": user_count,
            "cwd": cwd,
            "source_file": str(filepath),
            "ingest_mode": "session",
            "agent": agent,
            "wing": wing,
            "room": "sessions",
            "filed_at": first_ts or last_ts,
        }

        if dry_run:
            print(f"  [DRY RUN] {filepath.name} → {drawer_id} ({user_count} exchanges)")
            written += 1
            continue

        try:
            collection.upsert(
                ids=[drawer_id],
                documents=[manifest_text],
                metadatas=[metadata],
            )
            written += 1
        except Exception as e:
            print(f"  WARN failed to write {filepath.name}: {e}")
            skipped += 1

    print(f"\nWrote {written} session manifest drawers; skipped {skipped}.")


def _is_ai_tool_path(path: Path) -> bool:
    """Return True when `path` lives inside a known AI-tool storage dir.

    Detected paths (exact-segment match — substrings like `.gemini-backup`
    or `.codex-archive` do NOT match):
      - any segment ``.codex`` (Codex CLI sessions / archives)
      - any segment ``.gemini`` (Gemini CLI sessions under ~/.gemini/tmp/...)
      - the consecutive segment pair ``.claude/projects`` (Claude Code).
        ``.claude`` alone is NOT matched — that is the settings/config dir,
        not a conversation source.

    Used by ``_resolve_wing`` to default the destination wing to
    ``wing_api`` when the user hasn't passed an explicit ``--wing``.
    """
    try:
        parts = path.resolve().parts
    except (OSError, RuntimeError):
        return False

    if ".codex" in parts:
        return True
    if ".gemini" in parts:
        return True
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "projects":
            return True
    return False


def _split_new_and_duplicate_conversations(
    conversations: list,
    wing: str,
    source_file: str,
    mined_content_hashes: dict,
) -> tuple:
    """Hash each conversation and split them into (new, duplicate) lists.

    A conversation is a duplicate when its hash is already registered under
    a *different* source_file in the same wing — mining the same transcript
    into a second wing is a deliberate re-file, not a repeat, so the lookup
    is scoped to (wing, hash). Returns ([(hash, text), ...] new, [(hash,
    dup_source_file), ...] duplicates).
    """
    new_items = []
    duplicates = []
    for conversation in conversations:
        content_hash = hashlib.sha256(conversation.strip().encode("utf-8")).hexdigest()
        dup_source = mined_content_hashes.get((wing, content_hash))
        if dup_source is None or dup_source == source_file:
            new_items.append((content_hash, conversation))
        else:
            duplicates.append((content_hash, dup_source))
    return new_items, duplicates


def _is_unchanged_since_last_mine(source_file: str, mined_mtimes: dict) -> bool:
    """True iff source_file was mined at the current schema AND its on-disk
    mtime still matches what was stored -- the mtime-aware replacement for
    "we've seen this source_file before" (transcripts are not immutable).

    False (re-mine) whenever the file isn't in mined_mtimes at all, its
    stored mtime is None (never recorded -- pre-mtime-tracking drawer, or
    getmtime failed when it was written), or getmtime fails right now
    (treat as changed rather than silently trusting stale data).
    """
    if source_file not in mined_mtimes:
        return False
    stored_mtime = mined_mtimes[source_file]
    if stored_mtime is None:
        return False
    try:
        current_mtime = os.path.getmtime(source_file)
    except OSError:
        return False
    return abs(stored_mtime - current_mtime) < 0.001


def _resolve_wing(convo_path: Path, wing: Optional[str]) -> str:
    """Determine the destination wing for ``mine_convos``.

    Precedence (first match wins):

      1. Explicit ``wing`` argument from the user — always wins, even on
         an AI-tool path. Empty string is treated as "no wing".
      2. AI-tool path detection — defaults to ``wing_api`` so Claude
         Code / Codex / Gemini conversations group under a single wing
         dedicated to API-sourced content.
      3. Basename fallback — sanitized via ``config.normalize_wing_name``
         (lowercase, spaces/hyphens collapsed to underscores). Shared
         single source of truth with ``cmd_init``,
         ``room_detector_local``, and ``miner.load_config`` so all
         wing-slug producers stay in sync (per #1194 consolidation).
    """
    from .config import normalize_wing_name

    if wing:
        return wing
    if _is_ai_tool_path(convo_path):
        return "wing_api"
    return normalize_wing_name(convo_path.name)


def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
    include_subagents: bool = False,
):
    """Mine a directory of conversation files into the palace.

    extract_mode:
        "exchange" — default exchange-pair chunking (Q+A = one unit)
        "general"  — general extractor: decisions, preferences, milestones, problems, emotions
    include_subagents:
        False (default) — skip Claude Code ``subagents/`` directories
        True            — also mine subagent transcripts

    The real work is in :func:`_mine_convos_impl`; this wrapper holds the
    per-palace flock around it so two concurrent ``mempalace mine --mode
    convos`` invocations against the same palace can't pile up. This
    mirrors the pattern in :func:`mempalace.miner.mine`. The lock is
    non-blocking: ``MineAlreadyRunning`` propagates to the CLI (which
    renders a holder-aware message and exits non-zero) or to in-process
    callers that expect to coexist with another writer.

    Dry-run skips the lock — it never writes to the palace and so cannot
    corrupt anything, and skipping the lock lets dry-run probes coexist
    with a live mine.

    Chunking parameters (chunk_size, min_chunk_size) are read from
    MempalaceConfig inside :func:`_mine_convos_impl` so `config.json`
    governs both this path and the project-file miner in `miner.py`.
    """
    if dry_run:
        return _mine_convos_impl(
            convo_dir,
            palace_path,
            wing=wing,
            agent=agent,
            limit=limit,
            dry_run=dry_run,
            extract_mode=extract_mode,
            include_subagents=include_subagents,
        )

    with mine_palace_lock(palace_path):
        return _mine_convos_impl(
            convo_dir,
            palace_path,
            wing=wing,
            agent=agent,
            limit=limit,
            dry_run=dry_run,
            extract_mode=extract_mode,
            include_subagents=include_subagents,
        )


def _compute_hallways_for_wing_safe(wing, collection, drawers_filed, config=None):
    """Auto-populate the associative graph from the entities just mined.

    Best-effort: hallway computation must never fail an otherwise-good mine, and is
    skipped when nothing new was filed.
    """
    if drawers_filed <= 0:
        return
    try:
        from .hallways import compute_hallways_for_wing

        compute_hallways_for_wing(wing, col=collection, config=config)
    except Exception as exc:
        print(f"  (hallways skipped: {exc})")


def _normalize_convo_conversations(
    filepath: Path,
    source_file: str,
    collection,
    wing: str,
    agent: str,
    extract_mode: str,
    dry_run: bool,
    verbatim: bool = False,
) -> Optional[list]:
    """Normalize a transcript file into its individual conversations,
    registering it as filed when there's nothing worth mining. Returns None
    when the caller should skip the file (normalize failed, or the normalized
    content is only whitespace). A short transcript is still mined: the
    chunker keeps text below the min chunk size rather than dropping it.

    Kept as separate conversations rather than joined into one string so
    dedup can hash and skip per conversation — a Claude.ai privacy export
    bundles every conversation into a single file, and hashing the joined
    bundle means one new conversation added to a re-export changes the
    whole-file hash and hides the conversations that didn't change.
    """
    try:
        conversations = [c for c in normalize_conversations(str(filepath), verbatim=verbatim) if c]
    except UnparsedCodexTranscriptError as exc:
        logger.warning("Skipping %s: %s; source remains eligible for retry", filepath, exc)
        return None
    except (OSError, ValueError):
        if not dry_run:
            _register_file(collection, source_file, wing, agent, extract_mode)
        return None

    if not any(c.strip() for c in conversations):
        if not dry_run:
            _register_file(collection, source_file, wing, agent, extract_mode)
        return None

    return conversations


def _open_convo_collection(
    palace_path: str,
    *,
    dry_run: bool,
):
    """Open the conversation collection without creating it during dry-run."""
    if not dry_run:
        return get_collection(palace_path)

    try:
        return get_collection(
            palace_path,
            create=False,
            read_only=True,
        )
    except PalaceNotFoundError:
        # A missing palace or uninitialized collection represents empty
        # prior state to a dry-run. Do not create either one.
        return None


def _mine_convos_impl(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
    include_subagents: bool = False,
):
    from .config import MempalaceConfig

    palace_config = MempalaceConfig(palace_path=palace_path)
    cfg_chunk_size = palace_config.chunk_size
    # min_chunk_size explicitly. min_chunk_size_explicit returns the
    # validated value or None — None keeps convo's lower 30-char floor
    # (more permissive than the 50-char project default, so short
    # exchanges aren't dropped). Using the validated accessor (not raw
    # _file_config) means a garbage/negative/bool config value can't
    # TypeError the length gate below or ValueError out of chunk_exchanges
    # and abort convo ingest. (Upstream #1024 review fix.)
    explicit_min = palace_config.min_chunk_size_explicit
    cfg_min_chunk_size = explicit_min if explicit_min is not None else MIN_CHUNK_SIZE
    # Fork-only: hook_verbatim_mode controls whether convo hook writes raw
    # exchanges (1) or extracted memories (0). Preserved across the
    # upstream-#1024 tightening above.
    cfg_verbatim = palace_config.hook_verbatim_mode

    convo_path = Path(convo_dir).expanduser().resolve()
    wing = _resolve_wing(convo_path, wing)

    files = scan_convos(convo_dir, include_subagents=include_subagents)

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine -- Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    limit_suffix = f" (limit: {limit} new)" if limit > 0 else ""
    print(f"  Files:   {len(files)}{limit_suffix}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN -- nothing will be filed")
    print(f"{'-' * 55}\n")

    report_phase("open", files_total=len(files))
    collection = _open_convo_collection(
        palace_path,
        dry_run=dry_run,
    )

    # Bulk pre-fetch already-mined source_file -> stored mtime in one
    # paginated pass instead of `len(files)` separate WHERE-source_file
    # queries. On a 150k-drawer palace each per-file query costs ~2s, so a
    # 2000-file sweep used to spend >1h just deciding to skip.
    # prefetch_mined_set() does the same decisions in a single scan; loop
    # body becomes an O(1) dict lookup + a cheap local mtime comparison.
    #
    # A small target (a drain mine is one transcript) asks the index about
    # its own files and wing instead: the whole-palace scan costs the same
    # for one file as for two thousand, and on a 950K-drawer palace a
    # one-file mine sat in it for 5-10 minutes (#548).
    scoped = len(files) <= SCOPED_PREFETCH_MAX_FILES
    report_phase("prefetch", files_total=len(files), scoped=scoped)
    mined_mtimes: dict = (
        prefetch_mined_set(
            collection,
            extract_mode=extract_mode,
            source_files=[str(f) for f in files] if scoped else None,
        )
        if collection is not None
        else {}
    )
    # content_hash -> source_file for transcripts already filed. Repeated
    # exports from Claude/ChatGPT commonly land under a new filename each
    # run even when the conversation itself is unchanged, so the
    # source_file-keyed skip above ("mined_mtimes") never recognizes them —
    # this catches the same conversation reappearing at a new path. Not
    # scoped to candidate_source_files: see prefetch_content_hashes's
    # docstring for why that would silently miss bundle rows.
    mined_content_hashes: dict = (
        prefetch_content_hashes(
            collection, extract_mode=extract_mode, wing=wing if scoped else None
        )
        if collection is not None
        else {}
    )

    total_drawers = 0
    files_mined = 0
    files_skipped = 0
    files_processed = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        mine_yield_point()
        files_processed = i
        source_file = str(filepath)

        # Skip only if already filed at the current NORMALIZE_VERSION AND
        # unchanged on disk since. Transcripts are NOT assumed immutable:
        # a Claude Code session keeps appending to the same file while
        # active, and /compact or /clear can rewrite one in place -- so
        # "we've seen this source_file before" alone is not sufficient.
        # Falling through re-mines: _file_chunks_locked purges this
        # source_file's stale drawers before inserting fresh ones, so this
        # never leaves duplicates behind.
        if _is_unchanged_since_last_mine(source_file, mined_mtimes):
            files_skipped += 1
            continue

        if not _is_regular_source_file(filepath, Path(convo_dir).expanduser().resolve()):
            files_skipped += 1
            continue

        progress = {"file": i, "files_total": len(files), "drawers_filed": total_drawers}
        report_phase("normalize", **progress)
        conversations = _normalize_convo_conversations(
            filepath,
            source_file,
            collection,
            wing,
            agent,
            extract_mode,
            dry_run,
            verbatim=cfg_verbatim,
        )
        if conversations is None:
            continue

        # Hash and dedup per conversation, not per file: a Claude/ChatGPT
        # privacy export bundles every conversation into one file, so a
        # re-export that adds one new conversation changes the whole-file
        # hash and would hide the conversations that didn't change if we
        # hashed the joined bundle. Conversations whose hash is already
        # filed under a different source_file in this wing are dropped;
        # the rest are re-joined and mined as usual.
        new_items, duplicates = _split_new_and_duplicate_conversations(
            conversations, wing, source_file, mined_content_hashes
        )
        if not new_items:
            if not dry_run:
                _register_file(collection, source_file, wing, agent, extract_mode)
            dup_source = duplicates[0][1]
            print(
                f"  = [{i:4}/{len(files)}] {filepath.name[:50]:50} "
                f"duplicate of {Path(dup_source).name}"
            )
            files_skipped += 1
            continue

        content = "\n\n".join(text for _, text in new_items)
        content_hash = ",".join(h for h, _ in new_items)

        # Chunk — either exchange pairs or general extraction
        report_phase("chunk", **progress)
        if extract_mode == "general":
            from .general_extractor import extract_memories

            chunks = extract_memories(content, chunk_size=cfg_chunk_size)
            # Each chunk already has memory_type; use it as the room name
        else:
            chunks = chunk_exchanges(
                content,
                chunk_size=cfg_chunk_size,
                min_chunk_size=cfg_min_chunk_size,
            )

        if not chunks:
            if not dry_run:
                _register_file(collection, source_file, wing, agent, extract_mode)
            continue

        # Detect room from content (general mode uses memory_type instead)
        if extract_mode != "general":
            room = detect_convo_room(content)
        else:
            room = None  # set per-chunk below

        if dry_run:
            if extract_mode == "general":
                from collections import Counter

                type_counts = Counter(c.get("memory_type", "general") for c in chunks)
                types_str = ", ".join(f"{t}:{n}" for t, n in type_counts.most_common())
                print(f"    [DRY RUN] {filepath.name} -> {len(chunks)} memories ({types_str})")
            else:
                print(f"    [DRY RUN] {filepath.name} -> room:{room} ({len(chunks)} drawers)")
            total_drawers += len(chunks)
            # Track room counts
            if extract_mode == "general":
                for c in chunks:
                    room_counts[c.get("memory_type", "general")] += 1
            else:
                room_counts[room] += 1
            files_mined += 1
            if limit > 0 and files_mined >= limit:
                break
            continue

        if extract_mode != "general":
            room_counts[room] += 1

        # Lock + purge stale + file fresh chunks. Lock serializes concurrent
        # agents; purge removes pre-v2 drawers so the schema bump applies.
        report_phase("file", chunks=len(chunks), **progress)
        drawers_added, room_delta, skipped = _file_chunks_locked(
            collection,
            source_file,
            chunks,
            wing,
            room,
            agent,
            extract_mode,
            authored_at=_extract_authored_at(filepath),
            content_hash=content_hash,
            source_dir_ino=source_directory_identity(filepath),
        )
        if skipped:
            files_skipped += 1
            continue
        for r, n in room_delta.items():
            room_counts[r] += n

        for h, _ in new_items:
            mined_content_hashes[(wing, h)] = source_file
        total_drawers += drawers_added
        files_mined += 1
        print(f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")
        if limit > 0 and files_mined >= limit:
            break

    if not dry_run:
        # Compute hallways before the FTS5 validation: the latter opens a direct sqlite
        # connection to the Chroma DB, which can invalidate the live collection handle on
        # some Chroma builds and make the hallway fetch fail.
        report_phase("hallways", drawers_filed=total_drawers)
        _compute_hallways_for_wing_safe(wing, collection, total_drawers, config=palace_config)
        report_phase("validate", drawers_filed=total_drawers)
        _validate_palace_fts5_after_mine(palace_path)
    report_phase("done", drawers_filed=total_drawers)

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {files_processed - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]")
        sys.exit(1)
    from .config import MempalaceConfig

    mine_convos(sys.argv[1], palace_path=MempalaceConfig().palace_path)
