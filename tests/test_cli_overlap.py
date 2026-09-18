"""
test_cli_overlap.py — ``mempalace overlap`` cross-wing entity finder (slice of #191).

The ``overlap`` subcommand wraps the daemon's ``POST /cypher`` with an
inline-substituted Cypher query that intersects two wings on the same
:Entity node. Tests mock ``urllib.request.urlopen`` so dispatch, output
formats, parameter sanitization, and failure modes are exercised
without touching a real daemon.

Mirrors ``test_cli_cypher.py`` / ``test_cli_stats.py`` patterns.
"""

import argparse
import json
import urllib.error
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


def _make_cypher_responder(rows_payload: dict, captured: list | None = None):
    """Return a fake urlopen that serves /cypher.

    Captures the parsed POST body so tests can assert the Cypher string
    the CLI assembled — required for sanitization and intersection-shape
    coverage.
    """

    def fake_urlopen(req, timeout=None):
        if getattr(req, "data", None) is not None and captured is not None:
            captured.append(json.loads(req.data.decode()))
        return _FakeResp(json.dumps(rows_payload).encode())

    return fake_urlopen


_FULL_ROWS = {
    "rows": [
        {"entity": "Alice", "a_drawers": 4, "b_drawers": 3},
        {"entity": "Bob", "a_drawers": 2, "b_drawers": 1},
    ]
}


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "wing_a": "projects",
        "wing_b": "sessions",
        "limit": 50,
        "graph": None,
        "format": None,
        "palace": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestOverlapTableOutput:
    """Default table mode renders the intersect header and per-entity counts."""

    def test_renders_header_and_rows(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_cypher_responder(_FULL_ROWS)):
                cli.cmd_overlap(_args())

        out = capsys.readouterr().out
        assert "OVERLAP — projects" in out
        assert "sessions" in out
        for ent in ("Alice", "Bob"):
            assert ent in out
        # A and B columns render; column totals appear.
        assert "4" in out and "3" in out

    def test_empty_rows_renders_clean_message(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_cypher_responder({"rows": []})):
                cli.cmd_overlap(_args(wing_a="left", wing_b="right"))

        out = capsys.readouterr().out
        assert "No entity overlap between 'left' and 'right'" in out


class TestOverlapJsonOutput:
    """JSON mode passes through the daemon envelope + appends wing context."""

    def test_json_envelope_shape(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_cypher_responder(_FULL_ROWS)):
                cli.cmd_overlap(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 2
        assert payload["wing_a"] == "projects"
        assert payload["wing_b"] == "sessions"
        assert payload["graph"] == "mempalace_kg"
        assert payload["rows"][0]["entity"] == "Alice"

    def test_json_shorthand_via_legacy_flag(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_cypher_responder(_FULL_ROWS)):
                cli.cmd_overlap(_args(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["rows"][1]["entity"] == "Bob"


class TestOverlapCypherShape:
    """The assembled Cypher must intersect on the SAME :Entity node."""

    def test_cypher_uses_two_matches_bound_to_same_entity(self):
        from mempalace import cli

        captured: list[dict] = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_cypher_responder(_FULL_ROWS, captured=captured),
            ):
                cli.cmd_overlap(_args(wing_a="alpha", wing_b="beta"))

        cypher = captured[0]["cypher"]
        # Both wing names appear as inline literals.
        assert "'alpha'" in cypher
        assert "'beta'" in cypher
        # Two MATCH clauses with separate drawer aliases bound to the
        # same Entity node ``e`` — that's what gives the intersection.
        assert cypher.count("MATCH ") == 2
        assert "(e:Entity)" in cypher
        assert "(e)" in cypher
        # ORDER BY drives the highest-overlap rows to the top.
        assert "ORDER BY" in cypher

    def test_cypher_inlines_limit(self):
        from mempalace import cli

        captured: list[dict] = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_cypher_responder(_FULL_ROWS, captured=captured),
            ):
                cli.cmd_overlap(_args(limit=7))

        cypher = captured[0]["cypher"]
        assert "LIMIT 7" in cypher

    def test_graph_flag_propagates_to_body(self):
        from mempalace import cli

        captured: list[dict] = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_cypher_responder(_FULL_ROWS, captured=captured),
            ):
                cli.cmd_overlap(_args(graph="custom_kg"))

        assert captured[0]["graph"] == "custom_kg"


class TestOverlapValidation:
    """Argument validation — refuse missing/same wings before issuing a query."""

    def test_missing_wing_a_exits_2(self, capsys):
        from mempalace import cli

        with pytest.raises(SystemExit) as ex:
            cli.cmd_overlap(_args(wing_a=None))
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "two positional wing names" in err

    def test_same_wing_twice_exits_2(self, capsys):
        from mempalace import cli

        with pytest.raises(SystemExit) as ex:
            cli.cmd_overlap(_args(wing_a="same", wing_b="same"))
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "DIFFERENT" in err

    def test_empty_wing_name_sanitization_exits_2(self, capsys):
        """An empty/whitespace wing name fails sanitize_kg_value upstream
        — surface as exit 2, never reach the daemon."""
        from mempalace import cli

        # First validate the two-wings check passes (different non-empty
        # strings), then sanitize_kg_value rejects the blank.
        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_overlap(_args(wing_a="   ", wing_b="other"))
        assert ex.value.code == 2


class TestOverlapFailureModes:
    """Failure shape matches the sibling fast-path commands."""

    def test_no_daemon_url_exits_2(self, capsys):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-overlap"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_overlap(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "PALACE_DAEMON_URL" in err

    def test_unreachable_exits_2(self, capsys):
        from mempalace import cli

        def boom(req, timeout=None):
            raise ConnectionError("daemon down")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=boom):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_overlap(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "daemon" in err.lower()

    def test_404_exits_2(self, capsys):
        """Older daemon without /cypher → exit 1."""
        from mempalace import cli

        def four_oh_four(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=four_oh_four):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_overlap(_args())
        assert ex.value.code == 2

    def test_503_non_postgres_exits_2(self, capsys):
        """Daemon on chroma backend returns 503 for /cypher — exit 1."""
        from mempalace import cli

        def unavail(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 503, "Service Unavailable", hdrs=None, fp=None
            )

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=unavail):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_overlap(_args())
        assert ex.value.code == 2

    def test_inner_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        bad = {"error": "AGE catalog corrupted"}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_cypher_responder(bad)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_overlap(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "AGE catalog corrupted" in err


class TestParserAcceptsOverlap:
    """Argparse wiring — flags propagate onto the Namespace."""

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_overlap") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_positional_wings(self):
        ns = self._parse(["mempalace", "overlap", "A", "B"])
        assert ns is not None
        assert ns.command == "overlap"
        assert ns.wing_a == "A"
        assert ns.wing_b == "B"

    def test_limit_and_graph(self):
        ns = self._parse(["mempalace", "overlap", "A", "B", "--limit", "10", "--graph", "g2"])
        assert ns is not None
        assert ns.limit == 10
        assert ns.graph == "g2"

    def test_limit_rejects_negative(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "overlap", "A", "B", "--limit", "-1"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_format_json(self):
        ns = self._parse(["mempalace", "overlap", "A", "B", "--format", "json"])
        assert ns is not None
        assert ns.format == "json"

    def test_json_shorthand(self):
        ns = self._parse(["mempalace", "overlap", "A", "B", "--json"])
        assert ns is not None
        assert ns.json is True

    def test_missing_second_wing(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "overlap", "only-one"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2
