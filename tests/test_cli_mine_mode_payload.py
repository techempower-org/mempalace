"""No daemon mine payload may carry ``mode: null`` (#525).

Upstream v3.8 made ``--mode`` default to ``None`` so an unset flag could inherit
a per-source default. ``cmd_mine`` normalizes it once at the top —
``mode = getattr(args, "mode", None) or "projects"`` — under a comment saying
every branch below *and the daemon payload* read it. One branch did not: the
daemon-strict call passed ``args.mode`` raw, 128 lines after the normalization,
while the sibling payload eight lines earlier used the resolved value.

The daemon's ``MineBody`` then rejected it:

    422  {"loc": ["body", "mode"], "msg": "Input should be a valid string",
          "input": None}

⭐ Measured before the fix, daemon-strict, no ``--mode`` given: **every input
shape that reached the POST sent null** — a directory with or without
``--wing``, with ``--background``, with ``--no-tunnels``, and a prose file. The
one shape that did *not* 422 was a ``.jsonl`` transcript, and only because it
never reached the daemon at all: the mineable-path guard stops it locally with
exit 2, which is separate, intended behaviour.

These tests assert on what goes **on the wire**, because that is where the
contract lives — the producer is ``cli.py`` and the consumer is the daemon's
``search_models.MineBody``, in another repository. A unit test that inspected a
local variable would have passed throughout the outage.
"""

import ast
import inspect
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mempalace import cli


# ── a stub daemon that records what it is sent ─────────────────────────


class _Recorder(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"__unparseable__": raw}
        type(self).bodies.append(body)
        # Mirror MineBody: a null mode is a 422, exactly as production answers.
        if body.get("mode", "") is None:
            self.send_response(422)
            self.end_headers()
            self.wfile.write(b'{"detail":[{"loc":["body","mode"]}]}')
            return
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"queued": true}')

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):
        pass


@pytest.fixture
def daemon():
    """A real HTTP daemon on an ephemeral port, recording every body."""
    _Recorder.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, _Recorder.bodies
    srv.shutdown()


@pytest.fixture
def workspace(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.md").write_text("# notes\n\nsome prose for the miner\n")
    (src / "a.jsonl").write_text('{"type":"user","message":{"content":"hi"}}\n')
    home = tmp_path / "home"
    (home / ".mempalace").mkdir(parents=True)
    return src, home


def _run(srv, home, argv):
    """Invoke the REAL CLI, daemon-strict, against the stub."""
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PALACE_DAEMON_URL": "http://127.0.0.1:%d" % srv.server_address[1],
            "PALACE_API_KEY": "x",
            "PALACE_DAEMON_STRICT": "1",
            "MEMPALACE_AGENT_NAME": "probe",
        }
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    return subprocess.run(
        [sys.executable, "-m", "mempalace.cli", "mine"] + argv,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# ── the wire ───────────────────────────────────────────────────────────


class TestNoMinePayloadCarriesANullMode:
    @pytest.mark.parametrize(
        "shape",
        ["dir", "dir --wing", "dir --background", "dir --no-tunnels", "prose file"],
    )
    def test_the_posted_mode_is_always_a_string(self, daemon, workspace, shape):
        srv, bodies = daemon
        src, home = workspace
        argv = {
            "dir": [str(src)],
            "dir --wing": [str(src), "--wing", "probe"],
            "dir --background": [str(src), "--wing", "probe", "--background"],
            "dir --no-tunnels": [str(src), "--wing", "probe", "--no-tunnels"],
            "prose file": [str(src / "notes.md"), "--wing", "probe"],
        }[shape]

        proc = _run(srv, home, argv)

        assert bodies, "no POST reached the daemon for %s: %s" % (shape, proc.stderr[-300:])
        for body in bodies:
            assert body.get("mode") is not None, (
                "%s put mode=null on the wire — MineBody answers 422 (#525)" % shape
            )
            assert isinstance(body["mode"], str)
        assert proc.returncode == 0, proc.stderr[-300:]

    def test_an_explicit_mode_is_passed_through_unchanged(self, daemon, workspace):
        """The control: the bug was a MISSING flag, so a given one must survive."""
        srv, bodies = daemon
        src, home = workspace
        proc = _run(srv, home, [str(src), "--wing", "probe", "--mode", "convos"])
        assert proc.returncode == 0, proc.stderr[-300:]
        assert [b["mode"] for b in bodies] == ["convos"]


# ── the seam refuses rather than guesses ───────────────────────────────


class TestTheSeamRefusesANullMode:
    def test_post_daemon_mine_cli_raises_on_none(self):
        """Substituting the default here would trade a loud 422 for a
        silently wrong corpus: a caller that meant "projects" would mine in
        "convos" and nothing would say so."""
        with pytest.raises(ValueError, match="must be a string, not None"):
            cli._post_daemon_mine_cli("/tmp/x", wing="w", mode=None)

    def test_the_message_names_the_caller_s_obligation(self):
        with pytest.raises(ValueError) as exc:
            cli._post_daemon_mine_cli("/tmp/x", wing="w", mode=None)
        assert "resolve it at the call site" in str(exc.value)


# ── structural: no builder may read the raw attribute ──────────────────


class TestNoPayloadBuilderReadsArgsModeRaw:
    def test_no_raw_args_mode_read_survives(self):
        """Enumerate raw `args.mode` READS, not two syntactic shapes.

        ⭐ The audit that found only one of the two bugs walked dict literals
        with a `"mode"` key. That finds the hub payload and the daemon
        payload and MISSES the daemon-strict call, because that one is a
        call KEYWORD — `_post_daemon_mine_cli(..., mode=args.mode, ...)`.
        Matching on the shape of the *value* instead catches every position,
        including ones nobody has written yet.

        `cmd_repair` assigns `args.mode = "from-sqlite"`; that is a Store on
        a different subcommand's flag, not a read of the mine flag, so
        `ctx` is what separates them — hence the isinstance check rather
        than a name match.
        """
        tree = ast.parse(inspect.getsource(cli))
        reads = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and n.attr == "mode"
            and isinstance(n.value, ast.Name)
            and n.value.id == "args"
            and isinstance(n.ctx, ast.Load)
        ]
        assert reads == [], (
            "raw `args.mode` read at line(s) %s — --mode defaults to None "
            "upstream, so any unnormalized read is #525 again" % reads
        )

    def test_the_audit_itself_would_have_caught_the_call_keyword(self):
        """A control on the instrument: the dict-literal-only audit passes
        on the pre-fix source while the read-based one fails. Without this,
        "the audit is clean" means nothing."""
        broken = "def f(args):\n    return g(mode=args.mode)\n"
        tree = ast.parse(broken)

        dict_only = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Dict)
            for k in n.keys
            if isinstance(k, ast.Constant) and k.value == "mode"
        ]
        reads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and n.attr == "mode"
            and isinstance(n.value, ast.Name)
            and n.value.id == "args"
            and isinstance(n.ctx, ast.Load)
        ]
        assert dict_only == [], "the weaker audit sees nothing here"
        assert len(reads) == 1, "the read-based audit sees the call keyword"
