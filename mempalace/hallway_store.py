"""Hallway persistence — JSON file (default) or a postgres table (opt-in).

Why this module exists, measured on the production palace host 2026-09-10:

``hallways.json`` is growing fast enough that any single figure is stale on
arrival: **479 MB** (early evening) → **1,041,537,215 B / ~797K records /
21 wings** (20:03) → **1,142,562,893 B / 1,903,306 records / 50 wings**
(measured read-only at 23:0x the same night). No duplication — record count,
distinct ids and distinct (wing, sorted-pair) tuples all agree exactly, so
the growth is new wings plus the N(N-1)/2 pair combinatorics, not a writer
bug. Every hallway operation loads and JSON-parses the whole thing:

- ``list_hallways(wing=...)`` filters in Python *after* the full load, so the
  ``wing`` argument does not reduce the work at all. The daemon cannot
  fast-intercept it (palace-daemon#255): it runs under a 2 GB cgroup cap
  already sitting at 98.4%, and parsing a 1 GB JSON array materializes
  several GB of dicts.
- ``compute_hallways_for_wing`` loads it too, just to preserve four dynamics
  fields, and ``_compute_entity_tunnels_for_wing`` loads it a second time via
  ``list_hallways`` — so two parsed copies of the corpus are live at once,
  which is where the 1.6-4.1 GB RSS on a mine subprocess comes from.
- ``_save_hallways`` rewrites all 1.04 GB per mine that produces a hallway —
  O(total) per write, when the change is a handful of rows.

Sizing this honestly, because the issue's own numbers invite an overstatement:
measured at production scale (900K records / 396 MB synthetic) json.load is
3.1 s and json.dump 8.4 s, so the two-loads-plus-one-save per mine is
**~37 s of JSON I/O**, not the 29 minutes #442's comment reports for the
post-mine block. That 29 minutes is ``create_tunnel`` doing a full
``_load_tunnels`` + atomic ``_save_tunnels`` per call from inside a loop
(``palace_graph.py`` :840/:861, called at :1145 and :1278) — O(n^2) in the
tunnel count, against a *tunnels.json* that is only 837 KB at 2000 tunnels.
The sweep's open file was ``tunnels.json.tmp``, which says so directly.
Measured by the #474 lane, mechanism confirmed here. Moving hallways to
postgres removes the ~37 s and the RSS and unblocks the daemon's
fast-intercept; it does **not** fix the 29-minute mine.

The postgres store makes each of those proportional to the *wing* rather
than to the whole palace: an indexed ``WHERE wing = %s``, a wing-scoped
dynamics read, and a delete+insert of one wing.

**JSON remains the default.** The flag is ``hallway_backend`` (config.json)
or ``MEMPALACE_HALLWAY_BACKEND`` (env), and the production cutover is a
separate, deliberate step run after the migration is verified against real
data. Nothing here changes behaviour until someone opts in. That also keeps
the local-first promise intact: a chroma/sqlite install never needs postgres.

Connections are opened per operation rather than cached. Hallway reads and
writes are infrequent (a list call, one replace per wing per mine), so the
per-call connect is cheap — and it makes this store immune by construction
to the stale-cached-connection failure that #385 had to add a retry seam
for. The one high-volume caller, the migration script, passes its own
connection in and holds it open.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("mempalace_hallways")

HALLWAY_TABLE = "mempalace_hallways"

# Columns promoted out of the record because something queries or orders by
# them. Everything else rides in ``extra`` so a record round-trips losslessly
# and a newer writer's fields are not truncated by an older reader.
_PROMOTED = (
    "id",
    "wing",
    "entity_a",
    "entity_b",
    "co_occurrence_count",
    "rooms",
    "label",
    "created_at",
    "created_by",
)
# Accumulated through use; kept together so the recompute path can read just
# these without reading whole records.
_DYNAMICS = ("strength", "stability", "last_activated", "access_count")

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {HALLWAY_TABLE} (
    id                  text PRIMARY KEY,
    wing                text NOT NULL,
    entity_a            text NOT NULL,
    entity_b            text NOT NULL,
    co_occurrence_count integer NOT NULL DEFAULT 0,
    rooms               jsonb   NOT NULL DEFAULT '[]'::jsonb,
    label               text,
    created_at          text,
    created_by          text,
    dynamics            jsonb   NOT NULL DEFAULT '{{}}'::jsonb,
    extra               jsonb   NOT NULL DEFAULT '{{}}'::jsonb
)
"""
# ``created_at``/``last_activated`` stay ``text`` rather than ``timestamptz``
# on purpose: the records carry ISO-8601 strings and round-tripping them
# through a timestamp type normalizes offsets and sub-second precision, i.e.
# it rewrites the user's data to say something slightly different. ISO-8601
# UTC sorts correctly lexicographically and nothing range-queries these, so
# text costs nothing and keeps the values verbatim.

