"""
test_cli_duplicate.py — ``mempalace duplicate check`` (slice of #191, #363).

Binds ``mempalace_check_duplicate`` to the shell for pre-ingest guards and
dedup audits. Three behaviours carry most of the weight:

  * a completed check exits 0 whatever the verdict — the answer lives in
    ``is_duplicate``, not in an overloaded exit code;
  * ``--fail-on-duplicate`` opts into the scriptable guard (exit 1);
  * ``vector_disabled`` always exits 2. The tool refuses to answer when
    the HNSW index is gone rather than report a false "not a duplicate",
    and the CLI must not launder that into a success.

Mirrors ``test_cli_tunnels.py`` patterns.
"""

import argparse
import json
from unittest.mock import patch

import pytest


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _mcp_envelope(payload) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
    ).encode()


def _make_responder(payload, captured: list | None = None):
    def fake_urlopen(req, timeout=None):
        if captured is not None and getattr(req, "data", None) is not None:
            captured.append(json.loads(req.data.decode()))
        return _FakeResp(_mcp_envelope(payload))

    return fake_urlopen


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_DAEMON_STRICT": "1"}


def _args(**overrides):
    defaults = {
        "duplicate_action": "check",
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "content": "We chose pgvector over ChromaDB.",
        "content_file": None,
        "threshold": None,
        "fail_on_duplicate": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_NO_MATCH = {"is_duplicate": False, "matches": []}

_MATCHES = {
    "is_duplicate": True,
    "matches": [
        {
            "id": "drawer_memorypalace_decisions_aaa111",
            "wing": "memorypalace",
            "room": "decisions",
            "similarity": 0.987,
            "content": "We chose pgvector over ChromaDB.",
        },
        {
            "id": "drawer_memorypalace_architecture_bbb222",
            "wing": "memorypalace",
            "room": "architecture",
            "similarity": 0.912,
            "content": "pgvector replaced ChromaDB.",
        },
    ],
}


class TestDuplicateTableOutput:
    def test_no_match_reports_threshold(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_NO_MATCH)):
                cli.cmd_duplicate(_args())

        out = capsys.readouterr().out
        assert "No duplicate found at threshold 0.9" in out

    def test_matches_render_as_a_table(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_MATCHES)):
                cli.cmd_duplicate(_args())

        out = capsys.readouterr().out
        assert "DUPLICATES — 2 at threshold 0.9" in out
        assert "memorypalace" in out
        assert "decisions" in out
        assert "0.987" in out
        assert "0.912" in out

    def test_long_drawer_ids_are_truncated_not_wrapped(self, capsys):
        from mempalace import cli

        payload = {
            "is_duplicate": True,
            "matches": [
                {
                    "id": "drawer_" + ("x" * 90),
                    "wing": "w",
                    "room": "decisions",
                    "similarity": 0.95,
                }
            ],
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(payload)):
                cli.cmd_duplicate(_args())

        out = capsys.readouterr().out
        assert "…" in out
        assert max(len(line) for line in out.splitlines()) < 100

    def test_missing_similarity_renders_placeholder(self, capsys):
        from mempalace import cli

        payload = {"is_duplicate": True, "matches": [{"id": "d1", "wing": "w", "room": "r"}]}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(payload)):
                cli.cmd_duplicate(_args())

        assert "—" in capsys.readouterr().out


class TestDuplicateJsonOutput:
    def test_json_is_tool_passthrough(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_MATCHES)):
                cli.cmd_duplicate(_args(format="json"))

        assert json.loads(capsys.readouterr().out) == _MATCHES

    def test_json_shorthand_via_legacy_flag(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_NO_MATCH)):
                cli.cmd_duplicate(_args(json=True))

        assert json.loads(capsys.readouterr().out)["is_duplicate"] is False


