"""One 4xx contract and one JSON shape, across every daemon-facing family.

#518 — 401/403 are **64** (the daemon answered and refused a well-formed
request); 404 stays **2** (the route does not exist here, which is
unavailability for that verb and the signal six call sites use to fall back
to MCP). Before this, `_call_daemon_rest` and `_patch_daemon_rest` returned
``None`` for 404, 401 and 403 alike, so the three were indistinguishable to
every caller. Measured on 7ed80f03 against a stub daemon:

    stats  401 -> 2   403 -> 2   404 -> 2     all "palace daemon unreachable"
    wings  401 -> 2              404 -> 2     same sentence

#521 — every daemon-failure JSON carries ``error`` (prose), ``code`` (a
branchable key) and ``source``, plus ``status`` when an HTTP exchange
produced it. ``error`` holds prose in *every* writer, so the 20+ readers that
print ``.error`` are unaffected; ``code`` is additive (option C).

The families are enumerated from the source rather than hand-listed, because
a hand-listed sample is what let the two conventions drift: the issue named
three writers and an AST scan finds eleven literal sites in two shapes, none
of which is ``_fail_daemon`` itself (it builds its dict then ``.update()``s,
so no literal scan sees the largest emitter at all).
"""

import ast
import json
import pathlib
import urllib.error
from unittest.mock import patch

import pytest

CLI = pathlib.Path(__file__).resolve().parents[1] / "mempalace" / "cli.py"

# The documented keys, from the contract block in cli.py's header.
BRANCHABLE_CODES = {
    "daemon_unreachable",
    "daemon_error",
    "bad_request",
    "auth_failed",
    "read_only",
    "route_missing",
    "daemon_unavailable",
    "daemon_required",
    "daemon_busy",
}


def _http_error(code):
    def raise_it(req, timeout=None):
        return_url = getattr(req, "full_url", "http://x")
        raise urllib.error.HTTPError(return_url, code, "stub", hdrs=None, fp=None)

    return raise_it


@pytest.fixture(autouse=True)
def _daemon_url(monkeypatch):
    """A URL must exist or urllib rejects the bare path before urlopen runs —
    the patch would never be reached and the test would fail for the wrong
    reason."""
    monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PALACE_API_KEY", "stub")


class TestTheTransportCarriesTheStatus:
    """The producer half of the pair: `_call_daemon_rest` / `_patch_daemon_rest`."""

    @pytest.mark.parametrize("fn", ["_call_daemon_rest", "_patch_daemon_rest"])
    def test_404_still_returns_none_for_the_mcp_fallback(self, fn):
        from mempalace import cli

        f = getattr(cli, fn)
        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            assert f("/anything", {}) is None

    @pytest.mark.parametrize("fn", ["_call_daemon_rest", "_patch_daemon_rest"])
    @pytest.mark.parametrize("code", [401, 403])
    def test_401_and_403_raise_a_refusal_that_carries_the_status(self, fn, code):
        from mempalace import cli

        f = getattr(cli, fn)
        with patch("urllib.request.urlopen", side_effect=_http_error(code)):
            with pytest.raises(cli.DaemonAuthError) as exc:
                f("/anything", {})
        assert exc.value.status == code
        assert exc.value.code == "auth_failed"
        # Subclass of the refusal type, so every site that already routes
        # DaemonRequestError to 64 picks this up without knowing about auth.
        assert isinstance(exc.value, cli.DaemonRequestError)


class TestEveryFamilyAgreesOnTheCode:
    """The consumer half: one 4xx answer per family, not six."""

    def _exit(self, cmd, args, code):
        from mempalace import cli

        with patch("urllib.request.urlopen", side_effect=_http_error(code)):
            with pytest.raises(SystemExit) as exc:
                getattr(cli, cmd)(args)
        return exc.value.code

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refused_credential_is_64(self, code, monkeypatch):
        from mempalace import cli

        monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("PALACE_API_KEY", "x")
        monkeypatch.setattr(cli, "_daemon_strict", lambda: True)
        import argparse

        args = argparse.Namespace(json=True, palace=None, quiet=True, format="json")
        assert self._exit("cmd_status", args, code) == 64

    def test_a_missing_route_is_2(self, monkeypatch):
        from mempalace import cli

        monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
        monkeypatch.setattr(cli, "_daemon_strict", lambda: True)
        import argparse

        args = argparse.Namespace(json=True, palace=None, quiet=True, format="json")
        # 404 -> None -> the MCP fallback runs and also fails against the stub,
        # which is still "could not run" = 2, never 64.
        assert self._exit("cmd_status", args, 404) == 2


