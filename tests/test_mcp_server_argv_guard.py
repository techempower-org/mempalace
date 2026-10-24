"""#409: importing mempalace.mcp_server must not parse the IMPORTING process's argv.

Fleet finding 2026-09-03: a live `mempalace --palace X wings --json` left
MEMPALACE_PALACE_PATH set process-wide because the module-scope parser read the
CLI's argv; and `--backend not-a-real-backend` anywhere in argv made the import
itself raise KeyError. Only the server entrypoints own argv.

Since the 2026-10 upstream sync this is upstream's design (#2528/#2534): importing the package parses NO argv,
and main() (plus the other entry points) applies --palace/--backend/--read-only with _apply_server_flags(). It
replaces the fork's _is_server_entrypoint() guard, which an exec-fragment package import could no longer satisfy
for `python -m`.
"""

import os
import subprocess
import sys


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


def test_import_with_a_server_shaped_argv_still_has_no_side_effects():
    """Parsing happens in main(), not at import, even for the server's own argv (#2528)."""
    proc = _import_with_argv(["/usr/bin/mempalace-mcp", "--palace", "/tmp/some-palace"])
    assert proc.returncode == 0, proc.stderr[-800:]
    out = _json_line(proc)
    assert out["palace_env"] is None
    assert out["palace_flag"] is False


def test_python_dash_m_server_honours_palace_flag(tmp_path):
    """End to end: `python -m mempalace.mcp_server --palace X` serves X. The package split once dropped the flag
    for `python -m` launches, because the import ran before __main__ was visible."""
    import json

    palace = tmp_path / "mypalace"
    palace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "mempalace_status", "arguments": {}},
        },
    ]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("MEMPALACE_") and k != "XDG_CONFIG_HOME"
    }
    env["HOME"] = str(home)
    env["PYTHONPATH"] = os.getcwd()
    proc = subprocess.run(
        [sys.executable, "-m", "mempalace.mcp_server", "--palace", str(palace)],
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    replies = {
        r["id"]: r
        for r in map(json.loads, [ln for ln in proc.stdout.splitlines() if ln.startswith("{")])
    }
    status_text = replies[2]["result"]["content"][0]["text"]
    assert str(palace) in status_text, status_text[:400]
    assert str(home) not in status_text, "served the default palace, not --palace"
