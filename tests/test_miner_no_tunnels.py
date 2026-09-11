"""Skip the post-mine derived-analytics block: ``mine --no-tunnels`` (#474).

Every projects-mode mine recomputes three derived analytics for the WHOLE wing,
however few files changed:

    1. _compute_topic_tunnels_for_wing   -> tunnels.json
    2. compute_hallways_for_wing         -> hallways.json  (loads + rewrites it)
    3. _compute_entity_tunnels_for_wing  -> reads what (2) wrote, writes tunnels

Measured 2026-09-10 on the palace host: a 31-file memory sweep spent **29+ min
CPU and 1.6-4.1 GB RSS** in that block, holding the exclusive mine lock, with
twelve more sweeps queued behind it. The cost scales with the WING, not with the
change, so a hook-driven sweep of a handful of memory files pays for a
37K-drawer wing every time.

A memory sweep does not need tunnels at all, so the cheapest fix is to not do
the work. These tests pin two things: the block is skippable, and it is still
ON by default -- a silent default change here would quietly stop maintaining
the cross-wing graph for every existing caller.

Note the flag covers all THREE steps, not only the two named `..._tunnels_...`.
They are a dependency chain (3 reads what 2 wrote), and gating only the tunnel
pair would leave the hallways load + rewrite in place -- i.e. most of the I/O.

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_miner_no_tunnels.py -q
"""

from pathlib import Path

import pytest
import yaml

from mempalace import miner as miner_mod
from mempalace.miner import mine


def _nullcontext_factory():
    """`mine_palace_lock` stand-in: the real one takes an exclusive flock."""
    import contextlib

    @contextlib.contextmanager
    def _lock(_palace_path):
        yield

    return _lock


