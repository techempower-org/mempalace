"""check-docs must run the two checks that used to run only by hand or only in CI.

* The failure-shape record set (`scripts/failure_shape_recall.py --check-set-only`)
  had passed at every step of #531 — by hand. That stops being true the first time
  someone edits the spec's runnable table without touching the files.
* markdownlint ran only in CI (`lint-docs.yml`), so MD052 reached CI on #531's pre-squash
  tree with every local check-docs step green. A lint that runs only in CI is a check
  nobody ran.

Both steps are driven through the REAL `scripts/check-docs.sh` over a throwaway repo,
with a stub for the set check and the real markdownlint runner when one is present.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-docs.sh"

RUNNER = shutil.which("markdownlint-cli2") or (
    str(Path.home() / ".npm-global" / "bin" / "markdownlint-cli2")
    if (Path.home() / ".npm-global" / "bin" / "markdownlint-cli2").exists()
    else None
)

WORKFLOW = """\
name: Lint docs
jobs:
  markdownlint:
    steps:
      - uses: DavidAnson/markdownlint-cli2-action@v23
        with:
          globs: |
            README.md
            docs/**/*.md
"""


def _fixture_repo(tmp_path: Path, *, set_check_rc: int, docs: dict) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "docs").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "check-docs.sh")
    (root / "scripts" / "failure_shape_recall.py").write_text(
        f"import sys\nprint('stub set check')\nsys.exit({set_check_rc})\n"
    )
    (root / ".github" / "workflows" / "lint-docs.yml").write_text(WORKFLOW)
    for name in ("README.md", "CLAUDE.md", "FORK_CHANGELOG.md"):
        (root / name).write_text("# placeholder\n")
    for name, text in docs.items():
        (root / name).write_text(text)
    (root / "bin" / "gh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (root / "bin" / "pytest").write_text("#!/usr/bin/env bash\necho '0 tests collected'\n")
    for stub in ("gh", "pytest"):
        (root / "bin" / stub).chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _run(root: Path, *, with_runner: bool = True) -> "tuple[int, str]":
    path = f"{root / 'bin'}:{os.environ['PATH']}"
    env = dict(os.environ, PATH=path)
    if not with_runner:
        env["PATH"] = ":".join(
            p for p in path.split(":") if not (Path(p) / "markdownlint-cli2").exists()
        )
        env["HOME"] = str(root)  # no ~/.npm-global fallback either
    proc = subprocess.run(
        ["bash", "scripts/check-docs.sh"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.skipif(not SCRIPT.exists(), reason="check-docs.sh not present")
class TestRecordSetStep:
    def test_a_failing_set_check_fails_check_docs(self, tmp_path):
        root = _fixture_repo(tmp_path, set_check_rc=1, docs={"docs/a.md": "# a\n"})
        rc, out = _run(root, with_runner=False)
        assert rc == 1
        assert "failure-shape record set" in out and "stub set check" in out

    def test_a_passing_set_check_is_reported_ok(self, tmp_path):
        """Positive control on the step: it must be able to pass, not only to fail."""
        root = _fixture_repo(tmp_path, set_check_rc=0, docs={"docs/a.md": "# a\n"})
        rc, out = _run(root, with_runner=False)
        assert "failure-shape record set" in out
        assert "record set matches the spec" in out


@pytest.mark.skipif(not SCRIPT.exists(), reason="check-docs.sh not present")
class TestMarkdownlintStep:
    def test_globs_come_from_the_ci_workflow(self, tmp_path):
        """One source for local and CI: the step must name the globs it read."""
        root = _fixture_repo(tmp_path, set_check_rc=0, docs={"docs/a.md": "# a\n"})
        _, out = _run(root, with_runner=False)
        assert "2 globs from .github/workflows/lint-docs.yml" in out

    def test_runner_absent_is_a_warning_not_a_pass(self, tmp_path):
        root = _fixture_repo(tmp_path, set_check_rc=0, docs={"docs/a.md": "# a\n"})
        rc, out = _run(root, with_runner=False)
        assert "markdownlint-cli2 not found" in out
        assert rc == 0

    @pytest.mark.skipif(RUNNER is None, reason="markdownlint-cli2 not installed")
    def test_the_md052_ci_found_on_531s_pre_squash_tree_fails_locally(self, tmp_path):
        """Positive control: the exact shape CI caught — `[+-][^+-]` read as a reference link."""
        bad = "# r\n\nthe prose has `[+-][^+-]` and then [+-][^+-] bare\n"
        root = _fixture_repo(tmp_path, set_check_rc=0, docs={"docs/bad.md": bad})
        rc, out = _run(root)
        assert rc == 1
        assert "MD052" in out and "docs/bad.md" in out

    @pytest.mark.skipif(RUNNER is None, reason="markdownlint-cli2 not installed")
    def test_clean_docs_pass_the_lint_step(self, tmp_path):
        """Negative control: the step is not a constant red."""
        root = _fixture_repo(tmp_path, set_check_rc=0, docs={"docs/ok.md": "# ok\n\nfine\n"})
        rc, out = _run(root)
        assert rc == 0, out
        assert "markdownlint clean" in out
