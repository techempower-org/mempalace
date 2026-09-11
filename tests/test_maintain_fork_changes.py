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


class TestResolveHeadByFileAdd:
    """#476: `commit: HEAD` resolves from the commit that ADDED the entry
    file — the squash commit by construction, since the file arrives with
    the PR. Deterministic, offline, and needs nothing from the author,
    who cannot know the PR number when writing the entry.
    """

    def test_resolves_from_the_adding_commit(self, tmp_path):
        e = _entry(commit="HEAD")
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e],
            "HEAD",
            adding_commit=lambda path, branch: "cafe123",
            is_ancestor=lambda sha, ref: True,
        )

        assert unresolved == []
        assert changes == [(e, "cafe123")]

    def test_NEVER_touches_an_entry_that_already_has_a_sha(self, tmp_path):
        """The boundary that makes this mechanism safe.

        Every entry file that predates the one-file-per-entry split was
        created by the SPLIT's own commit. So asking "what added this
        file" about an already-resolved entry returns the migration
        commit — which would rewrite 137 correct historical shas to one
        wrong value, and that value is an ancestor of main, so it would
        pass the ancestry check forever and look right.
        """
        e = _entry(commit="9060e09")
        e["_path"] = str(tmp_path / "old.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e],
            "HEAD",
            adding_commit=lambda path, branch: pytest.fail(
                "must not ask about an entry that already has a sha"
            ),
        )

        assert (changes, unresolved) == ([], [])

    def test_disagreement_with_the_api_refuses_rather_than_choosing(self, tmp_path):
        e = _entry(commit="HEAD", fork_pr=480)
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e],
            "HEAD",
            adding_commit=lambda path, branch: "aaaaaaa",
            fetch=lambda pr: "bbbbbbb",
            is_ancestor=lambda sha, ref: True,
        )

        assert changes == []
        assert "refusing to choose" in unresolved[0][1]

    def test_agreement_with_the_api_resolves(self, tmp_path):
        e = _entry(commit="HEAD", fork_pr=480)
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e],
            "HEAD",
            adding_commit=lambda path, branch: "aaaaaaa",
            fetch=lambda pr: "aaaaaaa",
            is_ancestor=lambda sha, ref: True,
        )

        assert unresolved == []
        assert changes == [(e, "aaaaaaa")]

    def test_a_missing_adding_commit_is_reported_not_guessed(self, tmp_path):
        e = _entry(commit="HEAD")
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e], "HEAD", adding_commit=lambda path, branch: None
        )

        assert changes == []
        assert "could not find the commit" in unresolved[0][1]

    def test_an_unverifiable_fork_pr_is_a_NOTE_not_a_refusal(self, tmp_path):
        """A guessed number cannot corrupt the result, but it is still
        wrong documentation — so it is surfaced, not ignored.

        lucid wrote `fork_pr: 483` before `gh pr create` returned and the
        PR came back 490; 483 does not exist (404), so the API answer is
        None, file-add stands alone and is already right. Advisory rather
        than blocking, because an advisory that fails --check is an
        advisory someone deletes.
        """
        e = _entry(commit="HEAD", fork_pr=483)
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, notes = mfc.resolve_head_by_file_add(
            [e],
            "HEAD",
            adding_commit=lambda path, branch: "e4d52a3",
            fetch=lambda pr: None,
            is_ancestor=lambda sha, ref: True,
        )

        assert changes == [(e, "e4d52a3")], "file-add still resolves correctly"
        assert unresolved == [], "a guessed number must not block resolution"
        assert "unverifiable" in notes[0][1]


