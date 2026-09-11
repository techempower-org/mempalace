"""SessionStart hook: the compact branch (techempower-org/mempalace#449).

The hook must route ``source="compact"`` to ``mempalace.compact_recovery``
and pass its payload through verbatim, and must leave every other source on
the ordinary status-line path. A stub interpreter stands in for the venv so
the test needs neither a daemon nor the real package.
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "hooks", "palace-session-start.sh")
)

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="hook requires bash + jq",
)

_STUB = """#!/usr/bin/env bash
# Stands in for `python -m mempalace.compact_recovery`.
cat > "$MARKER_STDIN"
printf '%s' "$MARKER_ARGS" > /dev/null
echo '{"systemMessage":"RECOVERED","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"THE STORY"}}'
"""


def _stub_python(tmp_path, body=_STUB):
    py = tmp_path / "fake-python"
    py.write_text(body)
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return py


def _run(stdin_obj, env):
    return subprocess.run(
        ["bash", HOOK],
        input=json.dumps(stdin_obj),
        capture_output=True,
        text=True,
        env=env,
    )


def _env(tmp_path, py):
    e = dict(os.environ)
    e.update(
        {
            "MEMPALACE_DIR": str(tmp_path),
            "MEMPALACE_PYTHON": str(py),
            "MARKER_STDIN": str(tmp_path / "stdin.json"),
            "MARKER_ARGS": "",
            # Point the status path at a dead port so the non-compact case
            # cannot hang on the real daemon.
            "PALACE_DAEMON_URL": "http://127.0.0.1:1",
        }
    )
    return e


def test_compact_source_emits_the_recovery_payload(tmp_path):
    py = _stub_python(tmp_path)
    r = _run(
        {"session_id": "s1", "cwd": "/home/jp/Projects/memorypalace", "source": "compact"},
        _env(tmp_path, py),
    )
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["systemMessage"] == "RECOVERED"
    assert payload["hookSpecificOutput"]["additionalContext"] == "THE STORY"


def test_compact_branch_forwards_the_hook_json_on_stdin(tmp_path):
    py = _stub_python(tmp_path)
    _run(
        {"session_id": "s1", "cwd": "/home/jp/Projects/memorypalace", "source": "compact"},
        _env(tmp_path, py),
    )
    forwarded = json.loads((tmp_path / "stdin.json").read_text())
    assert forwarded["session_id"] == "s1"
    assert forwarded["source"] == "compact"


def test_non_compact_source_never_runs_the_recovery(tmp_path):
    py = _stub_python(tmp_path)
    r = _run(
        {"session_id": "s1", "cwd": "/home/jp/Projects/memorypalace", "source": "startup"},
        _env(tmp_path, py),
    )
    assert r.returncode == 0
    assert not (tmp_path / "stdin.json").exists()
    assert "RECOVERED" not in r.stdout


def test_a_failing_recovery_falls_through_to_the_status_line(tmp_path):
    py = _stub_python(tmp_path, "#!/usr/bin/env bash\nexit 1\n")
    r = _run(
        {"session_id": "s1", "cwd": "/home/jp/Projects/memorypalace", "source": "compact"},
        _env(tmp_path, py),
    )
    assert r.returncode == 0
    # Daemon is a dead port, so the fall-through prints the unreachable line —
    # the point is that the session still gets valid JSON, never an empty exit.
    assert json.loads(r.stdout)["systemMessage"]