_WING_INDEX = f"CREATE INDEX IF NOT EXISTS {HALLWAY_TABLE}_wing_idx ON {HALLWAY_TABLE} (wing)"
_WING_COUNT_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {HALLWAY_TABLE}_wing_count_idx "
    f"ON {HALLWAY_TABLE} (wing, co_occurrence_count DESC)"
)


# ─────────────────────────────────────────────────────────────────────────────
# Record <-> row
# ─────────────────────────────────────────────────────────────────────────────


def _scrub(value: Any) -> Any:
    """Strip the two byte classes postgres refuses, recursively (#411).

    Entity names, labels and room names are all transcript-derived, so they
    carry exactly the NUL bytes and lone surrogates that abort a write. On a
    ~797K-record import one bad byte would otherwise take down the whole
    migration, which is #411's failure mode with a much larger blast radius.
    Reuses the backend's scrubber so the two cannot drift.
    """
    from .backends.postgres import _scrub_json_value

    return _scrub_json_value(value)


def _undefined_table_error(message: str = "relation does not exist"):
    """Build the driver's UndefinedTable error, or a stand-in if unavailable."""
    try:
        from psycopg import errors as pg_errors

        return pg_errors.UndefinedTable(message)
    except Exception:  # pragma: no cover - driver missing in a chroma-only install
        return RuntimeError(message)


def _is_undefined_table(exc: BaseException) -> bool:
    """True when the hallway table has not been created yet."""
    try:
        from psycopg import errors as pg_errors

        if isinstance(exc, pg_errors.UndefinedTable):
            return True
    except Exception:  # pragma: no cover - driver missing
        pass
    return "does not exist" in str(exc)


def _record_to_row(record: dict) -> dict:
    """Split a hallway record into promoted columns + dynamics + extra."""
    record = _scrub(dict(record))
    row = {name: record.get(name) for name in _PROMOTED}
    row["co_occurrence_count"] = int(row.get("co_occurrence_count") or 0)
    row["rooms"] = list(row.get("rooms") or [])
    row["dynamics"] = {k: record[k] for k in _DYNAMICS if k in record}
    row["extra"] = {k: v for k, v in record.items() if k not in _PROMOTED and k not in _DYNAMICS}
    return row


def _row_to_record(row) -> dict:
    """Reassemble the original record. Inverse of :func:`_record_to_row`."""
    if isinstance(row, dict):
        data = dict(row)
    else:
        data = dict(zip((*_PROMOTED, "dynamics", "extra"), row))
    record = {name: data.get(name) for name in _PROMOTED}
    record["rooms"] = list(record.get("rooms") or [])
    for source in ("dynamics", "extra"):
        blob = data.get(source) or {}
        if isinstance(blob, str):
            blob = json.loads(blob)
        record.update(blob)
    return {k: v for k, v in record.items() if not (k == "label" and v is None)}


# ─────────────────────────────────────────────────────────────────────────────
# Stores
# ─────────────────────────────────────────────────────────────────────────────


class JsonHallwayStore:
    """The historical store: one JSON file, loaded and rewritten whole.

    Kept as the default and as the fallback for non-postgres installs. Calls
    back into ``hallways`` at call time rather than importing its helpers at
    module scope, so the existing tests that monkeypatch
    ``hallways._get_hallway_file`` keep working unchanged.
    """

    def __init__(self, config=None):
        self.config = config

    def ensure_schema(self) -> None:
        return None

    def _all(self) -> list[dict]:
        from . import hallways

        return hallways._load_hallways(self.config)

    def _write(self, records: list[dict]) -> None:
        from . import hallways

        hallways._save_hallways(records, self.config)

    def list(self, wing: Optional[str] = None, limit=None, offset=None) -> list[dict]:
        records = self._all()
        if wing is not None:
            records = [h for h in records if h.get("wing") == wing]
        else:
            records = list(records)
        if offset:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return records

    def count(self, wing: Optional[str] = None) -> int:
        return len(self.list(wing=wing))

    def dynamics_for_wing(self, wing: str) -> dict:
        lookup: dict = {}
        for h in self._all():
            if h.get("wing") != wing:
                continue
            key = tuple(sorted([h.get("entity_a"), h.get("entity_b")]))
            lookup[key] = {k: h[k] for k in _DYNAMICS if k in h}
        return lookup

    def replace_wing(self, wing: str, records: list[dict]) -> None:
        preserved = [h for h in self._all() if h.get("wing") != wing]
        self._write(preserved + list(records))

    def delete(self, hallway_id: str) -> bool:
        records = self._all()
        filtered = [h for h in records if h.get("id") != hallway_id]
        if len(filtered) == len(records):
            return False
        self._write(filtered)
        return True