class TestFileAddAgainstRealGit:
    """The #480 case, end to end against a real repository.

    #480's own entry has `commit: HEAD` and **no** `fork_pr`, because the
    entry was written before the PR existed and a guessed number would
    have been worse than none. That is precisely the case file-add
    resolution exists for, and the tests above stub `adding_commit`, so
    they prove the wiring but not the git invocation. These drive real
    `git log`.
    """

    @staticmethod
    def _repo(tmp_path, monkeypatch):
        import subprocess as sp

        sp.run(["git", "init", "-q", str(tmp_path)], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sp.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def _commit(self, tmp_path, path, body, msg):
        import subprocess as sp

        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
        sp.run(["git", "-C", str(tmp_path), "add", path], check=True)
        sp.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", msg], check=True)
        return sp.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_no_fork_pr_resolves_to_the_commit_that_added_the_file(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        self._commit(repo, "README.md", "unrelated\n", "chore: unrelated first commit")
        rel = "docs/fork-changes/2026-09-11-fork-changes-per-entry-pipeline.yaml"
        adding = self._commit(
            repo, rel, "id: x\ncommit: HEAD\n", "refactor(docs): the change (#480)"
        )
        self._commit(repo, "other.txt", "later\n", "chore: a later commit")

        e = _entry(commit="HEAD")
        e.pop("fork_pr", None)
        e["_path"] = rel
        assert "fork_pr" not in e, "this is the no-fork_pr case"

        # verify_ref="HEAD": the temp repo has no origin/main, and this test
        # is about file-add, not the ancestry gate (covered separately). The
        # REAL git_is_ancestor still runs — against this repo's HEAD.
        changes, unresolved, notes = mfc.resolve_head_by_file_add([e], "HEAD", verify_ref="HEAD")

        assert unresolved == [], unresolved
        assert notes == [], "no fork_pr means nothing to cross-check or warn about"
        assert changes == [(e, adding)], "must be the ADDING commit, not the tip"

    def test_a_later_edit_does_not_move_the_answer(self, tmp_path, monkeypatch):
        """`--diff-filter=A` means the ADD, so editing an entry later
        (a typo fix, a reworded body) must not re-point its sha."""
        repo = self._repo(tmp_path, monkeypatch)
        rel = "docs/fork-changes/e.yaml"
        adding = self._commit(repo, rel, "id: e\ncommit: HEAD\n", "feat: add entry (#1)")
        self._commit(repo, rel, "id: e\ncommit: HEAD\nsummary: fixed typo\n", "docs: typo (#2)")

        assert mfc.git_file_add_commit(rel) == adding

    def test_a_renamed_entry_file_still_resolves_to_its_original_add(self, tmp_path, monkeypatch):
        """Entry filenames embed the date, so correcting a date renames
        the file. `--follow` keeps the original add as the answer."""
        import subprocess as sp

        repo = self._repo(tmp_path, monkeypatch)
        old_rel = "docs/fork-changes/2026-09-10-e.yaml"
        adding = self._commit(repo, old_rel, "id: e\ncommit: HEAD\n" + "x" * 200, "feat: add (#1)")
        new_rel = "docs/fork-changes/2026-09-11-e.yaml"
        sp.run(["git", "-C", str(repo), "mv", old_rel, new_rel], check=True)
        sp.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "docs: fix the date (#2)"], check=True
        )

        assert mfc.git_file_add_commit(new_rel) == adding

    def test_an_untracked_path_is_reported_not_guessed(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        self._commit(repo, "README.md", "x\n", "chore: init")

        assert mfc.git_file_add_commit("docs/fork-changes/never-committed.yaml") is None

    def test_an_unmerged_entry_is_NOT_resolved_to_its_branch_commit(self, tmp_path, monkeypatch):
        """The property that stops #472 from recurring through this door.

        Run against `origin/main` (the sweep's default), an entry whose
        file has not merged yet finds NO add — so it stays `HEAD` instead
        of being written with the branch commit, which the squash would
        orphan moments later. That is exactly the failure this whole
        mechanism replaced, and file-add could otherwise reintroduce it:
        asked about `HEAD` on a feature branch it happily returns the
        branch commit.
        """
        import subprocess as sp

        repo = self._repo(tmp_path, monkeypatch)
        self._commit(repo, "README.md", "x\n", "chore: init")
        sp.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
        sp.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
        rel = "docs/fork-changes/unmerged.yaml"
        branch_sha = self._commit(repo, rel, "id: u\ncommit: HEAD\n", "feat: not merged yet (#9)")

        # Asked about the branch, file-add finds the branch commit...
        assert mfc.git_file_add_commit(rel, "feature") == branch_sha
        # ...but asked about main — which is what the sweep uses — it finds nothing.
        assert mfc.git_file_add_commit(rel, "main") is None

        e = _entry(commit="HEAD")
        e["_path"] = rel
        changes, unresolved, _notes = mfc.resolve_head_by_file_add([e], "main", verify_ref="main")

        assert changes == [], "an unmerged entry must not be resolved"
        assert "could not find the commit" in unresolved[0][1]


class TestAncestryGateBeforeWrite:
    """Oracle's rider on #492: a candidate sha is checked against
    `origin/main` BEFORE it can be written, and refused otherwise.

    The `--branch=origin/main` default already made a branch commit hard
    to write, but a default is one argument away from being wrong. The
    question the caller actually needs answered is not "which ref did you
    ask about" but "is this commit on main" — so that is what gets
    asserted, which makes the hazard impossible rather than unlikely.
    """

    def test_a_branch_only_sha_is_refused_not_written(self, tmp_path):
        e = _entry(commit="HEAD")
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e],
            "some-feature-branch",
            adding_commit=lambda path, branch: "b4a9c11",
            is_ancestor=lambda sha, ref: False,  # not on origin/main
        )

        assert changes == [], "a sha that is not on main must never be written"
        assert "not an ancestor of origin/main" in unresolved[0][1]
        assert "refusing to write it" in unresolved[0][1]

    def test_the_gate_verifies_against_origin_main_regardless_of_branch(self, tmp_path):
        """Even asked about a branch, the VERIFY ref stays origin/main."""
        seen = []
        e = _entry(commit="HEAD")
        e["_path"] = str(tmp_path / "x.yaml")

        mfc.resolve_head_by_file_add(
            [e],
            "a-feature-branch",
            adding_commit=lambda path, branch: "abc1234",
            is_ancestor=lambda sha, ref: seen.append((sha, ref)) or True,
        )

        assert seen == [("abc1234", "origin/main")]

    def test_an_ancestor_sha_still_resolves(self, tmp_path):
        e = _entry(commit="HEAD")
        e["_path"] = str(tmp_path / "x.yaml")

        changes, unresolved, _notes = mfc.resolve_head_by_file_add(
            [e],
            "HEAD",
            adding_commit=lambda path, branch: "abc1234",
            is_ancestor=lambda sha, ref: True,
        )

        assert (changes, unresolved) == ([(e, "abc1234")], [])
