"""Execution tests for Claude plugin hook wrapper scripts.

Post-2026-05-11 the wrappers are thin pass-throughs to palace-daemon's
``clients/hook.py``. The OLD test contract (which asserted the wrapper
invoked ``mempalace`` CLI fallbacks) was deleted in this rewrite — see
jphein/mempalace#68 for the CI-red incident.

The wrappers honor ``PALACE_DAEMON_HOOK_PY`` for override (CI fixtures,
non-default deployments). Production behavior is unchanged: env unset →
hardcoded ``/home/jp/Projects/palace-daemon/clients/hook.py``.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_HOOKS_DIR = REPO_ROOT / ".claude-plugin" / "hooks"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="bash required for Claude plugin hook wrapper tests",
)

SCRIPT_CASES = [
    ("mempal-stop-hook.sh", "stop"),
    ("mempal-precompact-hook.sh", "precompact"),
]


def _shell_path(path: Path) -> str:
    return path.as_posix()


def _run_hook(
    script_name: str,
    payload: str,
    hook_py: Optional[Path],
) -> subprocess.CompletedProcess:
    """Run the wrapper script with PALACE_DAEMON_HOOK_PY pointing at hook_py
    (or a guaranteed-missing path when hook_py is None)."""
    assert BASH is not None

    env = os.environ.copy()
    if hook_py is not None:
        env["PALACE_DAEMON_HOOK_PY"] = str(hook_py)
    else:
        env["PALACE_DAEMON_HOOK_PY"] = "/tmp/palace-daemon-hook-py-does-not-exist"
    env.pop("MEMPALACE_PYTHON", None)

    return subprocess.run(
        [BASH, _shell_path(PLUGIN_HOOKS_DIR / script_name)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
    )


@pytest.mark.parametrize(("script_name", "hook_name"), SCRIPT_CASES)
def test_wrapper_execs_hook_py_with_correct_args(
    tmp_path: Path, script_name: str, hook_name: str
) -> None:
    """When PALACE_DAEMON_HOOK_PY points to a real file + python3 is on PATH,
    the wrapper execs python3 with --hook <name> --harness claude-code."""
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.json"

    # A fake hook.py that records the args + stdin it received
    fake_hook_py = tmp_path / "hook.py"
    fake_hook_py.write_text(
        "import sys\n"
        f"open({str(args_file)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        f"open({str(stdin_file)!r}, 'w').write(sys.stdin.read())\n",
        encoding="utf-8",
    )

    payload = '{"session_id":"abc123"}'
    result = _run_hook(script_name, payload, hook_py=fake_hook_py)

    assert result.returncode == 0, (
        f"wrapper returned {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert args_file.read_text(encoding="utf-8") == (f"--hook {hook_name} --harness claude-code")
    assert stdin_file.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize(("script_name", "hook_name"), SCRIPT_CASES)
def test_wrapper_exits_zero_when_hook_py_missing(
    tmp_path: Path, script_name: str, hook_name: str
) -> None:
    """Missing hook.py = silent no-op (exit 0). The wrapper must not error
    on hosts that lack palace-daemon — a Stop event from such a host should
    just pass through without disturbing the harness."""
    payload = '{"session_id":"no-runner"}'
    result = _run_hook(script_name, payload, hook_py=None)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(("script_name", "hook_name"), SCRIPT_CASES)
def test_wrapper_passes_through_extra_args(
    tmp_path: Path, script_name: str, hook_name: str
) -> None:
    """Trailing args from the harness flow through to hook.py via "$@"."""
    args_file = tmp_path / "args.txt"
    fake_hook_py = tmp_path / "hook.py"
    fake_hook_py.write_text(
        f"import sys\nopen({str(args_file)!r}, 'w').write(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PALACE_DAEMON_HOOK_PY"] = str(fake_hook_py)

    assert BASH is not None
    result = subprocess.run(
        [BASH, _shell_path(PLUGIN_HOOKS_DIR / script_name), "--extra-flag", "extra-value"],
        input="{}",
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "--extra-flag extra-value" in args_file.read_text(encoding="utf-8")
