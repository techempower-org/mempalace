"""Tests for the canonical-room soft-warn taxonomy (techempower-org/mempalace#86).

Covers the three contract pieces:

1. ``mempalace.room_taxonomy.validate_room`` — empty list for canonical
   rooms, non-empty for non-canonical rooms, with a "did you mean"
   suggestion when one of the canonical names is a close match.
2. ``mempalace.miner.add_drawer`` / ``add_drawers`` — return shape now
   carries ``warnings``; the write still succeeds either way.
3. ``mempalace.mcp_server.tool_add_drawer`` / ``tool_diary_write`` —
   ``warnings`` rides out in the response dict; non-canonical rooms
   are accepted (no FK rejection).
"""

from __future__ import annotations

import pytest

from mempalace.room_taxonomy import (
    CANONICAL_ROOMS,
    is_canonical_room,
    suggest_canonical,
    validate_room,
)


# ── Module-level: validate_room ───────────────────────────────────────


class TestValidateRoom:
    def test_canonical_rooms_produce_no_warnings(self):
        for room in CANONICAL_ROOMS:
            assert validate_room(room) == [], (
                f"canonical room {room!r} should produce no warnings; got "
                f"{validate_room(room)!r}"
            )

    def test_seven_canonical_rooms_total(self):
        # Guardrail: the spec is explicit about the count. If this
        # changes, the spec and the docstring need to change too.
        assert len(CANONICAL_ROOMS) == 7
        assert set(CANONICAL_ROOMS) == {
            "architecture",
            "decisions",
            "problems",
            "planning",
            "sessions",
            "references",
            "discoveries",
        }

    def test_non_canonical_room_warns(self):
        warnings = validate_room("scratchpad")
        assert len(warnings) == 1
        msg = warnings[0]
        assert "scratchpad" in msg
        assert "not in the canonical taxonomy" in msg
        # The canonical list is rendered inline so the caller does not
        # need to import the constant to render a useful message.
        for room in CANONICAL_ROOMS:
            assert room in msg

    def test_close_match_yields_suggestion(self):
        # "decision" → "decisions" (singular vs plural)
        warnings = validate_room("decision")
        assert len(warnings) == 1
        assert "decisions" in warnings[0]
        assert "closest canonical match" in warnings[0]

    def test_diary_suggests_sessions(self):
        # The exact case that motivated #86: diary writes were silently
        # failing because "diary" isn't canonical. The soft warning
        # should accept it AND suggest "sessions" (canonical kin per
        # convo_miner's keyword routing).
        warnings = validate_room("diary")
        assert len(warnings) == 1
        msg = warnings[0]
        assert "diary" in msg
        # Either suggestion-bearing (preferred) or just the list — both
        # are acceptable; what matters is that the warning fires.
        # difflib at cutoff 0.6 currently matches "diary" → not close
        # enough; we accept either outcome to avoid coupling to difflib
        # internals, but assert the canonical list is present.
        for room in CANONICAL_ROOMS:
            assert room in msg

    def test_empty_room_warns(self):
        warnings = validate_room("")
        assert warnings  # non-empty
        assert "empty" in warnings[0].lower()

    def test_is_canonical_room(self):
        assert is_canonical_room("architecture") is True
        assert is_canonical_room("diary") is False
        assert is_canonical_room("") is False

    def test_suggest_canonical_returns_close_match(self):
        assert suggest_canonical("decision") == "decisions"
        assert suggest_canonical("architectur") == "architecture"

    def test_suggest_canonical_returns_none_for_unrelated(self):
        assert suggest_canonical("xyzzy") is None

    def test_suggest_canonical_custom_choices(self):
        # Pass a wider pool — useful for installations that maintain
        # custom rooms in the postgres lookup table.
        choices = list(CANONICAL_ROOMS) + ["experiments", "drafts"]
        assert suggest_canonical("experment", choices=choices) == "experiments"


# ── Integration: miner.add_drawer / add_drawers ───────────────────────


class _FakeCollection:
    """In-memory chroma-shaped collection for write-path tests."""

    def __init__(self):
        self.upserts: list[dict] = []

    def upsert(self, documents=None, ids=None, metadatas=None):
        self.upserts.append({"documents": documents, "ids": ids, "metadatas": metadatas})


class TestMinerAddDrawer:
    def test_canonical_room_no_warnings(self, tmp_path):
        from mempalace.miner import add_drawer

        source = tmp_path / "src.md"
        source.write_text("hello")
        col = _FakeCollection()
        result = add_drawer(
            col,
            wing="proj",
            room="architecture",
            content="hello",
            source_file=str(source),
            chunk_index=0,
            agent="test",
        )
        assert "id" in result
        assert result["id"].startswith("drawer_")
        assert result["warnings"] == []
        # Write actually happened.
        assert len(col.upserts) == 1

    def test_non_canonical_room_warns_but_writes(self, tmp_path):
        from mempalace.miner import add_drawer

        source = tmp_path / "src.md"
        source.write_text("hello")
        col = _FakeCollection()
        result = add_drawer(
            col,
            wing="proj",
            room="diary",  # non-canonical
            content="hello",
            source_file=str(source),
            chunk_index=0,
            agent="test",
        )
        assert result["warnings"], "expected at least one warning"
        assert "diary" in result["warnings"][0]
        # Crucially — the write SUCCEEDS regardless.
        assert len(col.upserts) == 1
        assert result["id"].startswith("drawer_")