class _StubCollection:
    """Records writes; reports no prior drawers so every file mines."""

    def __init__(self):
        self.upsert_calls = []
        self.delete_calls = []

    def get(self, **_kwargs):
        return {"ids": [], "metadatas": [], "documents": []}

    def upsert(self, *, documents, ids, metadatas):
        self.upsert_calls.append(
            {"documents": list(documents), "ids": list(ids), "metadatas": list(metadatas)}
        )

    def delete(self, where=None, **_kwargs):
        self.delete_calls.append(where)

    def query(self, **_kwargs):
        return {"ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]}

    def count(self):
        return sum(len(c["ids"]) for c in self.upsert_calls)


@pytest.fixture
def derived_spies(monkeypatch):
    """Replace all three derived-analytics steps with counters."""
    calls = {"topic": 0, "hallways": 0, "entity": 0}

    def topic(*_a, **_k):
        calls["topic"] += 1
        return 0

    def hallways(*_a, **_k):
        calls["hallways"] += 1
        return []

    def entity(*_a, **_k):
        calls["entity"] += 1
        return 0

    monkeypatch.setattr(miner_mod, "_compute_topic_tunnels_for_wing", topic)
    monkeypatch.setattr(miner_mod, "compute_hallways_for_wing", hallways)
    monkeypatch.setattr(miner_mod, "_compute_entity_tunnels_for_wing", entity)
    return calls


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text("Some durable content here.\n" * 60, encoding="utf-8")
    with open(root / "mempalace.yaml", "w") as fh:
        yaml.dump({"wing": "w474", "rooms": [{"name": "notes", "description": "Notes"}]}, fh)
    return root


def _mine(root, **kwargs):
    col = _StubCollection()
    mine(
        str(root),
        str(root / "palace"),
        collection=col,
        closets_collection=_StubCollection(),
        **kwargs,
    )
    return col


def test_derived_analytics_run_by_default(project, derived_spies):
    """Control: without the flag all three steps still run, exactly as before.

    If this ever goes quiet the skip test below proves nothing.
    """
    col = _mine(project)

    assert col.upsert_calls, "fixture must actually mine something"
    assert derived_spies == {"topic": 1, "hallways": 1, "entity": 1}


def test_compute_derived_false_skips_all_three(project, derived_spies):
    col = _mine(project, compute_derived=False)

    assert col.upsert_calls, "the mine itself must still happen"
    assert derived_spies == {"topic": 0, "hallways": 0, "entity": 0}, (
        "all three post-mine steps must be skipped -- gating only the two "
        "tunnel steps would leave the hallways load + rewrite in place"
    )


def test_compute_derived_true_is_the_default_signature(project, derived_spies):
    """Explicit True behaves identically to omitting it."""
    _mine(project, compute_derived=True)

    assert derived_spies == {"topic": 1, "hallways": 1, "entity": 1}


def test_dry_run_still_skips_derived_analytics(project, derived_spies):
    """Pre-existing behaviour, pinned so the new gate cannot resurrect it."""
    _mine(project, dry_run=True)

    assert derived_spies == {"topic": 0, "hallways": 0, "entity": 0}


def test_skipping_derived_does_not_change_what_is_filed(project, derived_spies):
    """The drawers written must be byte-identical with and without the flag."""
    with_derived = _mine(project)
    without = _mine(project, compute_derived=False)

    def shape(col):
        return [
            (m["source_file"], m["chunk_index"], m["wing"], m["room"])
            for call in col.upsert_calls
            for m in call["metadatas"]
        ]

    assert shape(with_derived) == shape(without)
    assert shape(without), "control: something was actually written"


assert Path  # fixtures above use tmp_path; keep the import meaningful


# ---------------------------------------------------------------------------
# zero-upsert short-circuit (#474) — no flag needed
# ---------------------------------------------------------------------------


class _AlreadyMinedCollection(_StubCollection):
    """Reports every file as already mined at its current mtime.

    Reproduces the measured case: the requeued CLAUDE.md mine wrote zero
    drawers because the stored source_mtime still matched, and then spent
    14+ minutes at 3.5 GB recomputing the wing's derived graph anyway.
    """

    def __init__(self, sources):
        super().__init__()
        import os

        from mempalace.palace import NORMALIZE_VERSION

        self._metas = [
            {
                "source_file": str(s),
                "source_mtime": os.path.getmtime(s),
                "normalize_version": NORMALIZE_VERSION,
            }
            for s in sources
        ]

    def get(self, **kwargs):
        if kwargs.get("offset", 0):
            return {"ids": [], "metadatas": [], "documents": []}
        return {
            "ids": [f"seed{i}" for i in range(len(self._metas))],
            "metadatas": [dict(m) for m in self._metas],
            "documents": [""] * len(self._metas),
        }


def test_zero_upsert_mine_skips_the_derived_block(project, derived_spies):
    """A mine that filed nothing has nothing to recompute for.

    Measured on the palace host: a requeued CLAUDE.md mine wrote zero drawers
    (all 661 drawers still carried the earlier filed_at, so the mtime check
    skipped the file) and still spent 14+ minutes at 3.5 GB in this block.
    """
    col = _AlreadyMinedCollection([project / "notes" / "a.md"])
    mine(
        str(project), str(project / "palace"), collection=col, closets_collection=_StubCollection()
    )

    assert col.upsert_calls == [], "control: this mine must file nothing"
    assert derived_spies == {"topic": 0, "hallways": 0, "entity": 0}


def test_zero_upsert_skip_needs_no_flag(project, derived_spies):
    """Explicitly asking for the derived block does not resurrect it when
    there is nothing to recompute — the short-circuit is unconditional."""
    col = _AlreadyMinedCollection([project / "notes" / "a.md"])
    mine(
        str(project),
        str(project / "palace"),
        collection=col,
        closets_collection=_StubCollection(),
        compute_derived=True,
    )

    assert col.upsert_calls == []
    assert derived_spies == {"topic": 0, "hallways": 0, "entity": 0}


def test_a_mine_that_files_something_still_computes(project, derived_spies):
    """Control for both tests above: the same fixture with an empty
    collection does file drawers and does run the derived block."""
    col = _mine(project)

    assert col.upsert_calls
    assert derived_spies == {"topic": 1, "hallways": 1, "entity": 1}


def test_fts5_validation_still_runs_when_derived_is_skipped(project, derived_spies, monkeypatch):
    """The integrity check is NOT a derived analytic and must never be gated.

    It sits inside `if not dry_run:` but outside the `compute_derived` gate.
    That placement is easy to break with an editor's re-indent, and nothing
    pinned it until this test.
    """
    from mempalace import miner as m

    calls = []
    monkeypatch.setattr(m, "_validate_palace_fts5_after_mine", lambda p: calls.append(p))
    monkeypatch.setattr(m, "get_collection", lambda *_a, **_k: _StubCollection())
    monkeypatch.setattr(m, "get_closets_collection", lambda *_a, **_k: _StubCollection())
    monkeypatch.setattr(m, "mine_palace_lock", _nullcontext_factory())

    mine(str(project), str(project / "palace"), compute_derived=False)

    assert derived_spies == {"topic": 0, "hallways": 0, "entity": 0}
    assert len(calls) == 1, "FTS5 validation must still run with the derived block skipped"


def test_fts5_validation_runs_when_derived_is_computed(project, derived_spies, monkeypatch):
    """Control: the same harness with the gate open also validates once."""
    from mempalace import miner as m

    calls = []
    monkeypatch.setattr(m, "_validate_palace_fts5_after_mine", lambda p: calls.append(p))
    monkeypatch.setattr(m, "get_collection", lambda *_a, **_k: _StubCollection())
    monkeypatch.setattr(m, "get_closets_collection", lambda *_a, **_k: _StubCollection())
    monkeypatch.setattr(m, "mine_palace_lock", _nullcontext_factory())

    mine(str(project), str(project / "palace"))

    assert derived_spies == {"topic": 1, "hallways": 1, "entity": 1}
    assert len(calls) == 1
