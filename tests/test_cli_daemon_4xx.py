"""A 4xx from the daemon is a request error, not an outage (#499).

Reproduced 2026-09-17: `mempalace list --wing 2g --room diary --limit 2`
printed

    palace daemon unreachable at http://familiar.lan:8085 — see mempalace
    status for diagnostics (daemon REST /list failed (400): Bad Request)

while the daemon was up and answering. The error was named for the layer
that noticed rather than the layer that failed, and sent the operator to
check daemon health instead of their own argument.

⭐ The daemon was never the problem and it already said so. Measured against
production:

    GET /list?wing=2g&room=diary  ->  400
    {"detail": {"error": "room 'diary' is not in the canonical set",
                "valid_rooms": ["architecture", ... ]}}

The message the operator needed was on the wire; the client discarded it and
substituted a guess about the daemon's health. So the fix is not to write a
better message — it is to stop throwing away the one that arrived.

Per `cli.py`'s header contract a bad argument is **64**, not 2 ("palace
unavailable") and not 1.
"""

import argparse
import io
import json
import urllib.error
from unittest.mock import patch

import pytest


BAD_ARGS = 64
PALACE_UNAVAILABLE = 2


def _http_error(code, body, reason="Bad Request"):
    return urllib.error.HTTPError(
        "http://d:8085/list", code, reason, {}, io.BytesIO(json.dumps(body).encode())
    )


_ROOM_DETAIL = {
    "detail": {
        "error": "room 'diary' is not in the canonical set",
        "valid_rooms": ["architecture", "decisions", "discoveries"],
    }
}


def _list_args(**overrides):
    defaults = {
        "wing": "2g",
        "room": "diary",
        "limit": 2,
        "json": False,
        "quiet": False,
        "palace": None,
        "tags": None,
        "since": None,
        "before": None,
        "format": None,
        "cursor": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── the transport layer keeps the two classes apart ────────────────────


@pytest.fixture(autouse=True)
def _a_daemon_url(monkeypatch):
    """`_call_daemon_rest` builds its URL before it ever calls urlopen."""
    monkeypatch.setattr("mempalace.cli._daemon_url", lambda: "http://d:8085")


class TestRequestErrorsAreDistinctFromOutages:
    def test_a_4xx_raises_a_request_error(self):
        from mempalace.cli import DaemonError, DaemonRequestError, _call_daemon_rest

        with patch("urllib.request.urlopen", side_effect=_http_error(400, _ROOM_DETAIL)):
            with pytest.raises(DaemonRequestError) as exc:
                _call_daemon_rest("/list", {"room": "diary"})

        assert isinstance(exc.value, DaemonError), "still a DaemonError for old handlers"
        assert exc.value.status == 400

    def test_it_carries_the_daemon_s_own_message(self):
        from mempalace.cli import DaemonRequestError, _call_daemon_rest

        with patch("urllib.request.urlopen", side_effect=_http_error(400, _ROOM_DETAIL)):
            with pytest.raises(DaemonRequestError) as exc:
                _call_daemon_rest("/list", {"room": "diary"})

        text = str(exc.value)
        assert "not in the canonical set" in text, "the daemon's message must survive"
        assert "diary" in text

    def test_a_plain_text_4xx_body_still_works(self):
        """Not every 4xx is a FastAPI JSON envelope."""
        from mempalace.cli import DaemonRequestError, _call_daemon_rest

        err = urllib.error.HTTPError(
            "http://d:8085/list", 422, "Unprocessable", {}, io.BytesIO(b"limit too large")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(DaemonRequestError) as exc:
                _call_daemon_rest("/list", {})
        assert "limit too large" in str(exc.value)

    def test_a_network_failure_is_still_an_outage(self):
        from mempalace.cli import DaemonError, DaemonRequestError, _call_daemon_rest

        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            with pytest.raises(DaemonError) as exc:
                _call_daemon_rest("/list", {})
        assert not isinstance(exc.value, DaemonRequestError), "an outage is not a bad request"

    def test_404_401_403_still_fall_through_to_none(self):
        """Older daemons lack routes; that is a fallback, not a user error."""
        from mempalace.cli import _call_daemon_rest

        for code in (404, 401, 403):
            with patch("urllib.request.urlopen", side_effect=_http_error(code, {}, "nope")):
                assert _call_daemon_rest("/list", {}) is None


# ── what the operator sees ─────────────────────────────────────────────


class TestListRendersARequestError:
    def _run(self, args, err):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_rest", side_effect=err),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_list(args)
        return exc.value.code

    def test_a_400_exits_64_not_2(self):
        from mempalace.cli import DaemonRequestError

        code = self._run(
            _list_args(),
            DaemonRequestError("room 'diary' is not in the canonical set", status=400),
        )
        assert code == BAD_ARGS, "a bad argument is 64 per cli.py's contract"

    def test_it_does_not_blame_the_daemon(self, capsys):
        from mempalace.cli import DaemonRequestError

        self._run(
            _list_args(),
            DaemonRequestError("room 'diary' is not in the canonical set", status=400),
        )
        err = capsys.readouterr().err
        assert "unreachable" not in err.lower(), "the daemon answered; it is not unreachable"
        assert "mempalace status" not in err, "do not send them to check daemon health"
        assert "not in the canonical set" in err, "show what the daemon actually said"
        assert "400" in err

    def test_an_outage_still_reads_as_an_outage(self, capsys):
        from mempalace.cli import DaemonError

        code = self._run(_list_args(), DaemonError("daemon unreachable at http://d:8085"))
        assert code != BAD_ARGS
        assert "unreachable" in capsys.readouterr().err.lower()

    def test_json_callers_get_the_shape_too(self, capsys):
        from mempalace.cli import DaemonRequestError

        code = self._run(
            _list_args(json=True),
            DaemonRequestError("room 'diary' is not in the canonical set", status=400),
        )
        assert code == BAD_ARGS
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "bad_request"
        assert payload["status"] == 400
        assert "canonical" in payload["detail"]
