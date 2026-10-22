"""#409: importing mempalace.mcp_server must not parse the IMPORTING process's argv.

Fleet finding 2026-09-03: a live `mempalace --palace X wings --json` left
MEMPALACE_PALACE_PATH set process-wide because the module-scope parser read the
CLI's argv; and `--backend not-a-real-backend` anywhere in argv made the import
itself raise KeyError. Only the server entrypoints own argv.
"""

import os
import subprocess
import sys

from mempalace import mcp_server


def test_predicate_recognizes_server_entrypoints():
    assert mcp_server._is_server_entrypoint("/x/bin/mempalace-mcp", "")
    assert mcp_server._is_server_entrypoint("/x/mempalace/mcp_server.py", "")
    assert mcp_server._is_server_entrypoint("python", "mempalace.mcp_server")
    # `python -m mempalace.mcp_server` runs the package's __main__.py (mcp_server became a package).
    assert mcp_server._is_server_entrypoint("python", "mempalace.mcp_server.__main__")
    assert mcp_server._is_server_entrypoint("python", "mempalace.mcp_proxy")


def test_predicate_rejects_other_importers():
    assert not mcp_server._is_server_entrypoint("/x/bin/mempalace", "mempalace.cli")
    assert not mcp_server._is_server_entrypoint("/x/bin/pytest", "")
    assert not mcp_server._is_server_entrypoint("main.py", "")  # the daemon


def test_env_override_forces_parsing(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MCP_PARSE_ARGV", "1")
    assert mcp_server._is_server_entrypoint("/x/bin/pytest", "")


def _import_with_argv(argv, env_extra=None):
    code = (
        "import os, sys, json\n"
        f"sys.argv = {argv!r}\n"
        "import mempalace.mcp_server as m\n"
        "print(json.dumps({'palace_env': os.environ.get('MEMPALACE_PALACE_PATH'),"
        " 'backend_env': os.environ.get('MEMPALACE_BACKEND'),"
        " 'palace_flag': bool(m._args.palace)}))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMPALACE_")}
    env["PYTHONPATH"] = os.getcwd()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    return proc


def _json_line(proc):
    """Importing mcp_server moves fd 1 onto stderr (the #225 stdio hijack), so
    the probe's JSON line may arrive on either stream."""
    import json

    for stream in (proc.stdout, proc.stderr):
        lines = [ln for ln in stream.splitlines() if ln.startswith("{")]
        if lines:
            return json.loads(lines[-1])
    raise AssertionError(f"no JSON line in output:\n{proc.stderr[-600:]}")


def test_import_from_a_cli_shaped_argv_has_no_side_effects():
    proc = _import_with_argv(
        [
            "/usr/bin/mempalace",
            "--palace",
            "/nonexistent/palace",
            "--backend",
            "not-a-real-backend",
            "wings",
        ]
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    out = _json_line(proc)
    assert out["palace_env"] is None
    assert out["backend_env"] is None
    assert out["palace_flag"] is False


def test_server_entrypoint_argv_still_parsed():
    proc = _import_with_argv(["/usr/bin/mempalace-mcp", "--palace", "/tmp/some-palace"])
    assert proc.returncode == 0, proc.stderr[-800:]
    out = _json_line(proc)
    assert out["palace_env"] == "/tmp/some-palace"
    assert out["palace_flag"] is True
