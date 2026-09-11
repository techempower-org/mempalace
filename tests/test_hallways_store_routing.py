"""hallways.py must route through the store, and default to JSON (#442).

Two things are pinned here, and the first matters more than the second:

1. **Nothing changes without an opt-in.** The production hallway file is
   ~1 GB; the cutover is a deliberate lead-run step after the migration is
   verified. A storage refactor that quietly changes the default is how a
   palace loses its hallways.
2. **On postgres, the wing-scoped paths stop reading the whole store.** That
   is the entire point: today `list_hallways(wing)` and
   `compute_hallways_for_wing` both pay a full 1 GB load, so the `wing`
   argument buys nothing.
"""

import pytest

from mempalace import hallway_store as hs
from mempalace import hallways as hallways_mod


class _Cfg:
    def __init__(self, backend="json", dsn="postgresql://fake/db"):
        self.hallway_backend = backend
        self.postgres_dsn = dsn
        self.hallway_file = "/nonexistent/hallways.json"


class _RecordingStore:
    """Stands in for a store so the routing is observable without a database."""

    def __init__(self, records=None, dynamics=None):
        self.records = list(records or [])
        self.dynamics = dynamics or {}
        self.calls = []

    def ensure_schema(self):
        self.calls.append(("ensure_schema",))

    def list(self, wing=None, limit=None, offset=None):
        self.calls.append(("list", wing, limit, offset))
        if wing is None:
            return list(self.records)
        return [r for r in self.records if r.get("wing") == wing]

    def dynamics_for_wing(self, wing):
        self.calls.append(("dynamics_for_wing", wing))
        return dict(self.dynamics)

    def replace_wing(self, wing, records):
        self.calls.append(("replace_wing", wing, len(records)))
        self.records = [r for r in self.records if r.get("wing") != wing] + list(records)

    def delete(self, hallway_id):
        self.calls.append(("delete", hallway_id))
        before = len(self.records)
        self.records = [r for r in self.records if r.get("id") != hallway_id]
        return len(self.records) != before


@pytest.fixture()
def recording(monkeypatch):
    store = _RecordingStore()
    monkeypatch.setattr(hallways_mod, "_hallway_store", lambda config=None: store)
    return store


# ---------------------------------------------------------------------------
# The default must not move
# ---------------------------------------------------------------------------


def test_default_config_still_uses_the_json_file(monkeypatch, tmp_path):
    """No flag set: the JSON store, reading the same file as before."""
    hallway_file = tmp_path / "hallways.json"
    hallway_file.write_text(
        '{"schema_version": 1, "hallways": ['
        '{"id": "h1", "wing": "w", "entity_a": "A", "entity_b": "B"}]}'
    )
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(hallway_file))

    store = hallways_mod._hallway_store(_Cfg(backend="json"))
    assert isinstance(store, hs.JsonHallwayStore)
    assert [h["id"] for h in store.list()] == ["h1"]


def test_json_store_reads_through_the_monkeypatchable_helpers(monkeypatch, tmp_path):
    """Existing tests patch hallways._get_hallway_file; that must keep working."""
    hallway_file = tmp_path / "h.json"
    hallway_file.write_text('[{"id": "x", "wing": "w"}]')
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(hallway_file))

    assert hallways_mod.list_hallways(config=_Cfg()) == [{"id": "x", "wing": "w"}]


# ---------------------------------------------------------------------------
# Reads route through the store
# ---------------------------------------------------------------------------


def test_list_hallways_delegates_the_wing_filter_to_the_store(recording):
    """The filter must reach the store, not be applied after a full read."""
    recording.records = [
        {"id": "a", "wing": "kiyo"},
        {"id": "b", "wing": "other"},
    ]

    result = hallways_mod.list_hallways(wing="kiyo")

    assert [h["id"] for h in result] == ["a"]
    assert ("list", "kiyo", None, None) in recording.calls


def test_list_hallways_passes_pagination_through(recording):
    hallways_mod.list_hallways(wing="kiyo", limit=25, offset=50)
    assert ("list", "kiyo", 25, 50) in recording.calls


def test_delete_hallway_delegates(recording):
    recording.records = [{"id": "gone", "wing": "w"}]
    assert hallways_mod.delete_hallway("gone") is True
    assert hallways_mod.delete_hallway("missing") is False


# ---------------------------------------------------------------------------
# The recompute path stops reading the whole store
# ---------------------------------------------------------------------------


class _FakeCol:
    def __init__(self, metadatas):
        self._metadatas = metadatas

    def count(self):
        return len(self._metadatas)

    def get(self, limit=None, offset=0, include=None):
        return {"metadatas": self._metadatas[offset : offset + (limit or len(self._metadatas))]}


def test_compute_reads_dynamics_for_one_wing_not_the_whole_store(recording):
    """Two full parses of a 1.04 GB file per mine; both copies live at once."""
    col = _FakeCol([{"wing": "kiyo", "room": "diary", "entities": "Aya;Lumi"}] * 3)

    hallways_mod.compute_hallways_for_wing("kiyo", col=col, min_count=2)

    assert ("dynamics_for_wing", "kiyo") in recording.calls
    assert not any(c[0] == "list" for c in recording.calls), (
        "the recompute must not read every hallway in the palace"
    )


def test_compute_writes_only_this_wing(recording):
    col = _FakeCol([{"wing": "kiyo", "room": "diary", "entities": "Aya;Lumi"}] * 3)

    hallways_mod.compute_hallways_for_wing("kiyo", col=col, min_count=2)

    replaces = [c for c in recording.calls if c[0] == "replace_wing"]
    assert replaces == [("replace_wing", "kiyo", 1)]


def test_compute_preserves_accumulated_dynamics(recording):
    """Dynamics survive a recompute, keyed by the sorted pair, as before."""
    recording.dynamics = {
        ("Aya", "Lumi"): {
            "strength": 0.91,
            "stability": 0.4,
            "last_activated": "2026-01-01T00:00:00+00:00",
            "access_count": 7,
        }
    }
    col = _FakeCol([{"wing": "kiyo", "room": "diary", "entities": "Lumi;Aya"}] * 3)

    created = hallways_mod.compute_hallways_for_wing("kiyo", col=col, min_count=2)

    assert created[0]["strength"] == 0.91
    assert created[0]["access_count"] == 7


def test_compute_with_no_surviving_pairs_still_clears_the_wing(recording):
    """Pairs that fell below min_count must not leave a stale snapshot behind."""
    recording.records = [{"id": "stale", "wing": "kiyo"}]
    col = _FakeCol([{"wing": "kiyo", "room": "diary", "entities": "Aya;Lumi"}])

    hallways_mod.compute_hallways_for_wing("kiyo", col=col, min_count=5)

    assert ("replace_wing", "kiyo", 0) in recording.calls
    assert recording.records == []