class TestTheJsonShapeIsOneShape:
    def _payload(self, capsys, err, want_json=True):
        from mempalace import cli

        with pytest.raises(SystemExit) as exc:
            cli._fail_daemon(err, want_json)
        return exc.value.code, json.loads(capsys.readouterr().out)

    def test_an_outage_carries_prose_in_error_and_a_key_in_code(self, capsys):
        from mempalace import cli

        code, payload = self._payload(capsys, cli.DaemonError("boom"), True)
        assert code == 2
        assert payload["error"] == "boom"  # prose, for the 20+ readers
        assert payload["code"] == "daemon_unreachable"  # key, for branching
        assert payload["source"] == "daemon"

    def test_a_refusal_carries_the_status_too(self, capsys):
        from mempalace import cli

        code, payload = self._payload(
            capsys, cli.DaemonAuthError("nope", status=401, detail="nope"), True
        )
        assert code == 64
        assert payload["code"] == "auth_failed"
        assert payload["status"] == 401
        assert payload["error"] == "nope"
        assert payload["source"] == "daemon"

    def test_every_code_we_emit_is_in_the_documented_set(self):
        """The key is only branchable if the set is closed and written down.

        Enumerated from the source, so a new writer with an undocumented key
        fails here rather than reaching a caller that cannot branch on it.
        """
        tree = ast.parse(CLI.read_text())
        emitted = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "code":
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        emitted.add(v.value)
        undocumented = emitted - BRANCHABLE_CODES
        assert not undocumented, (
            f"these `code` values are emitted but not in the header contract: "
            f"{sorted(undocumented)}"
        )

    def test_the_window_family_keeps_message_for_one_release(self):
        """Deprecated, not deleted — removing it in the same release that adds
        `code` would break the readers #512 created."""
        src = CLI.read_text()
        assert '"message"' in src, "the window family's deprecated `message` key is gone"


class TestDoctorNamesTheCauseInsteadOfLeakingIt:
    """`doctor` never raises, which is why it hid this for so long.

    Before #518 a 401 reached ``data.get(...)`` on a ``None`` and the
    AttributeError was caught by the blanket handler and rendered as
    ``unreachable @ <url>: 'NoneType' object has no attribute 'get'`` — a
    Python error shown to an operator as an outage.

    Measured during this change: making 401/403 raise **relocated** that leak
    to 404 rather than removing it, because 404 still returns ``None`` and the
    ``.get`` still ran. Both halves are covered here.
    """

    def _daemon_line(self, capsys, code, monkeypatch):
        from mempalace import cli

        monkeypatch.setattr(cli, "_daemon_url", lambda: "http://127.0.0.1:9")
        import argparse

        with patch("urllib.request.urlopen", side_effect=_http_error(code)):
            with pytest.raises(SystemExit):
                cli.cmd_doctor(argparse.Namespace(json=True))
        payload = json.loads(capsys.readouterr().out)
        return next(c for c in payload["checks"] if c["check"] == "daemon")

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refused_credential_is_not_called_unreachable(self, capsys, code, monkeypatch):
        check = self._daemon_line(capsys, code, monkeypatch)
        assert check["ok"] is False
        assert "unreachable" not in check["detail"].lower(), check["detail"]
        assert "PALACE_API_KEY" in check["detail"]
        assert str(code) in check["detail"]

    def test_a_missing_route_says_older_daemon_not_nonetype(self, capsys, monkeypatch):
        check = self._daemon_line(capsys, 404, monkeypatch)
        assert check["ok"] is False
        assert "NoneType" not in check["detail"], (
            f"the AttributeError is leaking into the operator's line: {check['detail']!r}"
        )
        assert "older daemon" in check["detail"]

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_no_status_leaks_a_python_exception_string(self, capsys, code, monkeypatch):
        """The general form of the defect, not just the two known strings."""
        check = self._daemon_line(capsys, code, monkeypatch)
        for leak in ("object has no attribute", "Traceback", "NoneType"):
            assert leak not in check["detail"], f"{leak!r} in {check['detail']!r}"
