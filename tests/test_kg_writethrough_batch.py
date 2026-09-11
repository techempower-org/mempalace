"""KG write-through commits once per batch, not once per drawer.

palace-daemon#265, from the design on #251. Measured on the palace host:
a projects-mode mine wrote **4,832 drawers in 69 minutes (~1.2 drawers/s)**
with `pg_stat_activity` showing one
``cypher('mempalace_kg', … MERGE (d:Drawer …`` statement per drawer; and
after the tunnel recompute was removed (#264) a single changed 4,627-line
file still spent ~4+ minutes in write-through after its 690 drawers were
already filed.

The mechanism is not the MERGEs themselves, it is the commits.
``add_mention``/``_run_cypher`` default ``commit=True``, ``kg.commit()``
exists for bulk callers, and ``backfill_age`` already uses
``commit=False`` + one commit per batch — but ``kg_writethrough`` never
passes ``commit``, so a 1000-drawer batch can issue up to 100,000
individual transaction commits.

The blocker was the contract: ``hook(drawer_id, document, metadata)`` is
per-drawer, so no caller could commit once for a batch. Stage A adds a
batch hook alongside it. Stages B (dedup entity MERGEs) and C (UNWIND,
pending an AGE 1.6.0 dialect probe) stay as design.
"""

import pytest

from mempalace import kg_writethrough as kgw
from mempalace.backends import postgres as pg


class _Entity:
    def __init__(self, name, type="unknown", count=1):
        self.name = name
        self.type = type
        self.count = count


class _RecordingKG:
    """Models the real KG's TRANSACTION semantics, not just its call surface.

    An earlier version of this fake got that distinction wrong, and the
    test built on it certified a property production does not have.
    ``_run_cypher`` routes through ``_with_conn_retry``, which on a
    statement-level DB error calls ``_rollback_quietly()`` ->
    ``conn.rollback()``. Under ``commit=False`` that discards **every
    mention accumulated in the batch so far**, not only the failing one.

    So ``pending`` is dropped on a DB-class failure here, exactly as
    postgres drops it. ``fail_is_db_error=False`` models a failure that
    does not reach the driver and therefore does not roll back.
    """

    def __init__(self, fail_on_entity=None, fail_is_db_error=True):
        self.committed = []
        self.pending = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_entity = fail_on_entity
        self.fail_is_db_error = fail_is_db_error

    @property
    def mentions(self):
        """Only what survived — committed edges, not attempted ones."""
        return self.committed

    def add_mention(
        self, drawer_id, entity_name, *, entity_type="unknown", count=1, confidence=0.5, commit=True
    ):
        if entity_name == self.fail_on_entity:
            if self.fail_is_db_error:
                # What _rollback_quietly() does to the open transaction:
                # everything pending in this batch is discarded.
                self.pending = []
                self.rollbacks += 1
            raise RuntimeError("AGE said no")
        self.pending.append((drawer_id, entity_name, commit))
        if commit:
            self.committed.extend(self.pending)
            self.pending = []
            self.commits += 1

    def commit(self):
        self.commits += 1
        self.committed.extend(self.pending)
        self.pending = []


def _extractor(text):
    return [_Entity(word) for word in text.split()]


DRAWERS = [
    {"drawer_id": "d1", "document": "alpha beta", "metadata": {}},
    {"drawer_id": "d2", "document": "gamma delta", "metadata": {}},
    {"drawer_id": "d3", "document": "epsilon", "metadata": {}},
]


# ---------------------------------------------------------------------------
# The measurement this exists to change
# ---------------------------------------------------------------------------


def test_a_batch_costs_one_commit_not_one_per_mention():
    """The headline: 5 mentions across 3 drawers must cost ONE commit."""
    kg = _RecordingKG()
    hook = kgw.make_age_batch_writethrough(kg, _extractor)

    hook(DRAWERS)

    assert len(kg.mentions) == 5
    assert kg.commits == 1, f"expected one commit for the batch, got {kg.commits}"
    assert all(commit is False for _, _, commit in kg.mentions), (
        "every mention must defer its commit to the batch"
    )


