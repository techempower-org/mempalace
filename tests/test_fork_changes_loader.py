"""#473: fork-change entries live one-per-file so concurrent PRs touch
disjoint files.

The monolithic ``docs/fork-changes.yaml`` had a single insertion point at
the top of ``entries:``, so every PR in a wave conflicted with every
other one there — measured across a 10-PR wave on 2026-09-10/11 with
zero source conflicts.

Presentation order therefore has to come from the data rather than from
position in a shared file. It is ``seq`` descending, ties broken on
``(date desc, id asc)``. The important property is that a *collision* in
``seq`` is harmless: two lanes both choosing ``max + 1`` land in separate
files and order deterministically, so ordering stops being a
coordination point without becoming implicit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fork_changes  # noqa: E402


def _write(dirpath: Path, entry: dict) -> Path:
    p = dirpath / fork_changes.entry_filename(entry)
    p.write_text(yaml.safe_dump(entry, sort_keys=False, allow_unicode=True))
    return p


def _entry(id_, seq, date="2026-09-10", **kw):
    e = {
        "id": id_,
        "seq": seq,
        "date": date,
        "bucket": "Fixed",
        "commit": "abc1234",
        "area": "CLI",
        "summary": f"summary for {id_}",
    }
    e.update(kw)
    return e


class TestOrdering:
    def test_seq_descending_is_newest_first(self, tmp_path):
        for e in (_entry("older", 1), _entry("newer", 3), _entry("middle", 2)):
            _write(tmp_path, e)

        ids = [e["id"] for e in fork_changes.load_entries(tmp_path)]

        assert ids == ["newer", "middle", "older"]

    def test_a_seq_collision_is_deterministic_not_an_error(self, tmp_path):
        """Two lanes both picking max+1 must not break the build, and must
        not order differently between runs — that is the whole reason
        ordering is allowed to stay explicit."""
        _write(tmp_path, _entry("bravo", 9))
        _write(tmp_path, _entry("alpha", 9))

        first = [e["id"] for e in fork_changes.load_entries(tmp_path)]
        second = [e["id"] for e in fork_changes.load_entries(tmp_path)]

        assert first == second == ["alpha", "bravo"], "tie breaks on id ascending"

    def test_date_breaks_ties_before_id(self, tmp_path):
        _write(tmp_path, _entry("aaa", 5, date="2026-01-01"))
        _write(tmp_path, _entry("zzz", 5, date="2026-06-01"))

        assert [e["id"] for e in fork_changes.load_entries(tmp_path)] == ["zzz", "aaa"]

    def test_missing_seq_sorts_last_rather_than_crashing(self, tmp_path):
        e = _entry("no-seq", 0)
        del e["seq"]
        _write(tmp_path, e)
        _write(tmp_path, _entry("has-seq", 1))

        assert [x["id"] for x in fork_changes.load_entries(tmp_path)] == ["has-seq", "no-seq"]


class TestFilenames:
    def test_filename_is_date_then_id(self):
        assert (
            fork_changes.entry_filename({"id": "my-change", "date": "2026-09-10"})
            == "2026-09-10-my-change.yaml"
        )

    def test_filename_accepts_a_date_object(self):
        import datetime

        name = fork_changes.entry_filename({"id": "x", "date": datetime.date(2026, 9, 10)})
        assert name == "2026-09-10-x.yaml"


class TestValidation:
    def test_duplicate_ids_across_files_are_rejected(self, tmp_path):
        """The dedup pass in maintain-fork-changes existed because rebase
        chains re-inserted entries. Split files make that a hard error we
        can detect instead of a silent duplicate."""
        (tmp_path / "2026-09-10-dup.yaml").write_text(
            yaml.safe_dump(_entry("dup", 1), sort_keys=False)
        )
        (tmp_path / "2026-09-11-dup.yaml").write_text(
            yaml.safe_dump(_entry("dup", 2, date="2026-09-11"), sort_keys=False)
        )

        with pytest.raises(fork_changes.ManifestError, match="duplicate entry id"):
            fork_changes.load_entries(tmp_path)

    def test_entry_without_an_id_is_rejected(self, tmp_path):
        (tmp_path / "2026-09-10-bad.yaml").write_text(yaml.safe_dump({"date": "2026-09-10"}))

        with pytest.raises(fork_changes.ManifestError, match="missing 'id'"):
            fork_changes.load_entries(tmp_path)

    def test_non_yaml_files_are_ignored(self, tmp_path):
        _write(tmp_path, _entry("real", 1))
        (tmp_path / "README.md").write_text("not an entry")
        (tmp_path / ".gitkeep").write_text("")

        assert [e["id"] for e in fork_changes.load_entries(tmp_path)] == ["real"]

    def test_missing_directory_is_an_explicit_error(self, tmp_path):
        with pytest.raises(fork_changes.ManifestError, match="not found"):
            fork_changes.load_entries(tmp_path / "nope")


class TestRoundTrip:
    def test_dump_then_load_preserves_a_multiline_body_exactly(self, tmp_path):
        """Rendered markdown depends on the body STRING, so a round trip
        that reflows it would silently change FORK_CHANGELOG output."""
        body = (
            "First paragraph that runs on for a while and would be a\n"
            "candidate for re-wrapping by a naive dumper.\n"
            "\n"
            "Second paragraph with ``inline code`` and a — dash.\n"
        )
        e = _entry("round", 1, body=body, tests="3 (TestThing)", files=["a/b.py"])
        (tmp_path / fork_changes.entry_filename(e)).write_text(fork_changes.dump_entry(e))

        loaded = fork_changes.load_entries(tmp_path)[0]

        assert loaded["body"] == body
        assert loaded["files"] == ["a/b.py"]
        assert loaded["tests"] == "3 (TestThing)"

    def test_dump_uses_block_style_for_bodies_so_diffs_stay_readable(self, tmp_path):
        e = _entry("blocky", 1, body="line one\nline two\n")

        text = fork_changes.dump_entry(e)

        assert "body: |" in text, f"expected a block scalar, got:\n{text}"


class TestMeta:
    """``merged_upstream`` is not a per-entry field and must survive the
    split — it renders the changelog's closing sections, which silently
    vanished the first time the loader returned only ``entries``.

    It lives NEXT TO the entries directory rather than inside it: it is
    edited when an upstream PR merges, not once per fork PR, so a shared
    file there does not reintroduce the wave conflict #473 is about.
    """

    def test_meta_is_merged_into_the_manifest(self, tmp_path):
        entries = tmp_path / "fork-changes"
        entries.mkdir()
        _write(entries, _entry("only", 1))
        (tmp_path / "fork-changes-meta.yaml").write_text(
            yaml.safe_dump({"merged_upstream": {"1024": {"title": "x"}}})
        )

        m = fork_changes.load_manifest(entries)

        assert [e["id"] for e in m["entries"]] == ["only"]
        assert m["merged_upstream"]["1024"]["title"] == "x"

    def test_absent_meta_is_not_an_error(self, tmp_path):
        entries = tmp_path / "fork-changes"
        entries.mkdir()
        _write(entries, _entry("only", 1))

        m = fork_changes.load_manifest(entries)

        assert m["entries"] and m.get("merged_upstream", {}) == {}


class TestIncludeHead:
    """#476: the strict check has to be able to SEE `commit: HEAD`.

    The exclusion lives in the enumeration, which is why hardening the
    ancestry predicate alone could not catch a missing sha — an excluded
    entry never reaches it. (`git merge-base --is-ancestor HEAD HEAD`
    also exits 0, so both halves had to learn about the literal.)
    """

    def test_head_is_excluded_by_default(self, tmp_path):
        _write(tmp_path, _entry("pending", 2, commit="HEAD"))
        _write(tmp_path, _entry("done", 1, commit="abc1234"))

        refs = fork_changes.iter_commit_refs(fork_changes.load_entries(tmp_path))

        assert refs == [("done", "abc1234")]

    def test_include_head_emits_it(self, tmp_path):
        _write(tmp_path, _entry("pending", 2, commit="HEAD"))
        _write(tmp_path, _entry("done", 1, commit="abc1234"))

        refs = fork_changes.iter_commit_refs(fork_changes.load_entries(tmp_path), include_head=True)

        assert ("pending", "HEAD") in refs
        assert ("done", "abc1234") in refs

    def test_an_empty_commit_is_never_emitted(self, tmp_path):
        _write(tmp_path, _entry("blank", 1, commit=""))

        assert (
            fork_changes.iter_commit_refs(fork_changes.load_entries(tmp_path), include_head=True)
            == []
        )
