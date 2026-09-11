"""test_provenance.py — source provenance + staleness on search hits.

A palace search returns the *indexed copy* of whatever was mined. For a
project fact that copy is usually a session transcript quoting the claim,
not the curated document that later corrected it — so a refuted claim can
come back looking authoritative (techempower-org/mempalace#451). These
helpers put the provenance on the hit so every reader (CLI, JSON, MCP)
can see which kind of source it is looking at and whether the file on
disk has moved on since it was indexed.
"""

import os
from datetime import datetime, timedelta

import pytest

from mempalace.provenance import (
    all_transcript,
    annotate,
    no_curated_source,
    provenance_note,
    source_kind,
    source_stale,
)


# ── source_kind ─────────────────────────────────────────────────────────


class TestSourceKind:
    def test_full_jsonl_path_is_transcript(self):
        hit = {"source_file": "/home/jp/.claude/projects/-home-jp-2g/abc-123.jsonl"}
        assert source_kind(hit) == "transcript"

    def test_basename_only_jsonl_is_transcript(self):
        """The MCP path strips directories — a bare basename must still work."""
        assert source_kind({"source_file": "abc-123.jsonl"}) == "transcript"

    def test_jsonl_match_is_case_insensitive(self):
        assert source_kind({"source_file": "ABC.JSONL"}) == "transcript"

    def test_memory_markdown_is_memory(self):
        hit = {"source_file": "/home/jp/.claude/projects/-home-jp-2g/memory/user_jp.md"}
        assert source_kind(hit) == "memory"

    def test_diary_id_without_source_is_diary(self):
        hit = {"id": "diary_2g_20260906_1", "source_file": None}
        assert source_kind(hit) == "diary"

    def test_drawer_id_key_also_recognised_for_diary(self):
        assert source_kind({"drawer_id": "diary_2g_20260906_1"}) == "diary"

    def test_project_markdown_is_file(self):
        assert source_kind({"source_file": "/home/jp/Projects/2g/CLAUDE.md"}) == "file"

    def test_empty_hit_is_unknown(self):
        assert source_kind({}) == "unknown"

    def test_question_mark_placeholder_is_unknown(self):
        """``searcher`` writes "?" when a drawer has no source_file."""
        assert source_kind({"source_file": "?"}) == "unknown"

    @pytest.mark.parametrize("key", ["source", "source_path", "_source_file_full"])
    def test_alternate_path_keys_are_read(self, key):
        assert source_kind({key: "/x/y/session.jsonl"}) == "transcript"

    def test_non_dict_is_unknown(self):
        assert source_kind("not a hit") == "unknown"


# ── source_stale ────────────────────────────────────────────────────────


