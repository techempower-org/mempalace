"""#476: entry commit shas are resolved AFTER the merge, by the merge
step — not written by the lane.

The previous resolver scanned the 12 lines after a ``commit:`` line for
any ``#NN`` and mapped that number to a recent commit. Measured on the 12
entries this repo actually needed fixed, it resolved 3 and got all three
wrong — one matched against an **upstream** issue number — and silently
left 9 unresolved while reporting success.

So the tests here mostly pin the *refusals*. Leaving an entry unresolved
is harmless and visible; a confident wrong sha is neither, and
``check-docs`` cannot catch it, because asserting a sha *resolves* is
satisfied by any real commit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fork_changes  # noqa: E402

_spec = importlib.util.spec_from_file_location("mfc", SCRIPTS / "maintain-fork-changes.py")
mfc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mfc)


def _entry(**kw):
    e = {
        "seq": 1,
        "id": "an-entry",
        "date": "2026-09-11",
        "bucket": "Fixed",
        "commit": "HEAD",
        "area": "CLI",
        "summary": "a thing changed",
    }
    e.update(kw)
    return e


class TestResolveHeadFromForkPr:
    def test_uses_the_merge_commit_sha_from_the_api(self):
        e = _entry(fork_pr=475)

        changes, unresolved = mfc.resolve_head_entries([e], fetch=lambda pr: "abcdef1")

        assert unresolved == []
        assert changes == [(e, "abcdef1")]

    def test_an_unmerged_pr_is_left_alone(self):
        """The sha does not exist yet — that is not a defect."""
        changes, unresolved = mfc.resolve_head_entries([_entry(fork_pr=999)], fetch=lambda pr: None)

        assert changes == []
        assert "not merged" in unresolved[0][1]

    def test_without_fork_pr_it_refuses_rather_than_inferring(self):
        """The old resolver inferred here, from prose, and was wrong."""
        changes, unresolved = mfc.resolve_head_entries(
            [_entry(body="fixes the thing from #433 and upstream #1829")],
            fetch=lambda pr: "should-not-be-called",
        )

        assert changes == []
        assert "no fork_pr" in unresolved[0][1]

    def test_a_non_numeric_fork_pr_is_reported_not_crashed_on(self):
        changes, unresolved = mfc.resolve_head_entries(
            [_entry(fork_pr="not-a-number")], fetch=lambda pr: "x"
        )

        assert changes == []
        assert "not a number" in unresolved[0][1]

    def test_entries_with_a_concrete_sha_are_untouched(self):
        changes, unresolved = mfc.resolve_head_entries(
            [_entry(commit="1234567")], fetch=lambda pr: "nope"
        )

        assert (changes, unresolved) == ([], [])


class TestSquashSubjectMatch:
    SUBJECTS = [
        ("aaaaaaa1", "fix(cli): do the thing (#418) (#457)"),
        ("bbbbbbb2", "fix(cli): do the thing"),
        ("ccccccc3", "feat(cli): something else (#460)"),
    ]

    def test_matches_the_squash_subject_not_the_branch_commit_itself(self):
        hits = mfc.squash_subject_match("fix(cli): do the thing", self.SUBJECTS)

        assert hits == ["aaaaaaa1"], "the identical bare subject must not match"

    def test_no_match_returns_empty_rather_than_a_near_miss(self):
        assert mfc.squash_subject_match("fix(cli): do the THING differently", self.SUBJECTS) == []


class TestRepairDangling:
    def test_repoints_a_non_ancestor_to_its_squash_commit(self):
        e = _entry(commit="dead123")
        subjects = [("5555555", "fix(cli): do the thing (#457)")]

        changes, unresolved = mfc.repair_dangling(
            [e],
            "origin/main",
            is_ancestor=lambda sha, branch: False,
            subject_of=lambda sha: "fix(cli): do the thing",
            branch_subjects=subjects,
        )

        assert unresolved == []
        assert changes == [(e, "5555555")]

    def test_an_ancestor_is_left_alone(self):
        changes, unresolved = mfc.repair_dangling(
            [_entry(commit="abc1234")],
            "origin/main",
            is_ancestor=lambda sha, branch: True,
            subject_of=lambda sha: pytest.fail("must not need a subject"),
            branch_subjects=[],
        )

        assert (changes, unresolved) == ([], [])

    def test_ambiguity_refuses_instead_of_picking(self):
        subjects = [
            ("1111111", "fix(cli): do the thing (#1)"),
            ("2222222", "fix(cli): do the thing (#2)"),
        ]

        changes, unresolved = mfc.repair_dangling(
            [_entry(commit="dead123")],
            "origin/main",
            is_ancestor=lambda sha, branch: False,
            subject_of=lambda sha: "fix(cli): do the thing",
            branch_subjects=subjects,
        )

        assert changes == []
        assert "2 subject matches" in unresolved[0][1]

    def test_a_vanished_object_is_reported_not_guessed(self):
        changes, unresolved = mfc.repair_dangling(
            [_entry(commit="dead123")],
            "origin/main",
            is_ancestor=lambda sha, branch: False,
            subject_of=lambda sha: None,
            branch_subjects=[],
        )

        assert changes == []
        assert "object is gone" in unresolved[0][1]

    def test_an_allowlisted_id_with_the_matching_sha_is_skipped(self):
        """Documented-unrecoverable entries must not re-report forever."""
        changes, unresolved = mfc.repair_dangling(
            [_entry(id="old-thing", commit="dead123")],
            "origin/main",
            is_ancestor=lambda sha, branch: False,
            subject_of=lambda sha: pytest.fail("must not be consulted"),
            branch_subjects=[],
            legacy_ids={"old-thing": "dead123"},
        )

        assert (changes, unresolved) == ([], [])

    def test_an_allowlisted_id_whose_sha_CHANGED_is_no_longer_skipped(self):
        """The exemption covers one known-bad sha, not the entry forever.

        Otherwise an allowlisted entry could be edited to any sha at all
        — including a plausible wrong one, which would be an ancestor and
        pass every check — and never be looked at again.
        """
        changes, unresolved = mfc.repair_dangling(
            [_entry(id="old-thing", commit="beef999")],
            "origin/main",
            is_ancestor=lambda sha, branch: False,
            subject_of=lambda sha: "some subject",
            branch_subjects=[],
            legacy_ids={"old-thing": "dead123"},
        )

        assert changes == []
        assert "not an ancestor" in unresolved[0][1], "must be reported, not skipped"


class TestLegacyFile:
    """The allowlist is keyed on ``(id, sha)``, never on id alone.

    Keyed on the id only, an allowlisted entry's ``commit:`` could later
    be changed to anything at all and stay skipped forever — the file
    would grant a permanent exemption to the entry rather than recording
    one known-bad sha. Pinning the sha means a resolved (or corrupted)
    entry starts failing the check until its line is removed, which is
    exactly the prompt we want.
    """

    def test_parses_id_and_sha_pairs(self, tmp_path):
        p = tmp_path / "legacy.txt"
        p.write_text("# a comment\n\nfirst-id  abc1234   # note\nsecond-id\tdef5678\n")

        assert mfc.load_legacy_ids(p) == {"first-id": "abc1234", "second-id": "def5678"}

    def test_an_id_without_a_sha_is_rejected_not_silently_exempted(self, tmp_path):
        p = tmp_path / "legacy.txt"
        p.write_text("lonely-id\n")

        with pytest.raises(ValueError, match="needs '<id> <sha>'"):
            mfc.load_legacy_ids(p)

    def test_absent_file_is_empty_not_an_error(self, tmp_path):
        assert mfc.load_legacy_ids(tmp_path / "nope.txt") == {}


class TestWriteBack:
    def test_applying_a_change_rewrites_only_the_commit_field(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mfc, "gh_merge_commit_sha", lambda pr, repo=None: "fedcba9")
        e = _entry(commit="HEAD", fork_pr=475, body="keep\nthis\n")
        path = fork_changes.write_entry(e, tmp_path)

        rc = mfc.main(
            [
                "--dir",
                str(tmp_path),
                "--no-repair",
                "--legacy-file",
                str(tmp_path / "none.txt"),
            ]
        )

        after = yaml.safe_load(path.read_text())
        assert rc == 0
        assert after["body"] == "keep\nthis\n"
        assert after["summary"] == e["summary"]
        assert after["commit"] == "fedcba9"


def test_check_mode_fails_when_an_entry_is_unresolved(tmp_path, capsys):
    fork_changes.write_entry(_entry(commit="HEAD"), tmp_path)

    rc = mfc.main(
        ["--check", "--dir", str(tmp_path), "--no-repair", "--legacy-file", str(tmp_path / "n.txt")]
    )

    assert rc == 1, "an unresolved HEAD must not read as success"
    assert "no fork_pr" in capsys.readouterr().err
