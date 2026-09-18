"""The hub forwarder must not send `mode: null` either (#525, path 1).

There are TWO daemon-ish mine payload builders, and they are mutually exclusive
**by configuration, not by input shape**:

* `cmd_mine`'s daemon-strict branch posts to the palace-daemon and then
  `sys.exit`s — so under daemon-strict the hub code below it is unreachable;
* `_forward_mine_to_hub` posts to a local `mempalace serve` hub, and only runs
  when daemon-strict is OFF and a live hub is registered for the palace.

⭐ That is why the reported 422 came from the daemon path and this one stayed
latent: the reporter's machine is daemon-strict, so it never reached here.
`_mine_args_forwardable` gates on flags only — no file-vs-directory condition —
so neither path is selected by what you point `mine` at.

This test drives path 1 directly, because no subprocess run under daemon-strict
can: the exit above it fires first.
"""

import argparse
import json

import pytest

from mempalace import cli


def _args(**kw):
    base = dict(
        dir="/tmp/some/source",
        mode=None,
        agent="probe",
        limit=0,
        dry_run=False,
        extract=None,
        wing="probe",
    )
    base.update(kw)
    return argparse.Namespace(**base)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"ok": True}).encode()


@pytest.fixture
def hub(monkeypatch):
    """A live-looking hub, with every POST body recorded."""
    from mempalace import server_registry

    monkeypatch.setattr(cli, "_hub_forward_disabled", lambda: False)
    monkeypatch.setattr(server_registry, "read_live_serverinfo", lambda p: {"port": 1})
    monkeypatch.setattr(server_registry, "client_base_url", lambda i: "http://127.0.0.1:1")

    bodies = []

    def fake_urlopen(req, timeout=None):
        if req.data:
            bodies.append(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return bodies


def _mine_arguments(bodies):
    """The mine arguments out of the JSON-RPC envelope, or fail loudly.

    Explicitly NOT `body.get("arguments", body)`: that falls back to the
    envelope, whose top level has no "mode", so a missing payload reads as
    `mode=None` — absence reported as a null value, which is precisely the
    bug under test wearing the costume of a test failure.
    """
    out = []
    for body in bodies:
        params = body.get("params")
        if not isinstance(params, dict):
            continue
        args = params.get("arguments")
        assert isinstance(args, dict), "envelope had no arguments dict: %r" % body
        out.append(args)
    assert out, "no mine payload was posted: %r" % bodies
    return out


class TestTheHubPayloadNeverCarriesANullMode:
    def test_an_unset_mode_is_normalized(self, hub):
        try:
            cli._forward_mine_to_hub(_args(mode=None), "/tmp/palace")
        except SystemExit:
            pass
        for args_ in _mine_arguments(hub):
            assert args_.get("mode") is not None, (
                "the hub payload carried mode=null — the same defect as the "
                "daemon path, one consumer over (#525)"
            )
            assert args_["mode"] == "projects"

    def test_an_explicit_mode_survives(self, hub):
        try:
            cli._forward_mine_to_hub(_args(mode="convos"), "/tmp/palace")
        except SystemExit:
            pass
        args_ = _mine_arguments(hub)[0]
        assert args_["mode"] == "convos", "a given --mode must not be rewritten"


class TestTheTwoPathsAreSelectedByConfigNotInputShape:
    def test_daemon_strict_exits_before_the_hub_code(self):
        """Structural, because it is the claim that decides the blast radius.

        The daemon-strict branch ends in `sys.exit`, and the hub forward call
        appears after it in `cmd_mine`'s body — so no input can reach both.
        """
        import ast
        import inspect

        src = inspect.getsource(cli.cmd_mine)
        tree = ast.parse(src)
        exits, forwards = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if fn == "exit":
                    exits.append(node.lineno)
                if fn == "_forward_mine_to_hub":
                    forwards.append(node.lineno)
        assert forwards, "hub forward call not found in cmd_mine"
        assert exits, "no sys.exit found in cmd_mine"
        assert min(exits) < min(forwards), (
            "the daemon-strict branch must exit before the hub forward, or a "
            "single run could take both paths"
        )

    def test_forwardability_does_not_depend_on_file_vs_directory(self):
        """`_mine_args_forwardable` gates on FLAGS only.

        Stated because the issue framed the split as file-vs-directory. It is
        not: both shapes take whichever path the configuration selects.
        """
        import inspect

        src = inspect.getsource(cli._mine_args_forwardable)
        for token in ("isdir", "isfile", ".jsonl", "splitext"):
            assert token not in src, (
                f"forwardability now inspects {token!r}; the two paths would "
                "then be selectable by input shape and this PR's reasoning "
                "about blast radius needs redoing"
            )
