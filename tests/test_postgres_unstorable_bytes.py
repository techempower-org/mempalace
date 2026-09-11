"""Postgres write path must survive every byte class Postgres refuses (#411).

``backends/pgvector.py`` strips two classes before binding — NUL (#1829) and
lone UTF-16 surrogates (#1833) — because one stray byte in a mined transcript
otherwise aborts the whole mine. ``backends/postgres.py`` only ever grew the
NUL half (#417), and only over documents and top-level metadata values: ids,
nested metadata and lone surrogates all still reached the driver untouched.

These are pure in-memory tests of the scrub plus the serialization invariant
it exists to protect, so no live Postgres is needed.
"""

import json
import re

import pytest

from mempalace.backends import postgres as pg

# json.dumps(ensure_ascii=True) renders a lone surrogate as a \\udXXX escape and
# a NUL as a \\u0000 escape. Postgres rejects both on the ::jsonb cast
# ("unsupported Unicode escape sequence"), so the serialized form is what has
# to come out clean.
_NUL_ESCAPE = "\\u0000"
_LONE_SURROGATE_ESCAPE = re.compile(r"\\ud[89ab][0-9a-f]{2}(?!\\udc)", re.IGNORECASE)


def assert_jsonb_safe(metadata: dict) -> None:
    """Fail unless ``metadata`` survives the ``%s::jsonb`` bind Postgres does."""
    serialized = json.dumps(metadata)
    assert _NUL_ESCAPE not in serialized, f"NUL escape survived: {serialized!r}"
    assert not _LONE_SURROGATE_ESCAPE.search(serialized), (
        f"lone surrogate escape survived: {serialized!r}"
    )
    # psycopg encodes the bound parameter as UTF-8; a lone surrogate raises
    # UnicodeEncodeError there even when json.dumps was happy to escape it.
    json.dumps(metadata, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Lone surrogates — the half #417 never covered
# ---------------------------------------------------------------------------


def test_lone_surrogate_in_document_is_replaced_and_counted():
    """A lone surrogate in a document becomes U+FFFD, provenanced like NUL."""
    documents = ["clean", "tool output \ud800 mid-line"]
    metadatas = [{"wing": "test"}, {"wing": "test"}]

    pg._scrub_unstorable(documents=documents, metadatas=metadatas)

    assert documents[0] == "clean"
    assert "\ud800" not in documents[1]
    assert documents[1] == "tool output � mid-line"
    assert metadatas[1]["lone_surrogates_replaced"] == 1
    assert "lone_surrogates_replaced" not in metadatas[0]


def test_lone_surrogate_in_metadata_value_is_replaced():
    """Metadata carries transcript-derived strings too, so it needs the pass."""
    metadatas = [{"source_file": "/var/log/app\udfff.txt"}]

    pg._scrub_unstorable(metadatas=metadatas)

    assert metadatas[0]["source_file"] == "/var/log/app�.txt"
    assert_jsonb_safe(metadatas[0])


def test_valid_surrogate_pair_is_left_alone():
    """An astral character is legal UTF-8 — the scrub must not mangle emoji."""
    documents = ["a rocket \U0001f680 flies"]
    pg._scrub_unstorable(documents=documents)
    assert documents[0] == "a rocket \U0001f680 flies"


# ---------------------------------------------------------------------------
# ids — bound as the ON CONFLICT key and never scrubbed before
# ---------------------------------------------------------------------------


def test_ids_are_scrubbed():
    """``id`` is bound into a ``text`` column; pgvector scrubs it, we must too."""
    ids = ["clean_id", "bad\x00id", "surrogate\ud800id"]

    pg._scrub_unstorable(ids=ids)

    assert ids == ["clean_id", "bad�id", "surrogate�id"]


# ---------------------------------------------------------------------------
# nested metadata — json.dumps escapes the byte, the jsonb cast then rejects it
# ---------------------------------------------------------------------------


def test_nested_metadata_is_scrubbed():
    """A NUL nested in a list or dict still poisons the ``::jsonb`` cast.

    ``_replace_nul_bytes`` only walked top-level ``str`` values, so a NUL one
    level down serialized to a ``\\u0000`` escape and aborted the batch exactly
    as an unscrubbed top-level one would.
    """
    metadatas = [
        {
            "tags": ["ok", "bad\x00tag"],
            "nested": {"inner": "deep\x00value", "also": ["\ud800"]},
        }
    ]

    pg._scrub_unstorable(metadatas=metadatas)

    assert metadatas[0]["tags"] == ["ok", "bad�tag"]
    assert metadatas[0]["nested"]["inner"] == "deep�value"
    assert metadatas[0]["nested"]["also"] == ["�"]
    assert_jsonb_safe(metadatas[0])


def test_metadata_keys_are_scrubbed():
    """A NUL in a key is as fatal as one in a value on the jsonb cast."""
    metadatas = [{"bad\x00key": "value"}]

    pg._scrub_unstorable(metadatas=metadatas)

    assert "bad�key" in metadatas[0]
    assert_jsonb_safe(metadatas[0])


def test_non_string_scalars_pass_through_unchanged():
    """JSON scalars must survive the walk with their types intact."""
    metadatas = [{"n": 3, "f": 1.5, "b": True, "none": None}]

    pg._scrub_unstorable(metadatas=metadatas)

    assert metadatas[0] == {"n": 3, "f": 1.5, "b": True, "none": None}


# ---------------------------------------------------------------------------
# The #417 contract must survive unchanged
# ---------------------------------------------------------------------------


def test_nul_contract_from_417_is_preserved():
    """U+FFFD substitution plus a counted ``nul_bytes_replaced``, as shipped."""
    documents = ["clean text", "log\x00with\x00nuls"]
    metadatas = [{"wing": "test"}, {"wing": "test", "source_file": "/var/log/a\x00.txt"}]

    pg._scrub_unstorable(documents=documents, metadatas=metadatas)

    assert documents[1] == "log�with�nuls"
    assert metadatas[1]["nul_bytes_replaced"] == 2
    assert metadatas[1]["source_file"] == "/var/log/a�.txt"
    assert "nul_bytes_replaced" not in metadatas[0]


def test_clean_input_gains_no_provenance_keys():
    """Nothing to replace means nothing added — metadata stays verbatim."""
    documents = ["perfectly ordinary prose"]
    metadatas = [{"wing": "test"}]

    pg._scrub_unstorable(documents=documents, metadatas=metadatas)

    assert metadatas == [{"wing": "test"}]


# ---------------------------------------------------------------------------
# Write paths actually call it
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.executed.append((str(sql), params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.autocommit = None
        self.closed = 0

    def cursor(self):
        return _FakeCursor(self)


class _FakeMod:
    def __init__(self, conn):
        self._conn = conn

    def connect(self, dsn, **kwargs):
        return self._conn


def _real_sql():
    """Real psycopg SQL composition — these tests assert on bound parameters.

    The ``test-linux`` CI job installs mempalace without the ``postgres``
    extra, so psycopg is absent there and importing it at module scope would
    fail collection. Skip only the four bind-site tests that genuinely need
    SQL composition; the pure-logic scrub tests above run everywhere, which is
    where the behaviour under test actually lives.
    """
    return pytest.importorskip("psycopg.sql", reason="requires the postgres extra")


@pytest.fixture()
def fake_collection(monkeypatch):
    """A PostgresCollection wired to a fake connection, setup already done."""
    conn = _FakeConn()
    monkeypatch.setattr(pg, "_load_psycopg2", lambda: (_FakeMod(conn), _real_sql()))
    col = pg.PostgresCollection.__new__(pg.PostgresCollection)
    col.dsn = "postgresql://fake/db"
    col.table_name = "mempalace_drawers"
    col._conn = None
    col._vec_type = "vector"
    col._table_am = "heap"
    col._index_am = "hnsw"
    col._setup_done = True
    col._vector_index_ready = True
    col._rows_since_index_check = 0
    col._local_row_estimate = 0
    return col, conn


def test_update_path_binds_scrubbed_metadata(fake_collection):
    """``update()`` serializes metadata straight into a ``::jsonb`` bind too."""
    col, conn = fake_collection

    col.update(ids=["d1"], metadatas=[{"note": "bad\x00byte and \ud800"}])

    params = conn.executed[-1][1]
    payload = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert _NUL_ESCAPE not in payload
    assert not _LONE_SURROGATE_ESCAPE.search(payload)


def test_insert_path_binds_scrubbed_id(fake_collection):
    """A NUL in an id reaches the ``text`` bind unless ``add()`` scrubs it."""
    col, conn = fake_collection

    col.add(
        documents=["doc"],
        ids=["bad\x00id"],
        metadatas=[{"wing": "test"}],
        embeddings=[[0.0] * 384],
    )

    bound = conn.executed[-1][1]
    flat = [v for group in bound if isinstance(group, list) for v in group]
    assert "bad\x00id" not in flat
    assert "bad�id" in flat


# ---------------------------------------------------------------------------
# Drift detector — postgres.py must cover everything pgvector.py covers
# ---------------------------------------------------------------------------


HOSTILE = [
    "plain",
    "nul\x00inside",
    "\x00leading",
    "trailing\x00",
    "lone high \ud800 surrogate",
    "lone low \udfff surrogate",
    "both \x00 and \ud834 together",
    "valid pair \U0001f680 stays",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_scrub_covers_every_class_pgvector_covers(text):
    """Whatever pgvector removes, postgres must remove (#411's drift guard).

    Substitution differs on purpose — pgvector deletes the byte, this fork
    replaces it with U+FFFD and counts it (#417) — so the invariant compared
    here is *storability*, not equality: after our scrub, pgvector's own
    sanitizers must find nothing left to do.
    """
    from mempalace.backends.pgvector import _strip_nul
    from mempalace.config import strip_lone_surrogates

    documents = [text]
    pg._scrub_unstorable(documents=documents)
    scrubbed = documents[0]

    assert _strip_nul(scrubbed) == scrubbed, "a NUL survived our scrub"
    assert strip_lone_surrogates(scrubbed) == scrubbed, "a lone surrogate survived our scrub"


def test_get_binds_scrubbed_ids(fake_collection):
    """A stored id went through the scrub, so the lookup key must match it."""
    col, conn = fake_collection

    col.get(ids=["bad\x00id"])

    params = conn.executed[-1][1]
    assert "bad\x00id" not in params
    assert "bad�id" in params


def test_delete_binds_scrubbed_ids(fake_collection):
    """Deleting by the caller's original id must still reach the stored row."""
    col, conn = fake_collection

    col.delete(ids=["bad\x00id"])

    params = conn.executed[-1][1]
    assert "bad\x00id" not in params
    assert "bad�id" in params