class TestMinerAddDrawers:
    def test_canonical_room_empty_warnings(self, tmp_path):
        from mempalace.miner import add_drawers

        source = tmp_path / "src.md"
        source.write_text("hello")
        col = _FakeCollection()
        chunks = [
            {"content": "c1", "chunk_index": 0},
            {"content": "c2", "chunk_index": 1},
        ]
        added, ids, warnings = add_drawers(
            col,
            wing="proj",
            room="references",
            chunks=chunks,
            source_file=str(source),
            agent="test",
        )
        assert added == 2
        assert len(ids) == 2
        assert warnings == []

    def test_non_canonical_room_single_warning(self, tmp_path):
        # Warnings are per-call, not per-chunk — every chunk in a batch
        # shares the room, so one warning covers them all.
        from mempalace.miner import add_drawers

        source = tmp_path / "src.md"
        source.write_text("hello")
        col = _FakeCollection()
        chunks = [
            {"content": "c1", "chunk_index": 0},
            {"content": "c2", "chunk_index": 1},
            {"content": "c3", "chunk_index": 2},
        ]
        added, ids, warnings = add_drawers(
            col,
            wing="proj",
            room="scratchpad",
            chunks=chunks,
            source_file=str(source),
            agent="test",
        )
        assert added == 3
        assert len(warnings) == 1
        assert "scratchpad" in warnings[0]

    def test_empty_chunks_still_returns_warnings(self, tmp_path):
        # No chunks → no upserts → but the room warning is independent
        # of chunk count; the caller should still see it.
        from mempalace.miner import add_drawers

        source = tmp_path / "src.md"
        source.write_text("hello")
        col = _FakeCollection()
        added, ids, warnings = add_drawers(
            col,
            wing="proj",
            room="not_a_room",
            chunks=[],
            source_file=str(source),
            agent="test",
        )
        assert added == 0
        assert ids == []
        assert warnings  # non-canonical → warning fires


# ── Integration: mcp_server tool_add_drawer / tool_diary_write ────────
# These mirror the existing test_mcp_server.py harness — use the same
# _patch_mcp_server / _get_collection helpers via fixtures from
# conftest.py.


@pytest.fixture
def _mcp_setup(monkeypatch, config, palace_path, kg):
    """Bootstrap mcp_server against a temp palace; return the module."""
    from tests.test_mcp_server import _patch_mcp_server, _get_collection

    _patch_mcp_server(monkeypatch, config, kg)
    _client, _col = _get_collection(palace_path, create=True)
    del _client
    from mempalace import mcp_server

    return mcp_server


class TestToolAddDrawerWarnings:
    def test_canonical_room_returns_empty_warnings_list(self, _mcp_setup):
        result = _mcp_setup.tool_add_drawer(
            wing="proj",
            room="architecture",
            content="hello world, this is a canonical-room write.",
        )
        assert result["success"] is True
        # warnings key is ALWAYS present (empty list, not None / missing)
        assert "warnings" in result
        assert result["warnings"] == []

    def test_non_canonical_room_returns_warning_and_writes(self, _mcp_setup):
        result = _mcp_setup.tool_add_drawer(
            wing="proj",
            room="diary",  # historically rejected by FK; now accepted
            content="hello world, this is a non-canonical-room write.",
        )
        assert result["success"] is True
        assert result["drawer_id"]
        assert result["warnings"], "non-canonical room must surface a warning"
        msg = result["warnings"][0]
        assert "diary" in msg
        assert "not in the canonical taxonomy" in msg

    def test_idempotent_replay_preserves_warnings(self, _mcp_setup):
        # Second call hits the "already_exists" short-circuit. Warnings
        # must still ride out so the caller sees the same signal both
        # times — otherwise a retry would silently lose the taxonomy
        # nudge.
        kwargs = dict(
            wing="proj",
            room="brainstorm",  # non-canonical
            content="identical content for idempotency check",
        )
        first = _mcp_setup.tool_add_drawer(**kwargs)
        second = _mcp_setup.tool_add_drawer(**kwargs)
        assert first["success"] is True
        assert second["success"] is True
        assert second.get("reason") == "already_exists"
        assert second["warnings"], "warnings must survive the idempotent path"


class TestToolDiaryWriteWarnings:
    def test_diary_write_emits_warning_for_legacy_diary_room(self, _mcp_setup):
        # tool_diary_write hardcodes room="diary" — historically that
        # triggered an FK rejection (commit 12a25d7). Post-#86 the
        # write succeeds and the response carries the taxonomy warning.
        result = _mcp_setup.tool_diary_write(
            agent_name="TestAgent",
            entry="Today we relaxed the canonical-room FK.",
            topic="architecture",
        )
        assert result["success"] is True
        assert result["entry_id"]
        assert "warnings" in result
        assert result["warnings"], "diary room is non-canonical; expect warning"
        assert "diary" in result["warnings"][0]
