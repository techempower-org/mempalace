"""hallways.json → postgres migration: streaming, idempotent, resumable (#442).

The source file on the production palace host is **1,041,537,215 bytes** with
~797K records. That number drives every requirement here:

- **Streaming.** ``json.load`` on it materializes several GB of Python dicts.
  A migration that has to allocate 4 GB to move data whose whole problem is
  that it allocates 4 GB is not a migration. The reader yields one record at
  a time.
- **Idempotent.** Re-running must converge, not duplicate — the upsert is
  keyed on the record id.
- **Resumable.** A run that dies at record 600,000 must not start over, and
  must refuse to resume against a file that changed underneath it.
- **Dry-run.** The lead runs the real cutover; a dry run has to report
  exactly what would happen without touching the database.
"""

import json

import pytest

from mempalace import migrate_hallways as mh


# ---------------------------------------------------------------------------
# Streaming reader
# ---------------------------------------------------------------------------


def _write(tmp_path, payload, name="hallways.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


RECORDS = [
    {"id": "h1", "wing": "kiyo", "entity_a": "A", "entity_b": "B", "co_occurrence_count": 3},
    {"id": "h2", "wing": "kiyo", "entity_a": "C", "entity_b": "D", "co_occurrence_count": 9},
    {"id": "h3", "wing": "other", "entity_a": "E", "entity_b": "F", "co_occurrence_count": 2},
]


def test_reads_the_wrapped_object_form(tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "hallways": RECORDS})
    assert list(mh.iter_hallway_records(path)) == RECORDS


def test_reads_the_bare_list_form(tmp_path):
    """Older files are a bare array; _load_hallways accepts both, so must this."""
    path = _write(tmp_path, RECORDS)
    assert list(mh.iter_hallway_records(path)) == RECORDS


def test_reads_an_empty_store(tmp_path):
    assert list(mh.iter_hallway_records(_write(tmp_path, {"hallways": []}))) == []
    assert list(mh.iter_hallway_records(_write(tmp_path, []))) == []


def test_records_spanning_chunk_boundaries_are_not_split(tmp_path):
    """The reader must not depend on a record fitting in one read()."""
    big = [dict(r, label="x" * 500) for r in RECORDS]
    path = _write(tmp_path, {"hallways": big})
    assert list(mh.iter_hallway_records(path, chunk_size=16)) == big


def test_multibyte_and_astral_characters_survive_chunking(tmp_path):
    """Entity names are user content: never split a character mid-decode."""
    records = [{"id": "h1", "wing": "w", "entity_a": "アヤ🚀", "entity_b": "Lumi—ルミ"}]
    path = _write(tmp_path, {"hallways": records})
    assert list(mh.iter_hallway_records(path, chunk_size=8)) == records


def test_the_reader_does_not_materialize_the_whole_file(tmp_path):
    """The point of the exercise: peak memory is a buffer, not the corpus."""
    path = _write(tmp_path, {"hallways": [dict(r, label="y" * 2000) for r in RECORDS * 40]})
    seen = 0
    for _ in mh.iter_hallway_records(path, chunk_size=64):
        seen += 1
    assert seen == 120


