"""Every merged fork PR must carry a docs/fork-changes entry (#519).

`scripts/check-docs.sh` verified render parity, sha resolution, sha ancestry
and upstream PR states — but never that a merged fork PR left an entry behind.
#517 was green on every check with no entry at all, and nothing would have
surfaced it later: `--next-seq` and the renderers are happy with any subset.
The checker answered a narrower question than its name (#505's class, and
#516's twin).

These tests drive the REAL `scripts/check-entry-coverage.sh` over throwaway
git repos built under the project's own `tmp/` (never /tmp, which is a 16 GB
tmpfs on this workstation).

The producer/consumer pair, executed together in
`test_every_allowlisted_pr_is_genuinely_missing_an_entry`:
`scripts/maintain-fork-changes.py` is the sweep that resolves landed
`commit: HEAD` values — it is a docs-tooling PR that adds no entry BY DESIGN,
so it is the reason the allowlist exists. This check is its consumer. An
allowlist that drifts from what the sweep actually produces would silence a
real miss, so the allowlist is verified against the repo rather than trusted.

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_check_docs_entry_coverage.py -q
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-entry-coverage.sh"
ALLOWLIST = REPO / "docs" / "fork-changes-no-entry.txt"


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
        },
    )


@pytest.fixture
def fixture_repo(tmp_path_factory):
    """A throwaway git repo under the PROJECT's tmp/, not /tmp (tmpfs = RAM)."""
    root = REPO / "tmp"
    root.mkdir(exist_ok=True)
    base = Path(tmp_path_factory.mktemp("entrycov", numbered=True))
    # mktemp lands in pytest's basetemp; relocate under the project tmp/.
    repo = root / base.name
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "docs" / "fork-changes").mkdir(parents=True)
    (repo / "seed").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()
    yield repo, baseline
    shutil.rmtree(repo, ignore_errors=True)


def _squash(repo, subject, n):
    (repo / f"f{n}").write_text(str(n))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{subject} (#{n})")


def _entry(repo, name, fork_pr):
    (repo / "docs" / "fork-changes" / f"{name}.yaml").write_text(
        f"seq: 1\nid: {name}\ndate: '2026-09-18'\nbucket: Fixed\n"
        f"commit: HEAD\nfork_pr: {fork_pr}\narea: CLI\n"
        f'summary: "x"\nbody: |\n  x\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"docs: entry for {fork_pr}")


def _run(repo, baseline, *extra):
    return subprocess.run(
        ["bash", str(SCRIPT), "--baseline", baseline, *extra],
        cwd=repo,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# the step, over a fixture repo
# ---------------------------------------------------------------------------


def test_a_merged_pr_without_an_entry_is_named(fixture_repo):
    repo, baseline = fixture_repo
    _squash(repo, "feat: covered", 101)
    _entry(repo, "covered", 101)
    _squash(repo, "feat: forgotten", 102)

    r = _run(repo, baseline)

    assert "102" in r.stdout + r.stderr
    assert "feat: forgotten" in r.stdout + r.stderr, "the PR title must be printed"
    assert "101" not in re.sub(r"#101\b", "", r.stdout + r.stderr).replace("102", "")


def test_a_fully_covered_repo_is_clean(fixture_repo):
    """Positive control: the step must be capable of reporting nothing."""
    repo, baseline = fixture_repo
    _squash(repo, "feat: covered", 101)
    _entry(repo, "covered", 101)

    r = _run(repo, baseline)

    assert r.returncode == 0
    assert "101" not in r.stderr


def test_the_step_is_not_vacuous_on_an_empty_range(fixture_repo):
    """A step that reports nothing because it looked at nothing proves nothing.

    With the baseline at HEAD there are no squash commits, so the output must
    say so explicitly rather than silently passing.
    """
    repo, baseline = fixture_repo
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    r = _run(repo, head)

    assert r.returncode == 0
    assert "0" in r.stdout, "the step must report how many squash commits it examined"


def test_warn_only_by_default_and_strict_fails(fixture_repo):
    repo, baseline = fixture_repo
    _squash(repo, "feat: forgotten", 102)

    warn = _run(repo, baseline)
    strict = _run(repo, baseline, "--strict")

    assert warn.returncode == 0, "first landing is warn-only"
    assert strict.returncode == 1, "--strict must fail"


def test_an_allowlisted_pr_is_not_reported(fixture_repo):
    repo, baseline = fixture_repo
    _squash(repo, "ci: tooling only", 103)
    (repo / "docs" / "fork-changes-no-entry.txt").write_text(
        "# NNN  reason\n\n103  CI/docs tooling; no user-visible change\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "allowlist")

    r = _run(repo, baseline, "--strict")

    assert r.returncode == 0
    assert "103" not in r.stderr


def test_allowlist_comments_and_blanks_are_ignored(fixture_repo):
    repo, baseline = fixture_repo
    _squash(repo, "ci: tooling only", 104)
    (repo / "docs" / "fork-changes-no-entry.txt").write_text(
        "\n# a comment mentioning 999\n\n   \n104  reason here\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "allowlist")

    r = _run(repo, baseline, "--strict")

    assert r.returncode == 0, r.stdout + r.stderr


def test_an_allowlist_line_without_a_reason_is_refused(fixture_repo):
    """An allowlist is a record of WHY, or it is just a mute button."""
    repo, baseline = fixture_repo
    _squash(repo, "ci: tooling only", 105)
    (repo / "docs" / "fork-changes-no-entry.txt").write_text("105\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "allowlist")

    r = _run(repo, baseline, "--strict")

    assert r.returncode == 1
    assert "105" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# producer / consumer, on the REAL repo
# ---------------------------------------------------------------------------


def test_every_allowlisted_pr_is_genuinely_missing_an_entry():
    """The allowlist must not drift into a mute button.

    Producer: scripts/maintain-fork-changes.py — the sweep, a docs-tooling PR
    that adds no entry by design. Consumer: this check. If a PR is allowlisted
    but DOES have an entry, the line is dead and hides nothing; if it is
    allowlisted but is not even a squash commit in range, it is a typo that
    would silence a real miss the day that number lands.
    """
    assert ALLOWLIST.exists(), "the allowlist file must exist"
    entries = set()
    for f in (REPO / "docs" / "fork-changes").glob("*.yaml"):
        m = re.search(r"^fork_pr:\s*(\d+)", f.read_text(), re.M)
        if m:
            entries.add(m.group(1))

    baseline = re.search(r"^BASELINE=([0-9a-f]+)", SCRIPT.read_text(), re.M).group(1)
    log = subprocess.run(
        ["git", "log", "--oneline", f"{baseline}..HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    squashed = set(re.findall(r"\(#(\d+)\)\s*$", log, re.M))

    allowlisted = []
    for line in ALLOWLIST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        allowlisted.append(line.split()[0])

    assert allowlisted, "control: the allowlist has at least one entry to check"
    for pr in allowlisted:
        assert pr in squashed, f"#{pr} is allowlisted but is not a squash commit since {baseline}"
        assert pr not in entries, f"#{pr} is allowlisted but HAS an entry — dead line"
