"""Hallway storage backend — postgres table behind a config flag (#442).

`~/.mempalace/hallways.json` on the production palace host was measured at
**1,041,537,215 bytes (1.04 GB)** on 2026-09-10, holding ~797K records across
21 wings, and it is growing (479 MB one week earlier). Three consequences,
all measured on production:

- ``_load_hallways`` JSON-parses the whole file on *every* call, which
  materializes several GB of Python dicts. The daemon runs under a 2 GB
  cgroup cap already sitting at 98.4%, so ``list_hallways`` cannot be
  fast-intercepted there at all (palace-daemon#255).
- the ``wing`` argument does not reduce the work: a wing-scoped call pays
  exactly the same cost as an unscoped one, because the filter is a Python
  list comprehension after the full load.
- ``_save_hallways`` rewrites the entire file per mine, O(total) per write,
  and the post-mine block parses the corpus twice over (once in
  ``compute_hallways_for_wing``, once via ``list_hallways`` in the entity
  tunnel step), so two copies are live at once — the 1.6-4.1 GB RSS. At
  production scale that is ~37 s of JSON I/O per mine. It is *not* the 29
  minutes #442 reports for the whole post-mine block: that is
  ``create_tunnel``'s per-call persist in palace_graph, measured separately.

These tests pin the store seam. The JSON path must stay byte-for-byte the
default until the lead runs the cutover, so the first test here is that
nothing changes without an explicit opt-in.
"""

import json

import pytest

from mempalace import hallway_store as hs


# ---------------------------------------------------------------------------
# Backend selection — JSON stays the default until the cutover
# ---------------------------------------------------------------------------


class _Cfg:
    """Minimal config stand-in; only the attributes the store reads."""

    def __init__(self, backend="json", dsn="postgresql://fake/db", hallway_file="/tmp/x.json"):
        self.hallway_backend = backend
        self.postgres_dsn = dsn
        self.hallway_file = hallway_file


def test_json_is_the_default_backend():
    """No opt-in means no behaviour change. The cutover is a separate step."""
    store = hs.get_hallway_store(_Cfg(backend="json"))
    assert isinstance(store, hs.JsonHallwayStore)


def test_postgres_selected_only_by_explicit_flag():
    store = hs.get_hallway_store(_Cfg(backend="postgres"))
    assert isinstance(store, hs.PostgresHallwayStore)


def test_postgres_without_a_dsn_falls_back_to_json_loudly(caplog):
    """A misconfigured flag must not silently lose hallways, or crash a mine."""
    import logging

    with caplog.at_level(logging.WARNING, logger="mempalace_hallways"):
        store = hs.get_hallway_store(_Cfg(backend="postgres", dsn=None))

    assert isinstance(store, hs.JsonHallwayStore)
    assert any("dsn" in r.getMessage().lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Record <-> row round trip must be lossless
# ---------------------------------------------------------------------------


FULL_RECORD = {
    "id": "hallway_w_a_b_deadbeef",
    "wing": "w",
    "entity_a": "Aya",
    "entity_b": "Lumi",
    "co_occurrence_count": 47,
    "rooms": ["diary", "letters"],
    "label": "Aya ↔ Lumi (co-occur in 47 drawers)",
    "created_at": "2026-09-10T00:00:00+00:00",
    "created_by": "auto",
    "strength": 0.73,
    "stability": 0.5,
    "last_activated": "2026-09-10T01:00:00+00:00",
    "access_count": 12,
}


def test_record_row_round_trip_is_lossless():
    """Verbatim-always applies to derived records too — lose no field."""
    row = hs._record_to_row(FULL_RECORD)
    back = hs._row_to_record(row)
    assert back == FULL_RECORD


def test_unknown_fields_survive_the_round_trip():
    """A record written by a newer version must not be truncated by an older one."""
    record = dict(FULL_RECORD, some_future_field={"nested": [1, 2]}, another="x")
    back = hs._row_to_record(hs._record_to_row(record))
    assert back == record


def test_round_trip_preserves_dynamics_exactly():
    """Dynamics are accumulated through use; silently resetting them is data loss."""
    back = hs._row_to_record(hs._record_to_row(FULL_RECORD))
    for field in ("strength", "stability", "last_activated", "access_count"):
        assert back[field] == FULL_RECORD[field]


# ---------------------------------------------------------------------------
# Unstorable bytes — entity names are transcript-derived (#411)
# ---------------------------------------------------------------------------


def test_unstorable_bytes_in_entity_names_are_scrubbed():
    """Entity names come from mined drawer metadata, so they carry stray bytes.

    Postgres refuses NUL and lone surrogates outright. An 800K-record import
    that aborts on record 400,000 because one transcript had a stray byte is
    the #411 failure mode with a much bigger blast radius, so the scrub
    happens at this boundary too.
    """
    record = dict(
        FULL_RECORD,
        id="hallway_w_a\x00b",
        entity_a="Bad\x00Name",
        entity_b="Lone\ud800Surrogate",
        label="tool output \x00 here",
        rooms=["ok", "bad\x00room"],
    )
    row = hs._record_to_row(record)

    flat = json.dumps(row, default=str)
    assert "\\u0000" not in flat
    assert "\x00" not in flat
    flat.encode("utf-8")  # a lone surrogate would raise here


# ---------------------------------------------------------------------------
# The wing filter must be in SQL, not a Python list comprehension
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return self.conn.rows

    def fetchone(self):
        return self.conn.rows[0] if self.conn.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows=None):
        self.executed = []
        self.rows = rows or []
        self.autocommit = None
        self.closed = 0
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def pg_store(monkeypatch):
    def _make(rows=None):
        conn = _FakeConn(rows=rows)
        store = hs.PostgresHallwayStore(dsn="postgresql://fake/db")
        monkeypatch.setattr(store, "_connect", lambda: conn)
        return store, conn

    return _make