def test_the_per_drawer_hook_still_commits_per_call():
    """Control: the old hook is unchanged, so existing callers are unaffected."""
    kg = _RecordingKG()
    hook = kgw.make_age_writethrough(kg, _extractor)

    for drawer in DRAWERS:
        hook(**drawer)

    assert kg.commits == 5, "the per-drawer hook keeps its per-call commit"


def test_commits_do_not_scale_with_batch_size():
    """100 drawers is still one commit; that is the whole point."""
    kg = _RecordingKG()
    hook = kgw.make_age_batch_writethrough(kg, _extractor)

    hook([{"drawer_id": f"d{i}", "document": "a b c", "metadata": {}} for i in range(100)])

    assert len(kg.mentions) == 300
    assert kg.commits == 1


# ---------------------------------------------------------------------------
# Failure handling — enrichment is opportunistic, it must not break ingest
# ---------------------------------------------------------------------------


def test_a_db_error_mid_batch_discards_that_batch_s_pending_edges():
    """The property production ACTUALLY has — asserted rather than wished for.

    An earlier version of this test claimed a failing mention cost only
    itself. It does not. ``_run_cypher`` routes through
    ``_with_conn_retry``, which calls ``_rollback_quietly()`` on a
    statement-level DB error, and under ``commit=False`` that discards
    everything pending in the batch.

    What survives is what the loop extracts *after* the failure, published
    by the final commit. The drawers themselves are untouched — they
    committed before the hook ran — and the lost edges are recoverable
    with ``backfill_age``.

    Restoring true per-mention isolation needs a SAVEPOINT around each
    mention; tracked as stage A2 on techempower-org/palace-daemon#265.
    """
    kg = _RecordingKG(fail_on_entity="delta")
    hook = kgw.make_age_batch_writethrough(kg, _extractor)

    hook(DRAWERS)

    names = [name for _, name, _ in kg.mentions]
    assert "delta" not in names
    assert not {"alpha", "beta", "gamma"} & set(names), (
        "the rollback discards every mention pending when the error hit"
    )
    assert "epsilon" in names, "mentions extracted after the failure still land"
    assert kg.rollbacks == 1
    assert kg.commits == 1


def test_a_non_db_failure_costs_only_its_own_mention():
    """Not every failure rolls back, so the loop's skip is still worth having."""
    kg = _RecordingKG(fail_on_entity="delta", fail_is_db_error=False)
    hook = kgw.make_age_batch_writethrough(kg, _extractor)

    hook(DRAWERS)

    names = [name for _, name, _ in kg.mentions]
    assert "delta" not in names
    assert {"alpha", "beta", "gamma", "epsilon"} <= set(names)
    assert kg.rollbacks == 0


def test_a_failing_extractor_skips_its_drawer_only():
    def _angry(text):
        if "gamma" in text:
            raise ValueError("extractor blew up")
        return _extractor(text)

    kg = _RecordingKG()
    hook = kgw.make_age_batch_writethrough(kg, _angry)

    hook(DRAWERS)

    names = [name for _, name, _ in kg.mentions]
    assert names == ["alpha", "beta", "epsilon"]
    assert kg.commits == 1


def test_a_failing_commit_is_swallowed_not_raised():
    """Ingest must survive a KG commit failure — the drawers are already written."""

    class _BadCommitKG(_RecordingKG):
        def commit(self):
            raise RuntimeError("commit failed")

    hook = kgw.make_age_batch_writethrough(_BadCommitKG(), _extractor)
    hook(DRAWERS)  # must not raise


def test_an_empty_batch_does_not_commit():
    """No work means no transaction."""
    kg = _RecordingKG()
    kgw.make_age_batch_writethrough(kg, _extractor)([])
    assert kg.commits == 0
    assert kg.mentions == []