class TestDuplicateArguments:
    def test_default_threshold_is_sent(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_NO_MATCH, captured=captured),
            ):
                cli.cmd_duplicate(_args())

        sent = captured[0]["params"]["arguments"]
        assert captured[0]["params"]["name"] == "mempalace_check_duplicate"
        assert sent["threshold"] == 0.9
        assert sent["content"] == "We chose pgvector over ChromaDB."

    def test_explicit_threshold_overrides(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_NO_MATCH, captured=captured),
            ):
                cli.cmd_duplicate(_args(threshold=0.75))

        assert captured[0]["params"]["arguments"]["threshold"] == 0.75

    def test_file_content_is_read_byte_exact(self, tmp_path):
        from mempalace import cli

        body = "first\r\nsecond\n"
        source = tmp_path / "candidate.txt"
        source.write_bytes(body.encode())

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_NO_MATCH, captured=captured),
            ):
                cli.cmd_duplicate(_args(content=None, content_file=str(source)))

        assert captured[0]["params"]["arguments"]["content"] == body

    def test_missing_content_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen") as urlopen:
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_duplicate(_args(content=None))

        assert ex.value.code == 2
        urlopen.assert_not_called()
        assert "--content" in capsys.readouterr().err

    def test_content_and_file_conflict_exits_2(self, capsys, tmp_path):
        from mempalace import cli

        source = tmp_path / "x.txt"
        source.write_text("x")
        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_duplicate(_args(content="inline", content_file=str(source)))

        assert ex.value.code == 2
        assert "not both" in capsys.readouterr().err

    def test_unreadable_file_exits_2(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_duplicate(_args(content=None, content_file=str(tmp_path / "nope.txt")))

        assert ex.value.code == 2
        assert "ERROR" in capsys.readouterr().err


class TestDuplicateThresholdValidation:
    """``--threshold`` is a 0-1 similarity, not a percentage."""

    def test_accepts_in_range(self):
        from mempalace import cli

        assert cli._unit_float("0.85") == 0.85
        assert cli._unit_float("0") == 0.0
        assert cli._unit_float("1") == 1.0

    def test_rejects_out_of_range(self):
        from mempalace import cli

        for bad in ("90", "1.5", "-0.1"):
            with pytest.raises(argparse.ArgumentTypeError):
                cli._unit_float(bad)

    def test_rejects_non_numeric(self):
        from mempalace import cli

        with pytest.raises(argparse.ArgumentTypeError):
            cli._unit_float("high")


class TestDuplicateExitCodes:
    def test_duplicate_found_exits_0_by_default(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_MATCHES)):
                cli.cmd_duplicate(_args())

        # No SystemExit raised — a completed check is a success.
        assert "DUPLICATES" in capsys.readouterr().out

    def test_fail_on_duplicate_exits_1(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_MATCHES)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_duplicate(_args(fail_on_duplicate=True))

        assert ex.value.code == 1
        assert "DUPLICATES" in capsys.readouterr().out

    def test_fail_on_duplicate_exits_0_when_novel(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_NO_MATCH)):
                cli.cmd_duplicate(_args(fail_on_duplicate=True))

        assert "No duplicate found" in capsys.readouterr().out


class TestDuplicateVectorDisabled:
    """A palace that cannot answer must never read as "not a duplicate"."""

    _DISABLED = {
        "is_duplicate": False,
        "matches": [],
        "vector_disabled": True,
        "vector_disabled_reason": "HNSW index missing",
        "hint": "duplicate detection requires vector search; run `mempalace repair` to restore",
    }

    def test_exits_2_and_explains(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(self._DISABLED)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_duplicate(_args())

        assert ex.value.code == 2
        out = capsys.readouterr().out
        assert "UNAVAILABLE" in out
        assert "HNSW index missing" in out
        assert "mempalace repair" in out
        # Crucially, it must NOT claim the content is novel.
        assert "No duplicate found" not in out

    def test_exits_2_in_json_mode_too(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(self._DISABLED)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_duplicate(_args(format="json"))

        assert ex.value.code == 2
        assert json.loads(capsys.readouterr().out)["vector_disabled"] is True


class TestDuplicateRouting:
    def test_palace_flag_uses_local_handler(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_check_duplicate", return_value=_NO_MATCH
            ) as handler:
                with patch("urllib.request.urlopen") as urlopen:
                    cli.cmd_duplicate(_args(palace=str(tmp_path), threshold=0.8))

        urlopen.assert_not_called()
        handler.assert_called_once_with(content="We chose pgvector over ChromaDB.", threshold=0.8)
        assert "No duplicate found at threshold 0.8" in capsys.readouterr().out

    def test_local_json_lands_on_stdout(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_check_duplicate", return_value=_MATCHES):
                cli.cmd_duplicate(_args(palace=str(tmp_path), format="json"))

        captured = capsys.readouterr()
        assert json.loads(captured.out) == _MATCHES
        assert captured.err == ""


class TestDuplicateFailureModes:
    def test_daemon_unreachable_exits_2(self, capsys):
        import urllib.error

        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_duplicate(_args())

        assert ex.value.code == 2
        assert "palace daemon unreachable" in capsys.readouterr().err

    def test_tool_error_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"error": "Duplicate check failed"}),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_duplicate(_args())

        assert ex.value.code == 2
        assert "Duplicate check failed" in capsys.readouterr().err
