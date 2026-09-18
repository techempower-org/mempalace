"""check-docs step 4 must examine EVERY mention of a PR, not just the first (#516).

`scripts/check-docs.sh` did `grep … | head -1` per document, so only the first
line mentioning a PR number contributed to the doc's claimed state. A drifted
claim appearing *after* a correct one was invisible — the check answered "does
the first mention agree?" rather than "do all mentions agree?".

Reproduced against the real repo before the fix (2026-09-17): appending
``PR #1377 is still open upstream.`` to the end of README.md, with #1377 MERGED
upstream, left check-docs reporting ``✓ all 245 PR references match upstream
state``. The appended line was verified to be inside the instrument's match set
first — a control that is present but never looked at produces "did not fire"
for the wrong reason, which is indistinguishable from "no drift".

These tests drive the real script over a fixture tree with a stubbed ``gh``, so
the line-selection and claim logic is what is under test rather than the network.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-docs.sh"


def _fixture_repo(tmp_path: Path, docs: dict, pr_states: dict) -> Path:
    """A minimal git repo carrying the three docs check-docs reads, plus stubs.

    ``gh`` answers from ``pr_states`` and nothing else, so no network and no
    shared-quota API calls. ``pytest`` is stubbed because step 1 fails hard
    without one and would muddy the exit code.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "check-docs.sh")
    for name, text in docs.items():
        (root / name).write_text(text)
    for name in ("README.md", "CLAUDE.md", "FORK_CHANGELOG.md"):
        if not (root / name).exists():
            (root / name).write_text("placeholder\n")

    cases = "\n".join(f'    {n}) echo "{s}" ;;' for n, s in pr_states.items())
    (root / "bin" / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  auth) exit 0 ;;\n"
        "  pr)\n"
        '    for a in "$@"; do case "$a" in [0-9]*) n="$a"; break;; esac; done\n'
        '    case "$n" in\n' + cases + "\n    esac\n"
        "    exit 0 ;;\n"
        "esac\nexit 0\n"
    )
    (root / "bin" / "pytest").write_text("#!/usr/bin/env bash\necho '0 tests collected'\n")
    for stub in ("gh", "pytest"):
        (root / "bin" / stub).chmod(0o755)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _run(root: Path) -> str:
    env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}")
    proc = subprocess.run(
        ["bash", "scripts/check-docs.sh"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout + proc.stderr


_FINDING = re.compile(r"PR #(\d+) is (MERGED|OPEN|CLOSED)")


def _step4(output: str) -> str:
    """The PR-state findings, matched by shape rather than by position.

    An earlier version of this helper sliced the text between the "4/7" and
    "5/7" headings. That was wrong and silently so: findings are printed to
    STDERR while headings go to STDOUT, so in the combined capture every
    finding lands after step 7 and the slice was always empty — every
    "no finding" assertion would have passed without running anything.
    ``test_drift_on_the_first_mention_still_detected`` is the control that
    keeps this honest: it must pass even BEFORE the #516 fix, which is only
    possible if the harness can see a finding at all.
    """
    return "\n".join(line for line in output.splitlines() if _FINDING.search(line))


@pytest.mark.skipif(not SCRIPT.exists(), reason="check-docs.sh not present")
class TestEveryMentionIsChecked:
    def test_drift_after_earlier_mentions_is_detected(self, tmp_path):
        """The #516 regression itself, in the shape it actually occurs.

        Earlier mentions are a bare reference and a multi-PR narrative line —
        neither makes a clean claim — and the stale claim comes last. Under
        ``head -1`` only the bare reference was read, so the check concluded
        "no claim" and passed. This is the real README's shape for #1377:
        line 30 references it without a claim, lines 225/446/458 each name
        another PR, and only the appended line claims a state.
        """
        readme = (
            "Intro line with no PR reference.\n"
            "Retry-once landed via upstream #1377 in v3.3.5.\n"
            "Superseded by #1377, which closed #1286.\n"
            "PR #1377 is still open upstream.\n"
        )
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" in _step4(_run(root))

    def test_a_correct_and_a_stale_claim_together_stay_ambiguous(self, tmp_path):
        """A KNOWN LIMIT, asserted so it cannot be mistaken for coverage.

        When one clean line claims the true state and another claims a
        different one, the check cannot tell drift from history and skips.
        Measured reason: `#1024` in FORK_CHANGELOG.md is exactly this shape —
        "pushed to the open #1024 PR branch (squash-merged upstream)" plus an
        authoritative "(MERGED)" — and is correct documentation of a MERGED
        PR. It is the only such pair in the repo. Flagging disagreement would
        fire on it, so the rule stays, and this test records the cost.
        """
        readme = (
            "No reference here.\n"
            "[#1377](https://github.com/MemPalace/mempalace/pull/1377) was merged.\n"
            "PR #1377 is still open upstream.\n"
        )
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))

    def test_correct_mention_only_stays_clean(self, tmp_path):
        """Negative control — the same shape without the stale claim."""
        readme = (
            "Intro line with no PR reference.\n"
            "[#1377](https://github.com/MemPalace/mempalace/pull/1377) was merged.\n"
            "Some prose in between.\n"
            "PR #1377 was merged upstream.\n"
        )
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))

    def test_drift_on_the_first_mention_still_detected(self, tmp_path):
        """What the check already caught must keep being caught."""
        readme = "PR #1377 is still open upstream.\nLater unrelated prose.\n"
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" in _step4(_run(root))

    def test_mention_at_end_of_line_is_seen(self, tmp_path):
        """A right-boundary regex must not require a trailing character."""
        readme = "No reference here.\nStill open per upstream #1377\n"
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" in _step4(_run(root))