def test_the_entity_cap_still_applies_per_drawer():
    kg = _RecordingKG()
    hook = kgw.make_age_batch_writethrough(kg, _extractor, max_entities_per_drawer=2)

    hook([{"drawer_id": "d1", "document": "a b c d e", "metadata": {}}])

    assert len(kg.mentions) == 2


# ---------------------------------------------------------------------------
# The collection must actually use the batch hook
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.executed.append(str(sql))

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

    def close(self):
        self.closed = 1


class _FakeMod:
    def __init__(self, conn):
        self._conn = conn

    def connect(self, dsn, **kwargs):
        return self._conn


@pytest.fixture()
def collection(monkeypatch):
    sql = pytest.importorskip("psycopg.sql", reason="requires the postgres extra")
    conn = _FakeConn()
    monkeypatch.setattr(pg, "_load_psycopg2", lambda: (_FakeMod(conn), sql))
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
    return col


def _add_three(col):
    col.add(
        documents=["alpha beta", "gamma delta", "epsilon"],
        ids=["d1", "d2", "d3"],
        metadatas=[{"wing": "w"}, {"wing": "w"}, {"wing": "w"}],
        embeddings=[[0.0] * 384] * 3,
    )


def test_the_collection_calls_the_batch_hook_once(collection):
    """One call carrying every drawer — not one call per drawer."""
    calls = []
    collection.set_kg_writethrough_batch(lambda drawers: calls.append(list(drawers)))

    _add_three(collection)

    assert len(calls) == 1, "the batch hook must be called once per _insert_rows"
    assert [d["drawer_id"] for d in calls[0]] == ["d1", "d2", "d3"]
    assert calls[0][0]["document"] == "alpha beta"
    assert calls[0][0]["metadata"] == {}  # wing/room already popped, as before


def test_the_batch_hook_wins_when_both_are_registered(collection):
    """Registering both must not double-write the graph."""
    per_drawer, batch = [], []
    collection.set_kg_writethrough(lambda **kw: per_drawer.append(kw["drawer_id"]))
    collection.set_kg_writethrough_batch(lambda drawers: batch.append(len(drawers)))

    _add_three(collection)

    assert batch == [3]
    assert per_drawer == [], "the per-drawer hook must not also run"


def test_the_per_drawer_hook_still_works_alone(collection):
    """Existing callers registered only the old hook; they must be unaffected."""
    seen = []
    collection.set_kg_writethrough(lambda **kw: seen.append(kw["drawer_id"]))

    _add_three(collection)

    assert seen == ["d1", "d2", "d3"]


def test_no_hook_registered_is_still_zero_overhead(collection):
    """The vector-only write path must stay byte-identical."""
    _add_three(collection)  # must not raise


def test_a_raising_batch_hook_never_breaks_ingest(collection):
    """Enrichment is opportunistic; the drawers are already committed."""

    def _boom(drawers):
        raise RuntimeError("the whole batch blew up")

    collection.set_kg_writethrough_batch(_boom)

    _add_three(collection)  # must not raise


# ---------------------------------------------------------------------------
# Control: writethrough off
# ---------------------------------------------------------------------------


def test_writethrough_off_issues_no_kg_work_at_all(collection):
    """The #249 precedent: always have a control with the suspect disabled."""
    kg = _RecordingKG()
    collection.set_kg_writethrough_batch(None)
    collection.set_kg_writethrough(None)

    _add_three(collection)

    assert kg.mentions == []
    assert kg.commits == 0


# ---------------------------------------------------------------------------
# The env builder and the palace wiring must actually reach the batch path
# ---------------------------------------------------------------------------