def test_malformed_json_raises_rather_than_silently_importing_nothing(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"hallways": [{"id": "h1", ', encoding="utf-8")
    with pytest.raises(ValueError):
        list(mh.iter_hallway_records(str(path)))


def test_missing_file_is_an_explicit_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(mh.iter_hallway_records(str(tmp_path / "nope.json")))


# ---------------------------------------------------------------------------
# Migration behaviour
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.rows = {}
        self.schema_calls = 0

    def ensure_schema(self, conn=None):
        self.schema_calls += 1

    def upsert_many(self, records, conn=None):
        for r in records:
            self.rows[r["id"]] = r
        return len(records)

    def count(self, wing=None, conn=None):
        if wing is None:
            return len(self.rows)
        return sum(1 for r in self.rows.values() if r.get("wing") == wing)


def test_dry_run_touches_nothing_and_still_reports(tmp_path):
    path = _write(tmp_path, {"hallways": RECORDS})
    store = _FakeStore()

    result = mh.migrate(path, store=store, dry_run=True, state_path=str(tmp_path / "s.json"))

    assert store.rows == {}
    assert store.schema_calls == 0
    assert result["total"] == 3
    assert result["dry_run"] is True
    assert result["by_wing"] == {"kiyo": 2, "other": 1}


def test_migration_imports_every_record(tmp_path):
    path = _write(tmp_path, {"hallways": RECORDS})
    store = _FakeStore()

    result = mh.migrate(path, store=store, state_path=str(tmp_path / "s.json"))

    assert result["imported"] == 3
    assert set(store.rows) == {"h1", "h2", "h3"}


def test_migration_is_idempotent(tmp_path):
    """Re-running converges. The upsert is keyed on the record id."""
    path = _write(tmp_path, {"hallways": RECORDS})
    store = _FakeStore()
    state = str(tmp_path / "s.json")

    mh.migrate(path, store=store, state_path=state)
    mh.migrate(path, store=store, state_path=state, restart=True)

    assert len(store.rows) == 3


def test_resume_skips_what_already_landed(tmp_path):
    """A run that died at N must not redo the first N records."""
    path = _write(tmp_path, {"hallways": RECORDS})
    state = tmp_path / "s.json"
    store = _FakeStore()

    mh._write_state(str(state), path, imported=2)
    result = mh.migrate(path, store=store, state_path=str(state))

    assert result["skipped"] == 2
    assert set(store.rows) == {"h3"}


def test_resume_refuses_when_the_source_file_changed(tmp_path):
    """Resuming by count against a different file would corrupt the import."""
    path = _write(tmp_path, {"hallways": RECORDS})
    state = tmp_path / "s.json"
    mh._write_state(str(state), path, imported=2)

    # Rewrite the source with different content (and therefore a new size).
    _write(tmp_path, {"hallways": RECORDS + [dict(RECORDS[0], id="h4")]})

    with pytest.raises(mh.MigrationStateMismatch):
        mh.migrate(path, store=_FakeStore(), state_path=str(state))


def test_restart_ignores_stale_state(tmp_path):
    path = _write(tmp_path, {"hallways": RECORDS})
    state = tmp_path / "s.json"
    mh._write_state(str(state), path, imported=2)

    result = mh.migrate(path, store=_FakeStore(), state_path=str(state), restart=True)

    assert result["skipped"] == 0
    assert result["imported"] == 3


def test_state_is_recorded_so_a_later_run_can_resume(tmp_path):
    path = _write(tmp_path, {"hallways": RECORDS})
    state = tmp_path / "s.json"

    mh.migrate(path, store=_FakeStore(), state_path=str(state))

    saved = json.loads(state.read_text())
    assert saved["imported"] == 3
    assert saved["source_size"] == (tmp_path / "hallways.json").stat().st_size


def test_unstorable_bytes_do_not_abort_the_import_and_are_reported(tmp_path):
    """One stray byte in 797K records must not abort the import (#411).

    The scrub itself lives at the storage boundary, in
    ``hallway_store._record_to_row``, so every writer gets it — the tunnel
    step and this migration alike. Duplicating it here would be a second
    copy to drift, which is the mistake #411 was filed about. What the
    migration owes the operator is *visibility*: a count of how many records
    carried bytes postgres cannot store, so a surprising number is noticed
    rather than absorbed silently.
    """
    records = [
        {"id": "ok", "wing": "w", "entity_a": "A", "entity_b": "B"},
        {"id": "bad", "wing": "w", "entity_a": "N\x00UL", "entity_b": "S\ud800urrogate"},
    ]
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({"hallways": records}, ensure_ascii=True),
        encoding="utf-8",
        errors="surrogatepass",
    )
    store = _FakeStore()

    result = mh.migrate(str(path), store=store, state_path=str(tmp_path / "s.json"))

    assert result["imported"] == 2, "a bad byte must not cost the other records"
    assert result["scrubbed"] == 1


def test_the_storage_boundary_is_what_actually_scrubs(tmp_path):
    """Pins the layer the test above deliberately does not assert on."""
    from mempalace.hallway_store import _record_to_row

    row = _record_to_row(
        {"id": "bad", "wing": "w", "entity_a": "N\x00UL", "entity_b": "S\ud800urrogate"}
    )

    assert "\x00" not in row["entity_a"]
    assert row["entity_a"] == "N�UL"
    assert row["entity_b"] == "S�urrogate"


def test_schema_is_ensured_before_the_first_insert(tmp_path):
    store = _FakeStore()
    mh.migrate(
        _write(tmp_path, {"hallways": RECORDS}),
        store=store,
        state_path=str(tmp_path / "s.json"),
    )
    assert store.schema_calls == 1


def test_batches_are_bounded(tmp_path):
    """797K single-row round trips would take hours; batch the upserts."""
    seen_batches = []

    class _Batching(_FakeStore):
        def upsert_many(self, records, conn=None):
            seen_batches.append(len(records))
            return super().upsert_many(records, conn=conn)

    path = _write(tmp_path, {"hallways": [dict(RECORDS[0], id=f"h{i}") for i in range(25)]})
    mh.migrate(path, store=_Batching(), state_path=str(tmp_path / "s.json"), batch_size=10)

    assert seen_batches == [10, 10, 5]


def test_dry_run_needs_no_configured_store(tmp_path, monkeypatch):
    """The rehearsal must be the easiest thing to run, not the hardest.

    A dry run opens no connection and writes no row, so requiring a
    configured postgres store before one exists would mean the only way to
    preview the import is to first set up the thing the preview is meant to
    de-risk. Caught by an end-to-end CLI run; every unit test above injects
    a store and so could not see it.
    """
    monkeypatch.delenv("MEMPALACE_HALLWAY_BACKEND", raising=False)
    path = _write(tmp_path, {"hallways": RECORDS})

    result = mh.migrate(path, dry_run=True, state_path=str(tmp_path / "s.json"))

    assert result["total"] == 3
    assert result["imported"] == 0


# ---------------------------------------------------------------------------
# Entity-class reporting — surface the corpus, never filter it silently
# ---------------------------------------------------------------------------


def test_dry_run_reports_entity_classes(tmp_path):
    """Most hallway entities are harvested code tokens, not names.

    Measured read-only against the live 1.14 GB file on the palace host
    (2026-09-10): of 154,692 distinct entities only 12,484 (8%) are
    word-shaped; identifier/path/url/template together are 47%. The first
    record in the store links the entity ``${this.baseUrl}/health``.

    Whether to import that is JP's call at cutover, so the migration reports
    the mix and filters nothing. A silent filter would be an irreversible
    edit to the corpus made by the tool that was only asked to move it.
    """
    records = [
        {"id": "1", "wing": "w", "entity_a": "Aya", "entity_b": "Lumi"},
        {"id": "2", "wing": "w", "entity_a": "${this.baseUrl}/health", "entity_b": "res.ok"},
        {"id": "3", "wing": "w", "entity_a": "docs/spec.md", "entity_b": "getUserName"},
        {"id": "4", "wing": "w", "entity_a": "https://example.com/x", "entity_b": "Aya"},
    ]
    path = _write(tmp_path, {"hallways": records})

    result = mh.migrate(path, dry_run=True, state_path=str(tmp_path / "s.json"))

    classes = result["entity_classes"]
    assert classes["template"] >= 1
    assert classes["path"] >= 1
    assert classes["identifier"] >= 1
    assert classes["url"] >= 1
    assert classes["word"] >= 1


def test_entity_classification():
    """The buckets themselves, so the reported table means something."""
    assert mh.classify_entity("${this.baseUrl}/health") == "template"
    assert mh.classify_entity("https://example.com/health") == "url"
    assert mh.classify_entity("docs/superpowers/specs/design.md") == "path"
    assert mh.classify_entity("getUserName") == "identifier"
    assert mh.classify_entity("base_url") == "identifier"
    assert mh.classify_entity("Aya") == "word"
    assert mh.classify_entity("memory palace") == "word"
    assert mh.classify_entity("") == "empty"


def test_migration_never_filters_entities(tmp_path):
    """Reporting is not filtering. Every record still imports."""
    records = [
        {"id": "1", "wing": "w", "entity_a": "${x}", "entity_b": "y/z"},
        {"id": "2", "wing": "w", "entity_a": "Aya", "entity_b": "Lumi"},
    ]
    store = _FakeStore()

    mh.migrate(
        _write(tmp_path, {"hallways": records}),
        store=store,
        state_path=str(tmp_path / "s.json"),
    )

    assert set(store.rows) == {"1", "2"}


# ---------------------------------------------------------------------------
# Truncated input — silent tail loss is the worst failure this can have
# ---------------------------------------------------------------------------


def test_unterminated_array_raises_instead_of_returning_short(tmp_path):
    """A file cut off at a record boundary must not read as complete.

    Killed scp, full disk, interrupted write: the array simply ends with no
    closing ``]``. Mid-record truncation already raises, but a cut exactly
    between records used to yield N records and no error — and the migration
    would then write a resume state recording N as the whole file. Silent
    tail loss on a 1 GB import, invisible until someone noticed hallways
    missing.
    """
    path = tmp_path / "cut.json"
    path.write_text(
        '{"schema_version": 1, "hallways": [{"id": "h1", "wing": "w"}, {"id": "h2", "wing": "w"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unterminated|truncat"):
        list(mh.iter_hallway_records(str(path)))


def test_unterminated_array_with_trailing_comma_also_raises(tmp_path):
    """The other boundary shape: cut right after a separator."""
    path = tmp_path / "cut2.json"
    path.write_text(
        '{"schema_version": 1, "hallways": [{"id": "h1", "wing": "w"}, ',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unterminated|truncat"):
        list(mh.iter_hallway_records(str(path)))


def test_truncation_prevents_a_resume_state_being_written(tmp_path):
    """The compounding half: a short read must not be recorded as complete."""
    path = tmp_path / "cut3.json"
    path.write_text('{"hallways": [{"id": "h1", "wing": "w"}', encoding="utf-8")
    state = tmp_path / "s.json"

    with pytest.raises(ValueError):
        mh.migrate(str(path), store=_FakeStore(), state_path=str(state), batch_size=1000)

    assert not state.exists(), "a failed read must not leave a resume point behind"


def test_a_properly_terminated_array_still_reads_clean(tmp_path):
    """Control for the two tests above — the guard must not fire on good input."""
    assert list(mh.iter_hallway_records(_write(tmp_path, {"hallways": RECORDS}))) == RECORDS
    assert list(mh.iter_hallway_records(_write(tmp_path, RECORDS))) == RECORDS


# ---------------------------------------------------------------------------
# Resume fingerprint
# ---------------------------------------------------------------------------


def test_resume_refuses_when_mtime_changed_at_the_same_size(tmp_path):
    """Size alone misses a same-length rewrite; mtime catches it cheaply."""
    path = _write(tmp_path, {"hallways": RECORDS})
    state = tmp_path / "s.json"
    mh._write_state(str(state), path, imported=2)

    import os
    import time

    stat = os.stat(path)
    os.utime(path, (stat.st_atime, stat.st_mtime + 1000))
    time.sleep(0)

    with pytest.raises(mh.MigrationStateMismatch):
        mh.migrate(path, store=_FakeStore(), state_path=str(state))
