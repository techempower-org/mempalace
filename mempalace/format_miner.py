"""format_miner.py — proposed for mempalace 3.3.6.

A third miner alongside ``miner.py`` (project files) and ``convo_miner.py``
(chat exports). This one handles **binary office-format documents**:
PDF, DOCX, PPTX, XLSX, RTF, EPUB.

Architecture matches the existing miner-per-content-type pattern:

    mempalace mine <dir>                  → miner.py
    mempalace mine <dir> --mode convos    → convo_miner.py
    mempalace mine <dir> --mode extract   → format_miner.py (this file)

**Read-time conversion**, never modifies source files on disk. The user's
``~/research.pdf`` stays exactly as it was after a mine — the bytes are
read into memory, handed to Microsoft's MarkItDown library for conversion
to Markdown, and the resulting text flows into the normal chunker /
drawer-store pipeline. Conversion artifacts never touch disk.

MarkItDown is an **optional** runtime dependency (declared as an extra).
If the user runs ``mempalace mine --mode extract`` without it installed,
they get a clear ``pip install markitdown`` instruction, not a crash.

**Per-format transformer routing** (verified live on a local mixed-format
test directory, 2026-05-19): MarkItDown 0.1.5 does NOT convert .rtf —
it returns the raw RTF control-code source unchanged. So .rtf is routed
to the purpose-built ``striprtf`` library; all other formats stay on
MarkItDown.

    .pdf, .docx, .pptx, .xlsx, .epub  → MarkItDown
    .rtf                              → striprtf

Both libraries are optional runtime dependencies. If a user tries to
extract a format whose transformer isn't installed, they get a clear
install message (SKIP_NO_MARKITDOWN or SKIP_NO_STRIPRTF), not a crash.

**13 fringe cases handled** (spec finalized 2026-05-19):

    1.  MarkItDown not installed     → SKIP_NO_MARKITDOWN  (clear install msg)
    2.  File too large (> max)        → SKIP_TOO_LARGE
    3.  iCloud cloud-only file        → SKIP_CLOUD_ONLY
    4.  Encrypted PDF                 → SKIP_ENCRYPTED
    5.  Empty file                    → SKIP_EMPTY
    6.  Permission denied             → SKIP_PERMISSION
    7.  Broken symlink                → SKIP_BROKEN_SYMLINK
    8.  Dirty encoding                → recovered via decode_robust
    9.  Windows path semantics        → pathlib throughout
    10. MarkItDown internal crash     → SKIP_EXTRACTION_ERROR
    11. Network / sync timeout        → SKIP_NETWORK_TIMEOUT
    12. Unrecognized extension        → SKIP_UNRECOGNIZED
    13. striprtf not installed        → SKIP_NO_STRIPRTF    (added 2026-05-19
                                                            after live test
                                                            on local RTF files)

Deferred (out of scope): custom PDF parsers for specific document types,
OCR on scanned PDFs, DRM-locked files, pathological corrupt files.
These get reported and skipped.
"""

from __future__ import annotations

import enum
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from .palace import (
    NORMALIZE_VERSION,
    SKIP_DIRS,
    _validate_palace_fts5_after_mine,
    file_already_mined,
    get_collection,
    mine_lock,
    mine_palace_lock,
    mine_yield_point,
    palace_write_serial,
    print_files_failed,
    raise_if_files_failed,
)

# Module-level imports from .miner so tests can patch them via
# mempalace.format_miner.<name>. Lazy imports inside functions would not
# expose these as attributes of this module, breaking the test seams.
from .config import MempalaceConfig, normalize_wing_name
from .collision_scan import assert_no_collisions
from .ids import ID_RECIPE, make_drawer_id_from_chunk, make_format_sentinel_id
from .source_identity import identity_metadata, source_directory_identity
from .miner import (
    _compute_topic_tunnels_for_wing,
    chunk_text,
    detect_room,
    load_config,
)

__all__ = [
    "SUPPORTED_FORMATS",
    "DEFAULT_MAX_FILE_SIZE",
    "ExtractionStatus",
    "decode_robust",
    "is_icloud_dataless",
    "extract_text",
    "scan_formats",
    "mine_formats",
]


# Same batch size as miner.py / convo_miner.py — bounds memory + Chroma payload.
DRAWER_UPSERT_BATCH_SIZE = 1000

# _file_chunks_locked returns this when the stale-drawer delete failed.
# Truthy, so ``if skipped`` still means nothing was filed.
PURGE_FAILED = "purge_failed"
_FORMAT_EXTRACT_MODE = "format"

# Minimum chunk size (chars) — drawers below this are dropped as not useful.
MIN_CHUNK_SIZE = 50


logger = logging.getLogger("mempalace_format_miner")


# Extensions MarkItDown can convert. Lowercase, leading dot. Case-insensitive
# match performed against ``Path.suffix.lower()``.
SUPPORTED_FORMATS = frozenset(
    {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".rtf",
        ".epub",
    }
)


# Same default cap as miner.py / convo_miner.py — guards against pathological
# binary files and runaway memory. The caller can override via
# ``extract_text(..., max_file_size=...)`` for legitimately large documents.
DEFAULT_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB


# Filename patterns that are not user content even if their extension matches.
_SKIP_FILENAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}


# Message patterns that identify an encrypted / password-protected PDF
# regardless of which underlying library raised the exception.
_ENCRYPTED_PATTERNS = re.compile(
    r"(encrypt|decrypt|password|protected)",
    re.IGNORECASE,
)