def test_env_builder_returns_a_batch_hook_that_commits_once(monkeypatch):
    """Without this wiring the batch factory is dead code in production."""
    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "regex")
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)
    kg = _RecordingKG()

    # Capitalized proper nouns: the real regex extractor matches those and
    # not the lowercase words the other tests use with a fake extractor.
    # (Caught by the "mentions must be non-empty" control below — a batch
    # that extracts nothing would otherwise pass a commits==0 assertion and
    # look like a working integration.)
    drawers = [
        {"drawer_id": "d1", "document": "Aya and Lumi met", "metadata": {}},
        {"drawer_id": "d2", "document": "the MemPalace project", "metadata": {}},
    ]

    hook = kgw.make_batch_writethrough_from_env(kg=kg, dsn="postgresql://fake/db")
    hook(drawers)

    assert kg.mentions, "the MENTIONS stage must have run at all"
    assert {n for _, n, _ in kg.mentions} == {"aya", "lumi", "mempalace"}
    assert kg.commits == 1, "the whole batch costs one commit through the env builder"


def test_env_builder_returns_none_when_writethrough_is_off(monkeypatch):
    """Control: the master switch still switches everything off."""
    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)

    assert kgw.make_batch_writethrough_from_env(kg=None, dsn=None) is None


def test_a_per_drawer_only_stage_still_composes_into_a_batch(monkeypatch):
    """The extraction queue is still per-drawer; it must not be dropped.

    Batching MENTIONS must not silently disable the queue stage when both
    are enabled — they compose, and the queue's own batching is stage B.
    """
    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "null")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "1")
    seen = []
    monkeypatch.setattr(
        kgw,
        "make_extraction_enqueue_writethrough",
        lambda dsn: lambda **kw: seen.append(kw["drawer_id"]),
    )

    hook = kgw.make_batch_writethrough_from_env(kg=_RecordingKG(), dsn="postgresql://fake/db")
    hook(DRAWERS)

    assert seen == ["d1", "d2", "d3"], "the per-drawer queue stage must still see every drawer"


def test_batchify_isolates_a_failing_drawer():
    """One drawer failing in a per-drawer stage must not drop the others."""
    seen = []

    def _flaky(*, drawer_id, document, metadata):
        if drawer_id == "d2":
            raise RuntimeError("nope")
        seen.append(drawer_id)

    kgw._batchify(_flaky)(DRAWERS)

    assert seen == ["d1", "d3"]


def test_palace_attaches_the_batch_hook_when_the_backend_supports_it(monkeypatch):
    """End of the wire: without this, the batch factory never runs in prod."""
    from mempalace import palace as palace_mod

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "null")
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)
    monkeypatch.setattr(
        "mempalace.knowledge_graph_age.KnowledgeGraphAGE", lambda dsn: _RecordingKG()
    )

    class _Col:
        def __init__(self):
            self.batch = None
            self.per_drawer = None

        def set_kg_writethrough_batch(self, hook):
            self.batch = hook

        def set_kg_writethrough(self, hook):
            self.per_drawer = hook

    col = _Col()
    palace_mod._writethrough_attached.discard(id(col))
    palace_mod._maybe_attach_writethrough(col, "postgresql://fake/db")

    assert col.batch is not None, "the batch hook must be the one attached"
    assert col.per_drawer is None, "the per-drawer hook must not also be attached"


def test_palace_falls_back_for_a_backend_without_the_batch_seam(monkeypatch):
    """chroma/sqlite collections have no batch seam; they must be unaffected."""
    from mempalace import palace as palace_mod

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "null")
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)
    monkeypatch.setattr(
        "mempalace.knowledge_graph_age.KnowledgeGraphAGE", lambda dsn: _RecordingKG()
    )

    class _OldCol:
        def __init__(self):
            self.per_drawer = None

        def set_kg_writethrough(self, hook):
            self.per_drawer = hook

    col = _OldCol()
    palace_mod._writethrough_attached.discard(id(col))
    palace_mod._maybe_attach_writethrough(col, "postgresql://fake/db")

    assert col.per_drawer is not None