class PostgresHallwayStore:
    """Hallways in a postgres table: wing-scoped reads, wing-scoped writes."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._missing_table_warned = False

    # -- connection -------------------------------------------------------

    def _connect(self):
        from .backends.postgres import _load_psycopg2

        psycopg, _sql = _load_psycopg2()
        conn = psycopg.connect(self.dsn)
        conn.autocommit = True
        return conn

    def _run(self, statements, conn=None, fetch=None, atomic=False):
        """Execute ``(sql, params)`` pairs, optionally on a caller's connection."""
        owned = conn is None
        conn = conn or self._connect()
        # A caller-supplied connection owns its own transaction; silently
        # committing someone else's open work would be worse than not
        # grouping ours.
        atomic = atomic and owned
        try:
            if atomic:
                conn.autocommit = False
            cur = conn.cursor()
            result = None
            for sql, params in statements:
                cur.execute(sql, params)
            if fetch == "all":
                result = cur.fetchall()
            elif fetch == "one":
                result = cur.fetchone()
            if atomic:
                conn.commit()
            return result
        except Exception:
            if atomic:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001 - the socket may already be gone
                    logger.debug("hallways: rollback failed", exc_info=True)
            raise
        finally:
            if owned:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 — closing a dead socket is not an error
                    logger.debug("hallways: closing connection failed", exc_info=True)

    # -- schema -----------------------------------------------------------

    def ensure_schema(self, conn=None) -> None:
        """Create the table and indexes if absent. Never drops, never rewrites."""
        self._run(
            [(_CREATE_TABLE, None), (_WING_INDEX, None), (_WING_COUNT_INDEX, None)],
            conn=conn,
        )

    def _read(self, statements, conn=None, fetch=None, empty=None):
        """Run a read, tolerating a table that the migration has not created.

        Before ``migrate_hallways`` runs there is no table. Raising an opaque
        UndefinedTable out of ``list_hallways`` would take down a mine over
        an ordering mistake; returning a bare empty result would be
        indistinguishable from "this wing has no hallways" and send whoever
        is debugging it somewhere else entirely. So: empty result, and say
        exactly which command is missing -- once per store, not per read.
        Mirrors the daemon's fast-intercept note (palace-daemon#255).
        """
        try:
            return self._run(statements, conn=conn, fetch=fetch)
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is the known case
            if not _is_undefined_table(exc):
                raise
            if not self._missing_table_warned:
                self._missing_table_warned = True
                logger.warning(
                    "hallway_backend=postgres but the %s table does not exist; "
                    "returning no hallways. Run `python -m mempalace.migrate_hallways` "
                    "to import hallways.json, then keep hallway_backend=postgres.",
                    HALLWAY_TABLE,
                )
            return empty

    # -- reads ------------------------------------------------------------

    _SELECT_COLUMNS = ", ".join((*_PROMOTED, "dynamics", "extra"))

    def list(self, wing: Optional[str] = None, limit=None, offset=None, conn=None) -> list[dict]:
        sql = f"SELECT {self._SELECT_COLUMNS} FROM {HALLWAY_TABLE}"
        params: list = []
        if wing is not None:
            sql += " WHERE wing = %s"
            params.append(wing)
        sql += " ORDER BY co_occurrence_count DESC, id"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))
        if offset:
            sql += " OFFSET %s"
            params.append(int(offset))
        rows = self._read([(sql, params)], conn=conn, fetch="all", empty=[]) or []
        return [_row_to_record(r) for r in rows]

    def count(self, wing: Optional[str] = None, conn=None) -> int:
        sql = f"SELECT COUNT(*) FROM {HALLWAY_TABLE}"
        params: list = []
        if wing is not None:
            sql += " WHERE wing = %s"
            params.append(wing)
        row = self._read([(sql, params)], conn=conn, fetch="one")
        return int(row[0]) if row else 0

    def dynamics_for_wing(self, wing: str, conn=None) -> dict:
        """The four accumulated fields for one wing, keyed by sorted entity pair.

        Keyed the same way ``_hallway_id`` hashes — sorted — so a record
        stored with the pair in the other order still matches and keeps its
        accumulated weights instead of silently resetting them on recompute.
        """
        sql = f"SELECT entity_a, entity_b, dynamics FROM {HALLWAY_TABLE} WHERE wing = %s"
        rows = self._read([(sql, [wing])], conn=conn, fetch="all", empty=[]) or []
        lookup: dict = {}
        for entity_a, entity_b, dynamics in rows:
            if isinstance(dynamics, str):
                dynamics = json.loads(dynamics)
            key = tuple(sorted([entity_a, entity_b]))
            lookup[key] = {k: v for k, v in (dynamics or {}).items() if k in _DYNAMICS}
        return lookup

    # -- writes -----------------------------------------------------------

    _INSERT = (
        f"INSERT INTO {HALLWAY_TABLE} "
        "(id, wing, entity_a, entity_b, co_occurrence_count, rooms, label, "
        " created_at, created_by, dynamics, extra) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET "
        "wing = EXCLUDED.wing, entity_a = EXCLUDED.entity_a, "
        "entity_b = EXCLUDED.entity_b, "
        "co_occurrence_count = EXCLUDED.co_occurrence_count, "
        "rooms = EXCLUDED.rooms, label = EXCLUDED.label, "
        "created_at = EXCLUDED.created_at, created_by = EXCLUDED.created_by, "
        "dynamics = EXCLUDED.dynamics, extra = EXCLUDED.extra"
    )

    @staticmethod
    def _insert_params(record: dict) -> list:
        row = _record_to_row(record)
        return [
            row["id"],
            row["wing"],
            row["entity_a"],
            row["entity_b"],
            row["co_occurrence_count"],
            json.dumps(row["rooms"], ensure_ascii=False),
            row["label"],
            row["created_at"],
            row["created_by"],
            json.dumps(row["dynamics"], ensure_ascii=False),
            json.dumps(row["extra"], ensure_ascii=False),
        ]

    def upsert_many(self, records, conn=None) -> int:
        statements = [(self._INSERT, self._insert_params(r)) for r in records]
        if not statements:
            return 0
        self._run(statements, conn=conn)
        return len(statements)

    def replace_wing(self, wing: str, records: list[dict], conn=None) -> None:
        """Swap one wing's rows in ONE transaction. Other wings are untouched.

        Both halves of that matter, and the JSON path had both: it preserved
        other wings (scope) *and* wrote a temp file then ``os.replace``d it
        (atomicity), so a wing could never be observed half-written. A DELETE
        followed by N autocommit INSERTs keeps the scope and quietly drops the
        atomicity -- a crash after the DELETE leaves the wing EMPTY, a crash
        midway leaves it partial, and the recompute that would repair it only
        runs on the next mine of that wing. So the whole swap commits or rolls
        back together.

        When the caller passes ``conn`` the caller's transaction governs.

        The delete runs even when ``records`` is empty: a wing whose pairs all
        fell below ``min_count`` must end up with no rows, not with its
        previous snapshot left behind.
        """
        statements = [(f"DELETE FROM {HALLWAY_TABLE} WHERE wing = %s", [wing])]
        statements.extend((self._INSERT, self._insert_params(r)) for r in records)
        self._run(statements, conn=conn, atomic=True)

    def delete(self, hallway_id: str, conn=None) -> bool:
        row = self._run(
            [
                (
                    f"DELETE FROM {HALLWAY_TABLE} WHERE id = %s RETURNING 1",
                    [hallway_id],
                )
            ],
            conn=conn,
            fetch="one",
        )
        return row is not None


# ─────────────────────────────────────────────────────────────────────────────
# Selection
# ─────────────────────────────────────────────────────────────────────────────


def get_hallway_store(config=None):
    """Return the configured hallway store. JSON unless postgres is opted into.

    A postgres selection with no DSN falls back to JSON with a warning rather
    than raising: a misconfigured flag must not take down a mine, and silently
    returning an empty store would look like "this wing has no hallways",
    which is worse than slow.
    """
    if config is None:
        from .config import MempalaceConfig

        config = MempalaceConfig()

    backend = (getattr(config, "hallway_backend", None) or "json").strip().lower()
    if backend != "postgres":
        return JsonHallwayStore(config)

    dsn = getattr(config, "postgres_dsn", None)
    if not dsn:
        logger.warning(
            "hallway_backend=postgres but no postgres DSN is configured "
            "(MEMPALACE_POSTGRES_DSN / postgres_dsn); falling back to the JSON "
            "hallway file at %s",
            getattr(config, "hallway_file", "<unknown>"),
        )
        return JsonHallwayStore(config)
    return PostgresHallwayStore(dsn)