class TestSourceStale:
    def test_file_modified_after_indexing_is_stale(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("refuted\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        assert source_stale({"source_file": str(f), "created_at": indexed}) is True

    def test_file_older_than_index_is_not_stale(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("still current\n")
        old = (datetime.now() - timedelta(days=2)).timestamp()
        os.utime(f, (old, old))
        indexed = datetime.now().isoformat()
        assert source_stale({"source_file": str(f), "created_at": indexed}) is False

    def test_modification_inside_grace_window_is_not_stale(self, tmp_path):
        """A file touched seconds after its own mine is the mine, not an edit."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        indexed_dt = datetime.now() - timedelta(seconds=30)
        os.utime(f, (datetime.now().timestamp(), datetime.now().timestamp()))
        assert source_stale({"source_file": str(f), "created_at": indexed_dt.isoformat()}) is False

    def test_basename_only_path_is_undecidable(self):
        hit = {"source_file": "abc.jsonl", "created_at": "2026-09-01T14:13:00"}
        assert source_stale(hit) is None

    def test_missing_file_is_undecidable(self, tmp_path):
        hit = {
            "source_file": str(tmp_path / "gone.md"),
            "created_at": "2026-09-01T14:13:00",
        }
        assert source_stale(hit) is None

    def test_unparseable_created_at_is_undecidable(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        assert source_stale({"source_file": str(f), "created_at": "unknown"}) is None

    def test_missing_created_at_is_undecidable(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        assert source_stale({"source_file": str(f)}) is None

    def test_future_index_timestamp_is_undecidable(self, tmp_path):
        """A created_at after ``now`` means a bad clock — decide nothing."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        future = (datetime.now() + timedelta(days=3)).isoformat()
        assert source_stale({"source_file": str(f), "created_at": future}) is None

    @pytest.mark.parametrize("key", ["indexed_at", "filed_at"])
    def test_alternate_timestamp_keys_are_read(self, tmp_path, key):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        assert source_stale({"source_file": str(f), key: indexed}) is True

    def test_never_raises_on_garbage(self):
        assert source_stale({"source_file": 17, "created_at": []}) is None
        assert source_stale("not a hit") is None


# ── annotate ────────────────────────────────────────────────────────────


class TestAnnotate:
    def test_adds_source_kind_to_every_hit(self):
        hits = [{"source_file": "a.jsonl"}, {"source_file": "/p/CLAUDE.md"}]
        annotate(hits)
        assert [h["source_kind"] for h in hits] == ["transcript", "file"]

    def test_mutates_in_place_and_returns_same_list(self):
        hits = [{"source_file": "a.jsonl"}]
        out = annotate(hits)
        assert out is hits

    def test_adds_source_stale_and_indexed_at_when_decidable(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        hits = [{"source_file": str(f), "created_at": indexed}]
        annotate(hits)
        assert hits[0]["source_stale"] is True
        assert hits[0]["source_indexed_at"] == indexed

    def test_stamps_source_stale_on_transcripts_too(self, tmp_path):
        """Suppression is a RENDERING rule. The machine-readable field stays on
        every kind so JSON/MCP consumers can still see it."""
        f = tmp_path / "session.jsonl"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        hits = [{"source_file": str(f), "created_at": indexed}]
        annotate(hits)
        assert hits[0]["source_kind"] == "transcript"
        assert hits[0]["source_stale"] is True

    def test_omits_source_stale_when_undecidable(self):
        hits = [{"source_file": "a.jsonl", "created_at": "unknown"}]
        annotate(hits)
        assert "source_stale" not in hits[0]

    def test_is_idempotent(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        hits = [{"source_file": str(f), "created_at": indexed}]
        first = [dict(h) for h in annotate(hits)]
        second = [dict(h) for h in annotate(hits)]
        assert first == second

    def test_tolerates_non_dict_items(self):
        hits = [None, "junk", 5, {"source_file": "a.jsonl"}]
        annotate(hits)
        assert hits[:3] == [None, "junk", 5]
        assert hits[3]["source_kind"] == "transcript"

    def test_tolerates_non_list_input(self):
        assert annotate(None) is None
        assert annotate({"results": []}) == {"results": []}


# ── provenance_note ─────────────────────────────────────────────────────


class TestProvenanceNote:
    def test_transcript_note(self):
        note = provenance_note({"source_file": "a.jsonl"})
        assert note == "quoted copy from a session transcript — verify at the curated source"

    def test_stale_note_carries_the_index_date(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).replace(microsecond=0)
        hit = {"source_file": str(f), "created_at": indexed.isoformat()}
        note = provenance_note(hit)
        assert note == (
            "source file modified after indexing "
            f"(indexed {indexed.strftime('%Y-%m-%d')}) — re-mine or read the file"
        )

    def test_stale_transcript_gets_only_the_transcript_note(self, tmp_path):
        """A live session's transcript is appended to continuously, so a
        transcript is ALWAYS "modified after indexing" — the note would fire on
        every open session and say nothing the transcript caveat doesn't already
        say. The staleness note is for curated files, which is the #451 case."""
        f = tmp_path / "session.jsonl"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        note = provenance_note({"source_file": str(f), "created_at": indexed})
        assert note == "quoted copy from a session transcript — verify at the curated source"
        assert "modified after indexing" not in note

    def test_stale_curated_file_still_gets_the_stale_note(self, tmp_path):
        """The suppression is transcript-only; a curated document going stale is
        exactly what #451 is about."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        note = provenance_note({"source_file": str(f), "created_at": indexed})
        assert note.startswith("source file modified after indexing")

    def test_stale_memory_file_still_gets_the_stale_note(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        f = mem / "user_jp.md"
        f.write_text("x\n")
        indexed = (datetime.now() - timedelta(days=2)).isoformat()
        hit = {"source_file": str(f), "source_path": str(f), "created_at": indexed}
        assert provenance_note(hit).startswith("source file modified after indexing")

    def test_fresh_curated_file_has_no_note(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x\n")
        old = (datetime.now() - timedelta(days=2)).timestamp()
        os.utime(f, (old, old))
        hit = {"source_file": str(f), "created_at": datetime.now().isoformat()}
        assert provenance_note(hit) is None

    def test_uses_already_annotated_fields(self):
        """Renderers get annotated hits; the note must not re-stat the disk."""
        hit = {"source_kind": "transcript", "source_stale": False}
        assert provenance_note(hit) == (
            "quoted copy from a session transcript — verify at the curated source"
        )


# ── all_transcript ──────────────────────────────────────────────────────


class TestAllTranscript:
    def test_true_when_every_hit_is_a_transcript(self):
        assert all_transcript([{"source_file": "a.jsonl"}, {"source_file": "b.jsonl"}]) is True

    def test_false_when_one_hit_is_curated(self):
        hits = [{"source_file": "a.jsonl"}, {"source_file": "/p/memory/x.md"}]
        assert all_transcript(hits) is False

    def test_false_for_empty_results(self):
        assert all_transcript([]) is False

    def test_false_for_non_list(self):
        assert all_transcript(None) is False


class TestNoCuratedSource:
    """The substantive #451 signal: nothing a human maintains matched.

    ``all_transcript`` is the pure case. A palace-written diary drawer is not
    a transcript, but it is not a curated document either — a result set of
    transcripts and diaries still contains nowhere a later correction could
    have landed, so the reader needs the same warning.
    """

    def test_true_for_transcripts_only(self):
        assert no_curated_source([{"source_file": "a.jsonl"}]) is True

    def test_true_for_transcripts_mixed_with_diary(self):
        hits = [{"source_file": "a.jsonl"}, {"id": "diary_2g_1", "source_file": None}]
        assert no_curated_source(hits) is True

    def test_false_when_a_project_document_matched(self):
        hits = [{"source_file": "a.jsonl"}, {"source_file": "/p/CLAUDE.md"}]
        assert no_curated_source(hits) is False

    def test_false_when_a_memory_file_matched(self):
        hits = [{"source_file": "a.jsonl"}, {"source_path": "/p/memory/x.md"}]
        assert no_curated_source(hits) is False

    def test_false_when_a_hit_is_unclassifiable(self):
        """An unknown-shaped hit is not evidence of anything — don't claim it."""
        assert no_curated_source([{"source_file": "a.jsonl"}, {}]) is False

    def test_false_for_empty_and_non_list(self):
        assert no_curated_source([]) is False
        assert no_curated_source(None) is False


# ── auto_query shares the predicate (no duplicate logic) ────────────────


class TestAutoQueryCuratedDelegates:
    def test_is_curated_matches_memory_kind(self):
        from mempalace.auto_query.runner import _is_curated

        assert _is_curated({"source_file": "/p/memory/user_jp.md"}) is True
        assert _is_curated({"source_path": "/p/memory/user_jp.md"}) is True
        assert _is_curated({"source_file": "/p/CLAUDE.md"}) is False
        assert _is_curated({"source_file": "a.jsonl"}) is False
        assert _is_curated({}) is False


# ── searcher result assembly (the MCP mempalace_search path) ────────────


class TestSearcherAnnotatesResults:
    """``search_memories`` runs on the daemon host behind MCP ``mempalace_search``.

    Every branch that assembles ``results`` has to stamp provenance, or the
    fleet's most-used read path is the one surface that stays silent about it.
    These hits keep ``source_path`` (the full path) beside the display
    basename, so staleness resolves here as well — measured against the host
    running the call, which under the daemon is the palace host.
    ``_bm25_only_via_postgres`` is the one arm that returns a basename only,
    and its hits are therefore decidable for kind but not for staleness.
    """

    def test_envelope_annotates_every_hit(self):
        from mempalace.searcher import _search_result_envelope

        out = _search_result_envelope(
            query="q",
            wing=None,
            room=None,
            source_file=None,
            since=None,
            before=None,
            hits=[
                {"source_file": "abc.jsonl"},
                {"source_file": "user_jp.md", "source_path": "/p/memory/user_jp.md"},
            ],
            candidates_fetched=2,
            pool_size=20,
            date_window_active=False,
        )
        assert [h["source_kind"] for h in out["results"]] == ["transcript", "memory"]

    def test_postgres_bm25_branch_annotates(self, monkeypatch):
        import sys
        import types

        from mempalace import searcher

        row = (
            "drawer_2g_general_aaa",
            "2g",
            "general",
            "FIVE HANDSETS REFUSE THIS NETWORK",
            {"source_file": "/home/jp/.claude/projects/-home-jp-2g/abc.jsonl"},
            0.481,
        )

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a, **k):
                return None

            def fetchall(self):
                return [row]

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return _Cur()

        fake = types.ModuleType("psycopg")
        fake.connect = lambda *a, **k: _Conn()
        monkeypatch.setitem(sys.modules, "psycopg", fake)

        out = searcher._bm25_only_via_postgres("handsets", "postgresql://x/y", wing="2g")
        assert out["results"][0]["source_kind"] == "transcript"