def test_wing_filter_is_pushed_into_sql(pg_store):
    """This is the whole point: a wing-scoped call must not read every row."""
    store, conn = pg_store(rows=[])

    store.list(wing="kiyo")

    sql, params = conn.executed[-1]
    assert "WHERE wing = %s" in sql
    assert params[0] == "kiyo"
    assert "SELECT" in sql


def test_list_applies_a_limit_so_an_unscoped_call_is_bounded(pg_store):
    """797K records is not a sane payload at any speed — page it."""
    store, conn = pg_store(rows=[])

    store.list(limit=50, offset=100)

    sql, params = conn.executed[-1]
    assert "LIMIT %s" in sql and "OFFSET %s" in sql
    assert 50 in params and 100 in params


def test_list_orders_by_co_occurrence_so_the_first_page_is_the_strongest(pg_store):
    store, conn = pg_store(rows=[])
    store.list(wing="kiyo", limit=10)
    sql, _ = conn.executed[-1]
    assert "ORDER BY co_occurrence_count DESC" in sql


def test_dynamics_for_wing_reads_only_that_wing(pg_store):
    """The recompute path loaded all 797K records just to preserve 4 fields."""
    store, conn = pg_store(rows=[])

    store.dynamics_for_wing("kiyo")

    sql, params = conn.executed[-1]
    assert "WHERE wing = %s" in sql
    assert params[0] == "kiyo"


def test_dynamics_lookup_key_is_the_sorted_entity_pair(pg_store):
    """Must match _hallway_id's sorted-pair symmetry or dynamics are lost."""
    store, conn = pg_store(
        rows=[("Lumi", "Aya", {"strength": 0.9, "stability": 0.4, "access_count": 3})]
    )

    lookup = store.dynamics_for_wing("kiyo")

    assert ("Aya", "Lumi") in lookup
    assert lookup[("Aya", "Lumi")]["strength"] == 0.9


def test_replace_wing_touches_only_that_wing(pg_store):
    """Other wings' records must survive, as the JSON path preserved them."""
    store, conn = pg_store()

    store.replace_wing("kiyo", [FULL_RECORD])

    statements = [s for s, _ in conn.executed]
    deletes = [s for s in statements if s.startswith("DELETE")]
    assert len(deletes) == 1
    assert "WHERE wing = %s" in deletes[0]
    assert any("INSERT INTO" in s for s in statements)


def test_replace_wing_with_no_records_still_clears_the_wing(pg_store):
    """A wing whose hallways all fell below min_count must end up empty."""
    store, conn = pg_store()

    store.replace_wing("kiyo", [])

    statements = [s for s, _ in conn.executed]
    assert any(s.startswith("DELETE") for s in statements)
    assert not any("INSERT INTO" in s for s in statements)


def test_delete_reports_whether_a_row_went(pg_store):
    store, conn = pg_store(rows=[(1,)])
    assert store.delete("hallway_w_a_b_deadbeef") is True
    sql, params = conn.executed[-1]
    assert sql.startswith("DELETE")
    assert params[0] == "hallway_w_a_b_deadbeef"


def test_schema_is_created_idempotently(pg_store):
    """ensure_schema runs on a live palace; it must never drop or rewrite."""
    store, conn = pg_store()

    store.ensure_schema()

    statements = " ".join(s for s, _ in conn.executed)
    assert "CREATE TABLE IF NOT EXISTS" in statements
    assert "CREATE INDEX IF NOT EXISTS" in statements
    assert "DROP" not in statements.upper()


def test_the_store_does_not_normalize_wing_names(pg_store):
    """Wing matching stays exact string equality, as the JSON path had it.

    ``hallways.py`` never calls ``normalize_wing_name`` — zero references in
    the module — so a wing spelled differently from the drawers' metadata
    matches nothing and the caller gets silence, not duplicates (measured by
    the #474 lane). Normalizing here would be an improvement *and* a
    behaviour change: queries that used to return nothing would start
    returning rows. This storage move is not the place to make it, so the
    store passes the caller's wing through verbatim and this test says so
    out loud rather than leaving it to be discovered.
    """
    store, conn = pg_store(rows=[])

    store.list(wing="Kiyo-XHCI-Fix")
    _, params = conn.executed[-1]
    assert params[0] == "Kiyo-XHCI-Fix", "wing must reach SQL exactly as given"

    store.replace_wing("Kiyo-XHCI-Fix", [])
    delete_sql, delete_params = conn.executed[-1]
    assert delete_sql.startswith("DELETE")
    assert delete_params[0] == "Kiyo-XHCI-Fix"