class ExtractionStatus(enum.Enum):
    """Outcome of an ``extract_text`` call.

    Test-friendly enum (each case has its own name) so callers can assert
    exactly which path was taken. ``OK`` means text came back; everything
    else is a skip variant.
    """

    OK = "ok"
    SKIP_TOO_LARGE = "skip:too_large"
    SKIP_CLOUD_ONLY = "skip:cloud_only"
    SKIP_EMPTY = "skip:empty"
    SKIP_NO_MARKITDOWN = "skip:no_markitdown"
    SKIP_NO_STRIPRTF = "skip:no_striprtf"
    SKIP_ENCRYPTED = "skip:encrypted"
    SKIP_PERMISSION = "skip:permission"
    SKIP_BROKEN_SYMLINK = "skip:broken_symlink"
    SKIP_UNRECOGNIZED = "skip:unrecognized"
    SKIP_EXTRACTION_ERROR = "skip:extraction_error"
    SKIP_MISSING_FORMAT_DEPS = "skip:missing_format_deps"
    SKIP_NETWORK_TIMEOUT = "skip:network_timeout"
    SKIP_UNREADABLE = "skip:unreadable"


# Skip variants that represent TRANSIENT environmental issues — the optional
# transformer dep isn't installed yet, or the network blipped. Sentinel
# writes for these would permanently mark the file as "already mined" and
# defeat re-mining after the missing piece is installed / the network is
# back. The orchestrator (``mine_formats``) checks this set before calling
# ``_register_file``. Per PR #1555 review (Copilot #14).
_TRANSIENT_MISSING_DEP_STATUSES = frozenset(
    {
        ExtractionStatus.SKIP_NO_MARKITDOWN,
        ExtractionStatus.SKIP_NO_STRIPRTF,
        ExtractionStatus.SKIP_MISSING_FORMAT_DEPS,
        ExtractionStatus.SKIP_NETWORK_TIMEOUT,
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Encoding fallback — same pattern as the terminal-session normalizer
# ─────────────────────────────────────────────────────────────────────────────


def decode_robust(raw: bytes) -> str:
    """Decode bytes to text without raising on dirty encodings.

    Strategy: UTF-8 first (the clean case). On failure, try CP1252 (handles
    legacy smart-quote bytes 0x91-0x9F that surface in older Office docs).
    Final fallback is UTF-8 with ``errors='replace'`` so no byte is ever
    lost — only made visible as the replacement char.
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        pass
    return raw.decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# iCloud cloud-only file detection
# ─────────────────────────────────────────────────────────────────────────────


def is_icloud_dataless(path: Path) -> bool:
    """True if ``path`` is an iCloud cloud-only placeholder (not local).

    Two indicators:
      1. The literal ``.icloud`` suffix iCloud uses for offloaded files.
      2. The macOS UF_COMPRESSED / dataless flag on the inode (``st_flags``).

    Either signal means MarkItDown would block on file I/O waiting for
    iCloud to materialize the bytes, which can hang for minutes. We skip
    these and report.
    """
    if path.suffix.lower() == ".icloud":
        return True
    # macOS-only: stat() exposes st_flags which carries the dataless flag.
    # On other platforms this attribute is absent or always 0.
    try:
        flags = getattr(path.lstat(), "st_flags", 0)
    except OSError:
        return False
    # SF_DATALESS = 0x40000000 on macOS. Avoid hardcoding magic numbers
    # cross-platform; check the bit if it exists.
    DATALESS_FLAG = 0x40000000
    return bool(flags & DATALESS_FLAG)


# ─────────────────────────────────────────────────────────────────────────────
# Conversion via MarkItDown — isolated so tests can patch this single seam
# ─────────────────────────────────────────────────────────────────────────────


def _extract_via_markitdown(path: Path) -> Optional[str]:
    """Run MarkItDown on ``path`` and return the resulting Markdown text.

    Used for .pdf, .docx, .pptx, .xlsx, .epub. NOT used for .rtf — MarkItDown
    0.1.5 doesn't convert RTF (returns raw control-code source unchanged),
    so .rtf is routed to ``_extract_via_striprtf`` instead.

    Raises ``ImportError`` if the markitdown package is not installed
    (caller's responsibility to translate this into ``SKIP_NO_MARKITDOWN``).

    Raises any other exception MarkItDown raises — the caller classifies
    them via message pattern (encrypted vs generic crash vs timeout).

    Returns ``None`` if MarkItDown returns an empty result for the file.
    """
    try:
        from markitdown import MarkItDown  # type: ignore[import-not-found]
    except ImportError:
        raise

    converter = MarkItDown()
    result = converter.convert(str(path))
    text = getattr(result, "text_content", None) or getattr(result, "markdown", None)
    if text is None:
        return None
    if not isinstance(text, str):
        return None
    return text


def _extract_via_striprtf(path: Path) -> Optional[str]:
    """Run striprtf on ``path`` and return plain text.

    Used exclusively for .rtf (see ``_extract_via_markitdown`` docstring for
    why). striprtf is pure-Python, MIT-licensed, ~150 lines, cross-platform
    (works on Python 3.6+).

    Raises ``ImportError`` if the striprtf package is not installed
    (caller's responsibility to translate this into ``SKIP_NO_STRIPRTF``).

    Returns ``None`` if striprtf strips the file to empty text.
    """
    try:
        from striprtf.striprtf import rtf_to_text  # type: ignore[import-not-found]
    except ImportError:
        raise

    raw = path.read_bytes()
    source = decode_robust(raw)
    text = rtf_to_text(source)
    if not isinstance(text, str):
        return None
    if text == "":
        return None
    return text


# ─────────────────────────────────────────────────────────────────────────────
# extract_text — the dispatcher with all 12 fringe cases handled
# ─────────────────────────────────────────────────────────────────────────────


def extract_text(
    path: Union[Path, str],
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> tuple[Optional[str], ExtractionStatus]:
    """Convert ``path`` to plain text via MarkItDown, with comprehensive fringe-case handling.

    Returns ``(text, ExtractionStatus)`` — text is ``None`` for every skip
    case, a non-empty string for ``OK``.

    Pure function (no I/O outside the file at ``path``). Source file is
    never modified.
    """
    # Accept str or Path; pathlib handles Windows separators correctly.
    # ``expanduser()`` resolves ``~/foo.pdf`` style paths so CLI inputs work
    # without forcing the caller to pre-resolve. Per PR #1555 review (Copilot).
    p = Path(path).expanduser()

    # Fringe Case 7 — broken symlink.
    # ``is_symlink()`` true + ``exists()`` false ⇒ link target is missing.
    if p.is_symlink() and not p.exists():
        logger.info("skip:broken_symlink %s", p)
        return None, ExtractionStatus.SKIP_BROKEN_SYMLINK

    # Fringe Case 3 — iCloud cloud-only file. Detect BEFORE stat() so we
    # don't accidentally trigger an iCloud materialization fetch.
    if is_icloud_dataless(p):
        logger.info("skip:cloud_only %s", p)
        return None, ExtractionStatus.SKIP_CLOUD_ONLY

    # General readability check. Anything unreadable here (file gone,
    # permission denied at stat-time, etc.) collapses to SKIP_UNREADABLE
    # or SKIP_PERMISSION depending on the error class.
    try:
        stat = p.stat()
    except PermissionError:
        logger.info("skip:permission (stat) %s", p)
        return None, ExtractionStatus.SKIP_PERMISSION
    except FileNotFoundError:
        # Distinguish "broken symlink" (target gone) from "file disappeared
        # between scan and extract" (regular file, no longer exists). The
        # is_symlink() check at the top of this function catches the symlink
        # case BEFORE stat(), so by the time we land here the symlink path
        # is rare — but a race between scan_formats and extract_text on a
        # plain file is the common cause. Per PR #1555 review (Copilot #8).
        if p.is_symlink():
            logger.info("skip:broken_symlink (stat) %s", p)
            return None, ExtractionStatus.SKIP_BROKEN_SYMLINK
        logger.info("skip:unreadable (file gone after scan) %s", p)
        return None, ExtractionStatus.SKIP_UNREADABLE
    except OSError as exc:
        logger.info("skip:unreadable %s -- %s", p, exc)
        return None, ExtractionStatus.SKIP_UNREADABLE

    # Fringe Case 5 — empty file. Skip silently.
    if stat.st_size == 0:
        logger.debug("skip:empty %s", p)
        return None, ExtractionStatus.SKIP_EMPTY

    # Fringe Case 2 — file exceeds the caller's cap. The default is 500 MB
    # (same as the existing miners). Callers with legitimate large docs
    # raise the cap explicitly.
    if stat.st_size > max_file_size:
        logger.info("skip:too_large %s (%d bytes > %d)", p, stat.st_size, max_file_size)
        return None, ExtractionStatus.SKIP_TOO_LARGE

    # Fringe Case 12 — unrecognized extension. Cheap to check, do it last
    # so we still report size / cloud / symlink issues for the obvious cases.
    if p.suffix.lower() not in SUPPORTED_FORMATS:
        logger.debug("skip:unrecognized %s (suffix=%s)", p, p.suffix)
        return None, ExtractionStatus.SKIP_UNRECOGNIZED

    # Per-format transformer routing. .rtf goes to striprtf (MarkItDown
    # 0.1.5 doesn't convert RTF — returns raw control-code source
    # unchanged, verified live on a local RTF set 2026-05-19).
    is_rtf = p.suffix.lower() == ".rtf"
    try:
        if is_rtf:
            text = _extract_via_striprtf(p)
        else:
            text = _extract_via_markitdown(p)
    except ImportError:
        # Fringe Case 1 / 13 — transformer not installed in this env.
        if is_rtf:
            logger.warning(
                "skip:no_striprtf %s — install with: pip install striprtf",
                p,
            )
            return None, ExtractionStatus.SKIP_NO_STRIPRTF
        logger.warning(
            "skip:no_markitdown %s — install with: pip install markitdown",
            p,
        )
        return None, ExtractionStatus.SKIP_NO_MARKITDOWN
    except TimeoutError:
        # Fringe Case 11 — network or sync-drive timeout.
        logger.info("skip:network_timeout %s", p)
        return None, ExtractionStatus.SKIP_NETWORK_TIMEOUT
    except PermissionError:
        # Fringe Case 6 — permission denied during file read.
        logger.info("skip:permission %s", p)
        return None, ExtractionStatus.SKIP_PERMISSION
    except Exception as exc:
        # Fringe Case 14 (PR #1555 review, Igor): MarkItDown raises
        # ``MissingDependencyException`` when a per-format sub-extra
        # (e.g. ``markitdown[pdf]``) isn't installed. Surface this as a
        # distinct status so users see the actionable signal instead of
        # the generic SKIP_EXTRACTION_ERROR. Match by type name (not
        # isinstance) so the catch fires without requiring markitdown to
        # be import-resolvable at module load — keeps the static-import
        # surface unchanged.
        if type(exc).__name__ == "MissingDependencyException":
            logger.warning(
                "skip:missing_format_deps %s — %s: %s",
                p,
                type(exc).__name__,
                str(exc)[:200],
            )
            return None, ExtractionStatus.SKIP_MISSING_FORMAT_DEPS
        # Fringe Case 4 vs Case 10: encrypted vs generic crash, by message.
        msg = str(exc)
        if _ENCRYPTED_PATTERNS.search(msg):
            logger.info("skip:encrypted %s -- %s", p, msg[:120])
            return None, ExtractionStatus.SKIP_ENCRYPTED
        logger.warning("skip:extraction_error %s -- %s: %s", p, type(exc).__name__, msg[:200])
        return None, ExtractionStatus.SKIP_EXTRACTION_ERROR

    # Either transformer can legitimately return None / empty (malformed
    # docs that parse but extract nothing). Treat both as extraction error
    # so the caller knows to skip rather than file an empty drawer.
    if not text:
        transformer = "striprtf" if is_rtf else "markitdown"
        logger.info("skip:extraction_error %s -- %s returned None/empty", p, transformer)
        return None, ExtractionStatus.SKIP_EXTRACTION_ERROR

    return text, ExtractionStatus.OK


# ─────────────────────────────────────────────────────────────────────────────
# Directory walker
# ─────────────────────────────────────────────────────────────────────────────


def scan_formats(directory: Union[Path, str]) -> list[Path]:
    """Walk ``directory`` recursively and return supported files, sorted.

    Skips:
      - Hidden / build directories listed in ``palace.SKIP_DIRS``
      - Filenames listed in ``_SKIP_FILENAMES`` (.DS_Store etc.)
      - Symlinks (prevents circular links / processing the same file via
        multiple paths; mirrors ``miner.py`` and ``convo_miner.py``)
      - Files whose suffix isn't in ``SUPPORTED_FORMATS``

    Returns a list of ``Path`` objects. The order is deterministic
    (sorted by path) so a re-mine processes files in the same order each
    time — useful for reproducing bug reports.
    """
    # ``expanduser().resolve()`` normalizes ``~/docs`` and relative paths so
    # CLI inputs like ``mempalace mine --mode extract ~/docs`` work without
    # the caller pre-resolving. Per PR #1555 review (Copilot #9).
    root = Path(directory).expanduser().resolve()
    if not root.exists():
        return []

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames in place to prune the walk. Uses the shared
        # palace.SKIP_DIRS constant so this stays in sync with the other
        # miners' skip set.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in _SKIP_FILENAMES:
                continue
            p = Path(dirpath) / name
            # Skip symlinks — prevents following links to /dev/urandom,
            # circular links, or processing the same file twice via
            # different paths. Mirrors miner.py:1068.
            if p.is_symlink():
                continue
            if p.suffix.lower() not in SUPPORTED_FORMATS:
                continue
            found.append(p)

    found.sort()
    return found


# ─────────────────────────────────────────────────────────────────────────────
# mine_formats — orchestrator. Walks a directory, transforms each supported
# file via extract_text, chunks the resulting Markdown via miner.chunk_text,
# files drawers via the same lock + purge + upsert pattern convo_miner uses.
# Source files on disk are never modified.
# ─────────────────────────────────────────────────────────────────────────────


def _print_mine_summary(
    files_seen: int,
    files_with_text: int,
    files_skipped: int,
    files_errored: int,
    total_drawers: int,
    status_counts: dict,
) -> None:
    """Print the post-mine summary block.

    Factored out of ``mine_formats`` to keep the orchestrator under the
    project's mccabe complexity ceiling (max-complexity=25 in pyproject.toml).
    No behavior change.
    """
    print(f"\n{'=' * 55}")
    print("  Summary")
    print(f"{'-' * 55}")
    print(f"  Files seen:        {files_seen}")
    print(f"  Files extracted:   {files_with_text}")
    print(f"  Files skipped:     {files_skipped}")
    print(f"  Files errored:     {files_errored}")
    print(f"  Total drawers:     {total_drawers}")
    if status_counts:
        print("  Extraction status:")
        for name, count in sorted(status_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {name:30} {count}")
    print(f"{'=' * 55}\n")


def _register_skip_sentinel_if_appropriate(
    collection,
    source_file: str,
    wing: str,
    agent: str,
    status: ExtractionStatus,
    source_mtime: Optional[float] = None,
) -> None:
    """Write the ``file_already_mined`` sentinel ONLY when the skip is durable.

    Transient missing-dep statuses (``SKIP_NO_MARKITDOWN``, ``SKIP_NO_STRIPRTF``,
    ``SKIP_MISSING_FORMAT_DEPS``, ``SKIP_NETWORK_TIMEOUT``) intentionally do
    NOT mark the file as mined — installing the missing extra later (or
    reconnecting the network) should let the next mine pass pick it up.
    """
    if status in _TRANSIENT_MISSING_DEP_STATUSES:
        return
    _register_file(collection, source_file, wing, agent, source_mtime=source_mtime)


def _page_is_rows(batch) -> Optional[list[tuple[str, str, dict]]]:
    """Normalize one ``collection.get`` page, or ``None`` when it is not a page."""
    if not isinstance(batch, dict):
        return None
    ids = batch.get("ids") or []
    if not isinstance(ids, list):
        return None
    docs = batch.get("documents") or []
    metas = batch.get("metadatas") or []
    if not isinstance(docs, list):
        docs = []
    if not isinstance(metas, list):
        metas = []
    rows = []
    for index, drawer_id in enumerate(ids):
        doc = docs[index] if index < len(docs) and isinstance(docs[index], str) else ""
        meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
        rows.append((str(drawer_id), doc, meta))
    return rows


def _load_format_rows(collection, source_file: str) -> list[tuple[str, str, dict]]:
    """Format-mode drawers for ``source_file``, including the sentinel."""
    rows: list[tuple[str, str, dict]] = []
    offset = 0
    while True:
        batch = collection.get(
            where={"source_file": source_file},
            limit=1000,
            offset=offset,
            include=["documents", "metadatas"],
        )
        page = _page_is_rows(batch)
        if not page:
            break
        for drawer_id, doc, meta in page:
            if meta.get("extract_mode") == _FORMAT_EXTRACT_MODE:
                rows.append((drawer_id, doc, meta))
        if len(page) < 1000:
            break
        offset += len(page)
    return rows


def _restore_format_rows(
    collection, prior: list[tuple[str, str, dict]], written_ids: list[str]
) -> None:
    """Put ``prior`` back after a rewrite that did not commit.

    Brand-new ids from this attempt are deleted first so they are not left
    beside the restored drawers. A delete that itself fails is logged: the
    uncommitted rows carry a ``chunk_total`` the current row count cannot
    meet, so the next mine retries instead of treating the mix as done.
    """
    if written_ids:
        try:
            collection.delete(ids=list(dict.fromkeys(written_ids)))
        except Exception:
            logger.warning(
                "Failed to drop uncommitted format drawers while restoring previous rows",
                exc_info=True,
            )
    if not prior:
        return
    try:
        collection.upsert(
            ids=[row[0] for row in prior],
            documents=[row[1] for row in prior],
            metadatas=[row[2] for row in prior],
        )
    except Exception:
        logger.warning(
            "Failed to restore previous format drawers after a failed rewrite",
            exc_info=True,
        )


def _delete_drawer_ids(collection, drawer_ids: list[str]) -> None:
    if drawer_ids:
        collection.delete(ids=drawer_ids)


def _sentinel_metadata(
    source_file: str,
    wing: str,
    agent: str,
    source_mtime: Optional[float],
    chunk_total: int,
    filed_at: str,
) -> dict:
    meta = {
        "wing": wing,
        "room": "documents",
        "source_file": source_file,
        "chunk_index": -1,
        "added_by": agent,
        "filed_at": filed_at,
        "ingest_mode": "extract",
        "extract_mode": _FORMAT_EXTRACT_MODE,
        "normalize_version": NORMALIZE_VERSION,
        "is_sentinel": True,
        "id_recipe": ID_RECIPE,
        "chunk_total": chunk_total,
        # The sentinel names a real source file and ``sync`` reads it as an
        # ordinary drawer, so it needs the identity the file's own drawers have.
        **identity_metadata(source_file),
    }
    if source_mtime is not None:
        meta["source_mtime"] = source_mtime
    return meta


def _register_file(
    collection,
    source_file: str,
    wing: str,
    agent: str,
    source_mtime: Optional[float] = None,
) -> None:
    """Write a sentinel so file_already_mined() returns True for 0-chunk files.

    The sentinel carries the mtime captured with the read and ``chunk_total``
    1, which is what ``file_already_mined(check_mtime=True)`` accepts. A
    marker with no ``source_mtime`` never matches that check, so the file
    would be extracted again on every run.

    Other format-mode rows for the file are removed only after the sentinel
    is in place, and the sentinel's ``chunk_total`` stays above the row count
    until that delete succeeds. A failure restores the previous rows and
    raises, so the skip is not reported as filed.
    """
    if source_mtime is None:
        try:
            source_mtime = os.path.getmtime(source_file)
        except OSError:
            source_mtime = None
    sentinel_id = make_format_sentinel_id(wing, source_file)
    filed_at = datetime.now().isoformat()
    prior = _load_format_rows(collection, source_file)
    written_ids: list[str] = []
    try:
        # chunk_total 2 with a single row cannot satisfy file_already_mined.
        collection.upsert(
            documents=["[empty]"],
            ids=[sentinel_id],
            metadatas=[_sentinel_metadata(source_file, wing, agent, source_mtime, 2, filed_at)],
        )
        written_ids.append(sentinel_id)
        stale = [row[0] for row in prior if row[0] != sentinel_id]
        _delete_drawer_ids(collection, stale)
        collection.upsert(
            documents=["[empty]"],
            ids=[sentinel_id],
            metadatas=[_sentinel_metadata(source_file, wing, agent, source_mtime, 1, filed_at)],
        )
    except Exception:
        _restore_format_rows(collection, prior, written_ids)
        raise


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    source_mtime: Optional[float] = None,
    content: Optional[str] = None,
    source_dir_ino: Optional[str] = None,
):
    """Lock the source file and replace its format-mode drawers.

    Drawers are upserted before any previous format drawer is deleted. Until
    the delete finishes, ``chunk_total`` is one higher than the number of
    rows written, so ``file_already_mined`` does not treat a crash in between
    as a complete mine. A failed delete restores the previous drawers and
    returns :data:`PURGE_FAILED` instead of leaving stale and new rows mixed.

    Returns ``(drawers_added, skipped)``. ``skipped`` is :data:`PURGE_FAILED`
    when the stale-drawer delete raised.
    """
    from .miner import (
        _extract_content_date_with_source,
        _extract_entities_for_metadata,
        detect_hall,
    )

    # Content date is per file. ``content`` is the full extracted text; the
    # helper still uses the filename and mtime when it is absent.
    file_content_date, file_content_date_source = _extract_content_date_with_source(
        source_file, content or ""
    )

    with mine_lock(source_file):
        # extract_mode scopes the check to format drawers. A project or convo
        # drawer for the same path is not "already mined" for this miner.
        if file_already_mined(
            collection,
            source_file,
            check_mtime=True,
            extract_mode=_FORMAT_EXTRACT_MODE,
        ):
            return 0, True

        filed_at = datetime.now().isoformat()
        prior = _load_format_rows(collection, source_file)
        written_ids: list[str] = []
        try:
            drawers_added = _upsert_chunk_batches(
                collection,
                chunks,
                wing=wing,
                room=room,
                source_file=source_file,
                agent=agent,
                source_mtime=source_mtime,
                source_dir_ino=source_dir_ino,
                file_content_date=file_content_date,
                file_content_date_source=file_content_date_source,
                filed_at=filed_at,
                chunk_total=len(chunks) + 1,
                written_ids=written_ids,
                detect_hall=detect_hall,
                extract_entities=_extract_entities_for_metadata,
            )
        except Exception:
            _restore_format_rows(collection, prior, written_ids)
            raise

        new_ids = set(written_ids)
        stale = [row[0] for row in prior if row[0] not in new_ids]
        try:
            _delete_drawer_ids(collection, stale)
        except Exception as exc:
            _restore_format_rows(collection, prior, written_ids)
            print(
                f"  ! [error] stale-drawer purge failed for {source_file!r} "
                f"({exc!r}); leaving existing drawers untouched, will retry "
                f"on the next mine",
                file=sys.stderr,
            )
            logger.debug("Stale-drawer purge failed for %s", source_file, exc_info=True)
            return 0, PURGE_FAILED

        try:
            _upsert_chunk_batches(
                collection,
                chunks,
                wing=wing,
                room=room,
                source_file=source_file,
                agent=agent,
                source_mtime=source_mtime,
                source_dir_ino=source_dir_ino,
                file_content_date=file_content_date,
                file_content_date_source=file_content_date_source,
                filed_at=filed_at,
                chunk_total=len(chunks),
                written_ids=written_ids,
                detect_hall=detect_hall,
                extract_entities=_extract_entities_for_metadata,
            )
        except Exception:
            _restore_format_rows(collection, prior, written_ids)
            raise
    return drawers_added, False


def _upsert_chunk_batches(
    collection,
    chunks,
    *,
    wing: str,
    room: str,
    source_file: str,
    agent: str,
    source_mtime: Optional[float],
    source_dir_ino: Optional[str],
    file_content_date,
    file_content_date_source,
    filed_at: str,
    chunk_total: int,
    written_ids: list[str],
    detect_hall,
    extract_entities,
) -> int:
    """Upsert ``chunks`` and append every stored id to ``written_ids``."""
    drawers_added = 0
    for batch_start in range(0, len(chunks), DRAWER_UPSERT_BATCH_SIZE):
        batch_docs: list = []
        batch_ids: list = []
        batch_metas: list = []
        for chunk in chunks[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]:
            drawer_id = make_drawer_id_from_chunk(wing, room, source_file, chunk["chunk_index"])
            chunk_content = chunk["content"]
            meta: dict = {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "chunk_index": chunk["chunk_index"],
                "added_by": agent,
                "filed_at": filed_at,
                "ingest_mode": "extract",
                "extract_mode": _FORMAT_EXTRACT_MODE,
                "normalize_version": NORMALIZE_VERSION,
                "hall": detect_hall(chunk_content),
                "id_recipe": ID_RECIPE,
                "chunk_total": chunk_total,
            }
            if source_mtime is not None:
                meta["source_mtime"] = source_mtime
            if source_dir_ino:
                meta["source_dir_ino"] = source_dir_ino
            if chunk.get("line_start") is not None:
                meta["line_start"] = chunk["line_start"]
            if chunk.get("line_end") is not None:
                meta["line_end"] = chunk["line_end"]
            if file_content_date:
                meta["content_date"] = file_content_date
                meta["content_date_source"] = file_content_date_source
            entities = extract_entities(chunk_content)
            if entities:
                meta["entities"] = entities
            batch_docs.append(chunk_content)
            batch_ids.append(drawer_id)
            batch_metas.append(meta)
        assert_no_collisions(list(zip(batch_ids, batch_metas)), collection)
        try:
            collection.upsert(
                documents=batch_docs,
                ids=batch_ids,
                metadatas=batch_metas,
            )
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise
        written_ids.extend(batch_ids)
        drawers_added += len(batch_docs)
    return drawers_added


def _yield_and_reopen(palace_path: str, collection):
    """Pass a file boundary, reopening the collection if requests ran in it.

    A request in the hub's handoff may close the backend under this handle (a
    search resets the Chroma caches after a transient index error). Reopening
    returns the cached collection when nothing closed it. A dry run holds no
    collection and gets ``None`` back.
    """
    if not mine_yield_point() or collection is None:
        return collection
    return get_collection(palace_path)


def mine_formats(
    format_dir: str,
    palace_path: str,
    wing: Optional[str] = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
) -> None:
    """Mine a directory of binary office-format files into the palace.

    A non-dry-run holds the per-palace write lock for the whole mine, the
    same lock ``miner.mine`` and ``convo_miner.mine_convos`` hold. Dry-run
    does not write, so it does not take the lock. ``KeyboardInterrupt``
    propagates after the summary. A file that fails to file raises
    ``MineFileErrors`` after the summary.
    """
    if dry_run:
        _mine_formats_impl(
            format_dir,
            palace_path,
            wing=wing,
            agent=agent,
            limit=limit,
            dry_run=True,
        )
        return
    with mine_palace_lock(palace_path):
        _mine_formats_impl(
            format_dir,
            palace_path,
            wing=wing,
            agent=agent,
            limit=limit,
            dry_run=False,
        )


def _mine_formats_impl(
    format_dir: str,
    palace_path: str,
    wing: Optional[str] = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
) -> None:
    """Mine a directory of binary office-format files into the palace.

    Walks ``format_dir`` via ``scan_formats``, converts each supported file
    to text via ``extract_text`` (which routes to MarkItDown or striprtf
    per format), chunks the result with the same chunker miner.py uses, and
    files the chunks as drawers under the given wing.

    Source files on disk are never modified — conversion is in-memory only.

    Parameters
    ----------
    format_dir :
        Directory to walk recursively. Hidden / build dirs are skipped
        (see ``palace.SKIP_DIRS``). Symlinks are skipped (consistency with
        ``miner.py`` and ``convo_miner.py``). Only files matching
        ``SUPPORTED_FORMATS`` are processed.
    palace_path :
        Path to the ChromaDB palace (the destination of the drawers).
    wing :
        Wing name. Defaults to the basename of ``format_dir`` (normalized).
    agent :
        Identifier recorded in each drawer's ``added_by`` metadata.
    limit :
        If > 0, process at most this many files. Useful for sampling.
    dry_run :
        If True, walk + extract + chunk but do NOT open the collection or
        upsert any drawers. Just prints what would have been filed.

    Notes
    -----
    - Loads ``MempalaceConfig`` once at the start. The current chunker
      (``miner.chunk_text``) uses module-level CHUNK_SIZE constants and
      doesn't accept overrides, so the loaded config values aren't yet
      threaded into chunking; the load is wired so future ``chunk_text``
      refactors that accept per-call sizing pick this up automatically.
    - One malformed file is recorded and the loop continues. After the
      summary, ``MineFileErrors`` is raised when any file failed, so the
      process does not exit 0.
    - ``KeyboardInterrupt`` prints the summary and propagates, which is
      exit code 130 when nothing else catches it.
    """
    # MempalaceConfig + chunk_text are imported at module level (above) so
    # tests can patch ``mempalace.format_miner.MempalaceConfig`` and
    # ``mempalace.format_miner.chunk_text`` directly. Hoisted as part of the
    # PR #1555 polish PR (Gemini #3).

    # Load palace-wide config. Chunk parameters (chunk_size, chunk_overlap,
    # min_chunk_size) are now threaded through chunk_text below, so users
    # who customized their config see the effect in format-mode mining.
    # Per PR #1555 review (Gemini #3).
    palace_config = MempalaceConfig(palace_path=palace_path)
    writes_at_start = palace_write_serial()

    format_path = Path(format_dir).expanduser().resolve()
    if not wing:
        wing = normalize_wing_name(format_path.name)

    # Load the project's mempalace.yaml (rooms list + wing override) so
    # detect_room has real categories to route into. Mirrors miner.py:1154.
    # Fall back to a single "documents" room if no config exists — keeps the
    # detector well-defined without forcing a yaml on every format dir.
    try:
        project_config = load_config(format_dir)
        rooms = project_config.get(
            "rooms",
            [
                {
                    "name": "documents",
                    "description": "All format-mined files",
                    "keywords": ["documents"],
                }
            ],
        )
    except Exception:
        logger.debug("mine_formats: load_config fallback to default rooms", exc_info=True)
        rooms = [
            {
                "name": "documents",
                "description": "All format-mined files",
                "keywords": ["documents"],
            }
        ]

    # Initialize loop-state up-front so the outer try/except handlers and the
    # post-try summary print can run safely even if scan_formats or
    # get_collection raises before the for loop starts. Per PR #1555 review
    # (Gemini #5).
    files: list = []
    collection = None
    total_drawers = 0
    files_mined = 0
    files_skipped = 0
    files_with_text = 0
    files_failed: list = []
    files_processed = 0
    status_counts: dict = defaultdict(int)

    try:
        # Use the resolved ``format_path``, not the raw ``format_dir``, so that
        # ``~/docs`` and relative inputs work consistently. Per PR #1555 review
        # (Copilot #10).
        files = scan_formats(format_path)

        print(f"\n{'=' * 55}")
        print("  MemPalace Mine -- Format extraction")
        print(f"{'=' * 55}")
        print(f"  Wing:    {wing}")
        print(f"  Source:  {format_path}")
        limit_suffix = f" (limit: {limit} new)" if limit > 0 else ""
        print(f"  Files:   {len(files)}{limit_suffix}")
        print(f"  Palace:  {palace_path}")
        if dry_run:
            print("  DRY RUN -- nothing will be filed")
        print(f"{'-' * 55}\n")

        collection = get_collection(palace_path) if not dry_run else None

        for i, filepath in enumerate(files, 1):
            collection = _yield_and_reopen(palace_path, collection)
            files_processed = i
            source_file = str(filepath)

            # Per-file try/except so one bad file can't crash the whole mine.
            # Mirrors miner.py's robustness pattern.
            try:
                # Taken before extract_text reads the file. A stat after the
                # read can observe an append the extract never saw and then
                # skip that append forever.
                try:
                    source_mtime: Optional[float] = os.path.getmtime(source_file)
                except OSError:
                    source_mtime = None

                # Pass extract_mode="format" so format-mode idempotency is
                # scoped to its own drawer set — drawers from convo_miner /
                # project miner on the same source_file don't falsely
                # indicate "already mined" to the format miner. Per PR #1555
                # review (Copilot #11).
                if not dry_run and file_already_mined(
                    collection, source_file, check_mtime=True, extract_mode="format"
                ):
                    files_skipped += 1
                    continue

                text, status = extract_text(filepath)
                status_counts[status.name] += 1

                if status != ExtractionStatus.OK or not text:
                    if not dry_run:
                        _register_skip_sentinel_if_appropriate(
                            collection,
                            source_file,
                            wing,
                            agent,
                            status,
                            source_mtime=source_mtime,
                        )
                    print(f"  - [{i:4}/{len(files)}] {filepath.name[:50]:50} {status.name}")
                    continue

                # Thread the user's MempalaceConfig chunk parameters through
                # so format-mode mining honors their tuning. Per PR #1555
                # review (Gemini #3).
                chunks = chunk_text(
                    text,
                    source_file,
                    chunk_size=palace_config.chunk_size,
                    chunk_overlap=palace_config.chunk_overlap,
                    min_chunk_size=palace_config.min_chunk_size,
                )
                if not chunks:
                    if not dry_run:
                        _register_file(
                            collection, source_file, wing, agent, source_mtime=source_mtime
                        )
                    print(f"  - [{i:4}/{len(files)}] {filepath.name[:50]:50} EMPTY_AFTER_CHUNK")
                    continue

                # Route this drawer to a room — same detect_room miner.py
                # uses (folder match → filename match → content keyword
                # scoring → fallback "general"). Mirrors miner.py:904.
                room = detect_room(filepath, text, rooms, format_path)
                files_with_text += 1

                if dry_run:
                    print(f"    [DRY RUN] {filepath.name} -> {len(chunks)} drawers")
                    total_drawers += len(chunks)
                    files_mined += 1
                    if limit > 0 and files_mined >= limit:
                        break
                    continue

                drawers_added, skipped = _file_chunks_locked(
                    collection,
                    source_file,
                    chunks,
                    wing,
                    room,
                    agent,
                    source_mtime=source_mtime,
                    content=text,
                    source_dir_ino=source_directory_identity(filepath),
                )
                if skipped == PURGE_FAILED:
                    files_failed.append(source_file)
                    continue
                if skipped:
                    files_skipped += 1
                    continue

                total_drawers += drawers_added
                files_mined += 1
                print(f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")
                if limit > 0 and files_mined >= limit:
                    break
            except Exception as exc:
                # The loop continues, but the file is a failure: the summary
                # counts it and MineFileErrors makes the run exit non-zero.
                files_failed.append(source_file)
                logger.warning(
                    "mine_formats: error processing %s — %s: %s",
                    source_file,
                    type(exc).__name__,
                    str(exc)[:200],
                )
                print(
                    f"  ! [{i:4}/{len(files)}] {filepath.name[:50]:50} ERROR: {type(exc).__name__}"
                )
                continue
    except KeyboardInterrupt:
        print("\n  Mine interrupted by user (Ctrl-C).")
        raise
    else:
        # All files processed without interruption — compute cross-wing topic
        # tunnels linking this wing to others that share confirmed topics.
        # Mirrors the post-loop tunnel block in miner._mine_impl: tunnel-compute
        # failures must never fail a mine, so any exception is logged and
        # skipped quietly.
        if not dry_run:
            try:
                tunnels_added = _compute_topic_tunnels_for_wing(wing, config=palace_config)
                if tunnels_added:
                    print(f"\n  Topic tunnels: +{tunnels_added} cross-wing link(s)")
            except Exception as exc:
                logger.warning(
                    "mine_formats: topic tunnel computation skipped — %s: %s",
                    type(exc).__name__,
                    str(exc)[:200],
                )
                print(
                    f"\n  WARNING: topic tunnel computation skipped — {exc}",
                    file=sys.stderr,
                )

            # End-of-mine FTS5 integrity check (#1537). Mirrors _mine_impl;
            # raises MineValidationError to cmd_mine if PRAGMA quick_check
            # finds malformed FTS5 rows so a corrupted palace cannot silently
            # exit Done on the --mode extract path that bypasses _mine_impl.
            # Sits outside the per-file try/except in the for-loop body, so
            # caught per-file errors do not mask the integrity result.
            _validate_palace_fts5_after_mine(palace_path, writes_since=writes_at_start)
    finally:
        # Hook-spawned mines write a PID file that miner.py's
        # _cleanup_mine_pid_file() clears; we mirror that so format-mode
        # mines kicked off the same way don't leave a stale PID behind.
        try:
            from .miner import _cleanup_mine_pid_file
        except ImportError:
            _cleanup_mine_pid_file = None
        if _cleanup_mine_pid_file is not None:
            try:
                _cleanup_mine_pid_file()
            except Exception:
                logger.debug("mine_formats: _cleanup_mine_pid_file failed", exc_info=True)
        print_files_failed(files_failed)
        _print_mine_summary(
            files_seen=files_processed,
            files_with_text=files_with_text,
            files_skipped=files_skipped,
            files_errored=len(files_failed),
            total_drawers=total_drawers,
            status_counts=status_counts,
        )

    raise_if_files_failed(files_failed, files_processed)
