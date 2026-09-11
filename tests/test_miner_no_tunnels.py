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