@pytest.mark.skipif(not SCRIPT.exists(), reason="check-docs.sh not present")
class TestNoNewFalsePositives:
    def test_commentary_line_mentioning_other_prs_is_ignored(self, tmp_path):
        """Iterating every line must not turn narrative into a finding: a line
        naming several PRs cannot be attributed to any one of them.

        The claim on this line is deliberately ``open`` — the state that WOULD
        fire against a MERGED PR. An earlier version of this test used a line
        claiming ``closed``, which the MERGED branch ignores anyway, so it
        passed with the commentary skip removed and was not evidence of
        anything.
        """
        readme = "No reference here.\nSuperseded by #1377, which is open pending #1286.\n"
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))

    def test_open_inside_another_word_is_not_a_claim(self, tmp_path):
        """ "opencode" and "reopened" contain "open" but claim nothing.

        Latent before #516 because only one line per doc was examined; reading
        every line amplifies it. Measured on the real repo: #108 and #110
        became findings purely because their lines mention "opencode".
        """
        readme = (
            "No reference here.\nPR #1377 attempted `enabled: false` but opencode ignored it.\n"
        )
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))

    def test_reopened_is_not_an_open_claim(self, tmp_path):
        readme = "No reference here.\nPR #1377 was reopened and then landed.\n"
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))

    def test_a_longer_number_does_not_match_a_shorter_one(self, tmp_path):
        """`#101` must not inherit the claim on a line about `#1018`.

        This is the real shape, taken from the repo rather than invented.
        Without a right boundary `/45` matches inside `/459efab`, and that
        line carries no other `#NNNN` so the multi-PR commentary skip does not
        save it — the check read "open-and-refuse sequence" as a claim that
        #45 is OPEN, off a line about a commit hash.

        Two earlier versions of this test were not evidence: one never
        mentioned the short number at all (so it was never queried), and one
        used `#1018`, where the commentary skip catches it anyway and the
        boundary is never exercised. Both passed with the boundary removed.
        """
        readme = (
            "Tracking #45 for later; no state claimed here.\n"
            "See [`459efab`](https://github.com/o/r/commit/459efab) for the"
            " open-and-refuse sequence.\n"
        )
        root = _fixture_repo(tmp_path, {"README.md": readme}, {45: "MERGED"})
        assert "PR #45 is" not in _step4(_run(root))

    def test_a_doc_with_no_claim_at_all_is_clean(self, tmp_path):
        readme = "Mentions #1377 without saying anything about its state.\n"
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))

    def test_both_states_across_lines_is_treated_as_narrative(self, tmp_path):
        """A changelog legitimately says a PR was open and later merged. That is
        ambiguous, not drift — flagging it would make history unwritable."""
        readme = (
            "No reference here.\n"
            "PR #1377 was open for three weeks.\n"
            "PR #1377 was merged on the 6th.\n"
        )
        root = _fixture_repo(tmp_path, {"README.md": readme}, {1377: "MERGED"})
        assert "#1377" not in _step4(_run(root))
