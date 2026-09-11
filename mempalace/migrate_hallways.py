"""Move ``hallways.json`` into the postgres hallway table (#442).

Idempotent, resumable, streaming, with a dry run. Every one of those is
forced by the size of the thing being moved: the production file is
**1,041,537,215 bytes / ~797K records** (palace host, 2026-09-10).

Streaming is the load-bearing one. ``json.load`` on that file materializes
several GB of Python dicts — the exact cost this migration exists to
eliminate — so the reader here yields one record at a time out of a small
buffer, using ``json.JSONDecoder.raw_decode`` rather than a third-party
streaming parser. No new dependency, which keeps the local-first install
unchanged.

Run it with ``python -m mempalace.migrate_hallways --help``. The cutover
itself — flipping ``hallway_backend`` to ``postgres`` — is deliberately NOT
part of this script: import first, verify against real data, flip second.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Iterator, Optional

logger = logging.getLogger("mempalace_hallways")

DEFAULT_BATCH_SIZE = 500
_DEFAULT_CHUNK = 1 << 20  # 1 MiB of text per read


class MigrationStateMismatch(RuntimeError):
    """Resume state does not describe the file we were handed."""


# ─────────────────────────────────────────────────────────────────────────────
# Streaming reader
# ─────────────────────────────────────────────────────────────────────────────


def _find_array_start(handle, chunk_size: int) -> tuple[str, int]:
    """Return (buffer, index) positioned at the first record-array element.

    Accepts both persisted shapes, matching ``hallways._load_hallways``: the
    wrapped ``{"schema_version": N, "hallways": [...]}`` and the older bare
    ``[...]``.
    """
    buf = ""
    while True:
        stripped = buf.lstrip()
        if stripped.startswith("["):
            return buf, buf.index("[") + 1
        marker = buf.find('"hallways"')
        if marker != -1:
            bracket = buf.find("[", marker)
            if bracket != -1:
                return buf, bracket + 1
        chunk = handle.read(chunk_size)
        if not chunk:
            if not buf.strip():
                return "", -1  # empty file
            raise ValueError("hallways file contains no record array")
        buf += chunk


def iter_hallway_records(path: str, chunk_size: int = _DEFAULT_CHUNK) -> Iterator[dict]:
    """Yield hallway records one at a time without loading the file.

    Text mode is deliberate: Python's text IO will not split a multi-byte
    character across two ``read()`` calls, so entity names in any script
    survive chunking. ``raw_decode`` returning a ``ValueError`` is ambiguous
    between "buffer ends mid-record" and "the file is malformed", so it is
    only treated as malformed once the file is exhausted.
    """
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8") as handle:
        buf, idx = _find_array_start(handle, chunk_size)
        if idx == -1:
            return
        exhausted = False
        while True:
            # Compact only once the consumed prefix is worth the copy. Slicing
            # the buffer after *every* record makes this quadratic in the
            # chunk size — measured at 6x slower on a 201 MB file — because
            # each of the ~2000 records in a 1 MiB chunk copies the remainder.
            if idx > chunk_size:
                buf = buf[idx:]
                idx = 0
            while idx < len(buf) and buf[idx] in " \t\r\n,":
                idx += 1
            if idx < len(buf) and buf[idx] == "]":
                return  # the only clean exit: we saw the array close
            if idx >= len(buf):
                if exhausted:
                    # Ran out of input without ever seeing "]". The file was
                    # cut between records — killed transfer, full disk,
                    # interrupted write. Returning here would report a short
                    # read as a complete one, and migrate() would then record
                    # that count as a finished resume point: silent tail loss
                    # on a 1 GB import. Mid-record truncation already raises
                    # below; this is the boundary case that did not.
                    raise ValueError(
                        "hallways file ends with an unterminated record array "
                        "(no closing ']'): the file is truncated. Refusing to "
                        "report a partial read as complete."
                    )
                chunk = handle.read(chunk_size)
                if not chunk:
                    exhausted = True
                    continue
                buf = buf[idx:] + chunk
                idx = 0
                continue
            try:
                record, end = decoder.raw_decode(buf, idx)
            except ValueError:
                # Ambiguous: either the buffer ends mid-record, or the file is
                # malformed. Only the second is true once reads come up empty.
                chunk = handle.read(chunk_size)
                if not chunk:
                    raise ValueError(
                        f"hallways file is truncated or malformed near byte offset "
                        f"{idx} of the current buffer; the record array does not parse"
                    ) from None
                buf = buf[idx:] + chunk
                idx = 0
                continue
            yield record
            idx = end


# ─────────────────────────────────────────────────────────────────────────────
# Resume state
# ─────────────────────────────────────────────────────────────────────────────


def _source_fingerprint(path: str) -> dict:
    """Cheap identity for the source file: size + mtime.

    Not a content hash on purpose — hashing 1 GB costs a full read, which is
    most of what resuming exists to avoid. Size alone misses a same-length
    rewrite, so mtime carries the rest. Both are O(1) from one stat.
    """
    stat = os.stat(path)
    return {
        "source_path": os.path.abspath(path),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
    }


def _write_state(state_path: str, source_path: str, imported: int) -> None:
    payload = _source_fingerprint(source_path)
    payload["imported"] = int(imported)
    directory = os.path.dirname(os.path.abspath(state_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, state_path)


def _read_state(state_path: str, source_path: str) -> int:
    """Return how many records a previous run committed, or 0.

    Refuses to resume against a file whose size changed: the resume point is
    an ordinal into this file's record sequence, so applying it to different
    content would skip the wrong records and silently under-import.
    """
    if not os.path.exists(state_path):
        return 0
    try:
        with open(state_path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("hallway migration: unreadable state at %s; starting over", state_path)
        return 0
    current = _source_fingerprint(source_path)
    changed = [
        field
        for field in ("source_size", "source_mtime_ns")
        # A state file written before mtime was tracked has no mtime to
        # compare; fall back to size alone rather than refusing every
        # in-flight resume.
        if field in saved and saved.get(field) != current[field]
    ]
    if changed:
        raise MigrationStateMismatch(
            f"resume state at {state_path} does not describe {source_path} any more "
            f"({', '.join(changed)} differ: recorded "
            f"{ {f: saved.get(f) for f in changed} }, now "
            f"{ {f: current[f] for f in changed} }). The saved position is an ordinal "
            "into the record sequence, so resuming would skip the wrong records. "
            "Re-run with --restart to import from the beginning (the upsert makes "
            "that safe)."
        )
    return int(saved.get("imported") or 0)


# ─────────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────────


def _has_unstorable_bytes(record: dict) -> bool:
    blob = json.dumps(record, ensure_ascii=False, default=str)
    return "\x00" in blob or any("\ud800" <= ch <= "\udfff" for ch in blob)


# ─────────────────────────────────────────────────────────────────────────────
# Entity classes — report the corpus, never edit it
# ─────────────────────────────────────────────────────────────────────────────

# Hallway entities are harvested from drawer metadata, and most of them are
# not names. Measured read-only against the live 1.14 GB store on the palace
# host (2026-09-10): of 154,692 distinct entities only 12,484 — 8% — are
# word-shaped, while identifier/path/url/template together are 47%. The very
# first record in the file links the entity ``${this.baseUrl}/health``.
#
# Whether that belongs in the palace is a judgement call about the corpus,
# not about storage, so this module reports the mix and filters NOTHING. A
# silent filter would be an irreversible edit to the user's data, made by the
# tool that was only asked to move it — and "we never summarize, we never
# paraphrase" applies to deciding which of someone's entities were worth
# keeping. The class table goes in the dry-run output so the decision is
# made deliberately, with numbers, at cutover.

_TEMPLATE_RE = re.compile(r"\$\{|\{\{|%[sd]\b|<%|\$\(")
_URL_RE = re.compile(r"://")
_PATH_RE = re.compile(r"[/\\]")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_CAMEL_OR_SNAKE_RE = re.compile(r"[a-z][A-Z]|_")
_WORDS_RE = re.compile(r"^[^\W\d_][\w'\-]*(?: [^\W\d_][\w'\-]*){0,5}$", re.UNICODE)


def classify_entity(entity) -> str:
    """Bucket one entity name. ``other`` is a catch-all, not a junk verdict.

    Deliberately ordered most-specific first: a template that contains a
    slash is a template, not a path.
    """
    if not entity or not isinstance(entity, str):
        return "empty"
    if _TEMPLATE_RE.search(entity):
        return "template"
    if _URL_RE.search(entity):
        return "url"
    if _PATH_RE.search(entity):
        return "path"
    if _IDENTIFIER_RE.match(entity) and _CAMEL_OR_SNAKE_RE.search(entity):
        return "identifier"
    if _WORDS_RE.match(entity):
        return "word"
    return "other"


def migrate(
    source_path: str,
    *,
    store=None,
    config=None,
    dry_run: bool = False,
    restart: bool = False,
    state_path: Optional[str] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    conn=None,
    progress_every: int = 50_000,
) -> dict:
    """Import ``source_path`` into the hallway table. Returns a summary dict."""
    # A dry run reads and reports; it opens nothing and writes nothing, so it
    # must not require a configured postgres store. Demanding one would make
    # the safe rehearsal harder to run than the real thing, which is exactly
    # backwards for the step whose job is to de-risk the other one.
    if store is None and not dry_run:
        from .hallway_store import PostgresHallwayStore, get_hallway_store

        store = get_hallway_store(config)
        if not isinstance(store, PostgresHallwayStore):
            raise RuntimeError(
                "hallway migration needs the postgres store. Set "
                "MEMPALACE_HALLWAY_BACKEND=postgres and a postgres DSN for the "
                "migration run; the palace's own hallway_backend can stay 'json' "
                "until the import is verified."
            )
    state_path = state_path or (source_path + ".migration-state.json")

    skip = 0 if (restart or dry_run) else _read_state(state_path, source_path)

    summary = {
        "source": os.path.abspath(source_path),
        "total": 0,
        "imported": 0,
        "skipped": skip,
        "scrubbed": 0,
        "by_wing": {},
        "entity_classes": {},
        "dry_run": dry_run,
    }

    if not dry_run:
        if _accepts_conn(store.ensure_schema):
            store.ensure_schema(conn=conn)
        else:
            store.ensure_schema()

    batch: list[dict] = []

    def _flush() -> None:
        if not batch:
            return
        if _accepts_conn(store.upsert_many):
            store.upsert_many(batch, conn=conn)
        else:
            store.upsert_many(batch)
        summary["imported"] += len(batch)
        batch.clear()
        _write_state(state_path, source_path, summary["skipped"] + summary["imported"])

    for index, record in enumerate(iter_hallway_records(source_path)):
        summary["total"] += 1
        wing = record.get("wing") or "(none)"
        summary["by_wing"][wing] = summary["by_wing"].get(wing, 0) + 1
        for entity in (record.get("entity_a"), record.get("entity_b")):
            klass = classify_entity(entity)
            summary["entity_classes"][klass] = summary["entity_classes"].get(klass, 0) + 1
        if _has_unstorable_bytes(record):
            summary["scrubbed"] += 1
        if dry_run or index < skip:
            continue
        batch.append(record)
        if len(batch) >= batch_size:
            _flush()
        if progress_every and summary["imported"] and summary["imported"] % progress_every == 0:
            logger.info(
                "hallway migration: %s imported (%s skipped)",
                summary["imported"],
                summary["skipped"],
            )
    _flush()
    return summary


def _accepts_conn(func) -> bool:
    try:
        import inspect

        return "conn" in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mempalace.migrate_hallways",
        description="Import hallways.json into the postgres hallway table (#442).",
        epilog=(
            "This imports only. Flipping hallway_backend to 'postgres' is a "
            "separate, deliberate step taken after the import is verified."
        ),
    )
    parser.add_argument("source", nargs="?", help="path to hallways.json (default: from config)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and report; touch neither the schema nor any row",
    )
    parser.add_argument("--restart", action="store_true", help="ignore saved resume state")
    parser.add_argument(
        "--state", help="resume-state file (default: <source>.migration-state.json)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="rows per upsert round trip"
    )
    return parser


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    source = args.source
    if not source:
        from .config import MempalaceConfig

        source = MempalaceConfig().hallway_file

    try:
        summary = migrate(
            source,
            dry_run=args.dry_run,
            restart=args.restart,
            state_path=args.state,
            batch_size=args.batch_size,
        )
    except MigrationStateMismatch as exc:
        print(f"refusing to resume: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"hallway migration failed: {exc}", file=sys.stderr)
        return 1

    label = "would import" if summary["dry_run"] else "imported"
    print(f"source        {summary['source']}")
    print(f"records read  {summary['total']}")
    print(f"{label:<13} {summary['imported'] if not summary['dry_run'] else summary['total']}")
    if summary["skipped"]:
        print(f"resumed past  {summary['skipped']}")
    if summary["scrubbed"]:
        print(
            f"scrubbed      {summary['scrubbed']} record(s) carried NUL or lone-surrogate "
            "bytes postgres cannot store; replaced with U+FFFD"
        )
    print("by wing:")
    for wing, count in sorted(summary["by_wing"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:>9}  {wing}")
    if summary["entity_classes"]:
        total_slots = sum(summary["entity_classes"].values()) or 1
        print("entity classes (one count per entity slot; nothing is filtered):")
        for klass, count in sorted(summary["entity_classes"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:>9}  {klass:<11} {100 * count / total_slots:5.1f}%")
        print(
            "  note: code-shaped entities (identifier/path/url/template) are "
            "imported as-is.\n"
            "        Filtering them is a separate, deliberate decision — the "
            "table makes\n"
            "        them cheap to hold and cheap to query, so it stays open "
            "after import."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
