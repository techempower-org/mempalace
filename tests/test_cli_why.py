"""
test_cli_why.py — ``mempalace why <drawer_id>`` (slice of #191).

The ``why`` subcommand composes three read-only daemon calls into one
report explaining why a drawer surfaces:
  1. ``mempalace_get_drawer``   → location + tags + content snippet
  2. ``/cypher`` (MENTIONS)     → top entities
  3. ``mempalace_search``       → semantic neighbors

Tests mock ``urllib.request.urlopen`` and dispatch each request to the
right canned response based on the URL path. The drawer itself is
filtered out of neighbors (self-similarity == 1.0).

Mirrors ``test_cli_overlap.py`` / ``test_cli_tags.py`` patterns.
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


def _mcp_envelope(payload: dict) -> bytes:
    """Wrap a tool result the way the daemon's /mcp endpoint does."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
    ).encode()


def _make_responder(
    *,
    drawer_payload: dict | None = None,
    entities_rows: dict | None = None,
    search_payload: dict | None = None,
    captured: list | None = None,
):
    """Dispatch urlopen calls by URL path → canned response.

    Captures (method, path, body) tuples so tests can assert what the
    CLI actually sent.
    """

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        body = None
        if getattr(req, "data", None) is not None:
            try:
                body = json.loads(req.data.decode())
            except Exception:
                body = req.data
        if captured is not None:
            captured.append((method, url, body))

        if url.endswith("/cypher"):
            return _FakeResp(json.dumps(entities_rows or {"rows": []}).encode())
        if url.endswith("/mcp"):
            tool = (body or {}).get("params", {}).get("name", "")
            if tool == "mempalace_get_drawer":
                return _FakeResp(_mcp_envelope(drawer_payload or {}))
            if tool == "mempalace_search":
                return _FakeResp(_mcp_envelope(search_payload or {"results": []}))
            return _FakeResp(_mcp_envelope({}))
        raise AssertionError(f"unexpected url {url}")

    return fake_urlopen


_DRAWER = {
    "drawer_id": "drawer_x_123",
    "content": "First paragraph about pgvector.\n\nSecond paragraph that should be ignored.",
    "wing": "memorypalace",
    "room": "decisions",
    "tags": ["postgres", "pgvector"],
    "metadata": {},
}

_ENTITIES = {
    "rows": [
        {"entity": "pgvector", "count": 4, "etype": "tool"},
        {"entity": "AGE", "count": 2, "etype": "tool"},
    ]
}

_NEIGHBORS = {
    "results": [
        # self — must be filtered out
        {
            "id": "drawer_x_123",
            "wing": "memorypalace",
            "room": "decisions",
            "distance": 0.0,
            "matched_via": "vector",
            "content": "self",
        },
        {
            "id": "drawer_y_456",
            "wing": "memorypalace",
            "room": "references",
            "distance": 0.12,
            "matched_via": "vector",
            "content": "neighbor body",
        },
        {
            "id": "drawer_z_789",
            "wing": "projects",
            "room": "sessions",
            "distance": 0.18,
            "matched_via": "keyword",
            "content": "another neighbor",
        },
    ]
}


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "drawer_id": "drawer_x_123",
        "neighbors": 5,
        "entities": 10,
        "graph": None,
        "format": None,
        "palace": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestWhyTableOutput:
    """Default table mode renders the three-block report."""

    def test_renders_location_entities_and_neighbors(self, capsys):
        from mempalace import cli

        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows=_ENTITIES,
            search_payload=_NEIGHBORS,
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args())

        out = capsys.readouterr().out
        assert "WHY — drawer drawer_x_123" in out
        assert "memorypalace / decisions" in out
        assert "postgres" in out and "pgvector" in out
        # Entities block — pgvector is top
        assert "pgvector" in out
        assert "×4" in out
        # Neighbors block — self is filtered out, others appear
        assert "drawer_y_456" in out
        assert "drawer_z_789" in out
        assert "drawer_x_123" in out.split("NEIGHBORS")[0]  # only in header
        assert "drawer_x_123" not in out.split("NEIGHBORS")[1]

    def test_no_entities_block_shows_friendly_empty(self, capsys):
        from mempalace import cli

        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows={"rows": []},
            search_payload=_NEIGHBORS,
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args())

        out = capsys.readouterr().out
        assert "no MENTIONS edges" in out
        # Neighbors block still rendered
        assert "drawer_y_456" in out

    def test_no_neighbors_shows_friendly_empty(self, capsys):
        from mempalace import cli

        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows=_ENTITIES,
            search_payload={"results": []},
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args())

        out = capsys.readouterr().out
        assert "no neighbors returned" in out


class TestWhyJsonOutput:
    """JSON mode emits a structured payload with all three sections."""

    def test_json_envelope_shape(self, capsys):
        from mempalace import cli

        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows=_ENTITIES,
            search_payload=_NEIGHBORS,
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["drawer_id"] == "drawer_x_123"
        assert payload["wing"] == "memorypalace"
        assert payload["room"] == "decisions"
        assert payload["tags"] == ["postgres", "pgvector"]
        assert payload["snippet"].startswith("First paragraph")
        assert len(payload["entities"]) == 2
        assert payload["entities"][0]["entity"] == "pgvector"
        # Self filtered out — only 2 neighbors land
        assert len(payload["neighbors"]) == 2
        assert payload["neighbors"][0]["id"] == "drawer_y_456"

    def test_json_shorthand_via_legacy_flag(self, capsys):
        from mempalace import cli

        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows=_ENTITIES,
            search_payload=_NEIGHBORS,
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["drawer_id"] == "drawer_x_123"


class TestWhyComposition:
    """Verify the three daemon calls are wired with the right arguments."""

    def test_get_drawer_and_search_called_with_self_snippet(self):
        from mempalace import cli

        captured: list = []
        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows=_ENTITIES,
            search_payload=_NEIGHBORS,
            captured=captured,
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args(neighbors=3))

        # Find the mempalace_search request
        searches = [
            body
            for _, url, body in captured
            if url.endswith("/mcp")
            and (body or {}).get("params", {}).get("name") == "mempalace_search"
        ]
        assert searches, "expected exactly one mempalace_search call"
        search_args = searches[0]["params"]["arguments"]
        # Query is the first non-blank paragraph
        assert "First paragraph" in search_args["query"]
        # Limit asks for one extra to account for the self-match.
        assert search_args["limit"] == 4

    def test_entities_query_uses_drawer_id_literal_and_limit(self):
        from mempalace import cli

        captured: list = []
        responder = _make_responder(
            drawer_payload=_DRAWER,
            entities_rows=_ENTITIES,
            search_payload=_NEIGHBORS,
            captured=captured,
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_why(_args(entities=7))

        cyphers = [body for _, url, body in captured if url.endswith("/cypher")]
        assert cyphers, "expected exactly one /cypher call"
        cypher = cyphers[0]["cypher"]
        assert "'drawer_x_123'" in cypher
        assert "[m:MENTIONS]" in cypher
        assert "LIMIT 7" in cypher
        assert "ORDER BY" in cypher

    def test_entities_query_500_degrades_to_empty_not_failure(self, capsys):
        """A 500 on /cypher (AGE not configured, query rejected) leaves
        the entities section empty but still emits the full report."""
        from mempalace import cli

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            if url.endswith("/cypher"):
                raise urllib.error.HTTPError(url, 500, "AGE catalog error", hdrs=None, fp=None)
            if url.endswith("/mcp"):
                body = json.loads(req.data.decode())
                tool = body["params"]["name"]
                if tool == "mempalace_get_drawer":
                    return _FakeResp(_mcp_envelope(_DRAWER))
                if tool == "mempalace_search":
                    return _FakeResp(_mcp_envelope(_NEIGHBORS))
            raise AssertionError(f"unexpected {url}")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_why(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["entities"] == []
        assert len(payload["neighbors"]) == 2


class TestWhyValidation:
    """Reject bad input before issuing any daemon hop."""

    def test_missing_drawer_id_exits_2(self, capsys):
        from mempalace import cli

        with pytest.raises(SystemExit) as ex:
            cli.cmd_why(_args(drawer_id=None))
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "drawer ID" in err

    def test_whitespace_drawer_id_exits_2(self, capsys):
        from mempalace import cli

        with pytest.raises(SystemExit) as ex:
            cli.cmd_why(_args(drawer_id="   "))
        assert ex.value.code == 2


class TestWhyFailureModes:
    """Failure shape matches the sibling fast-path commands."""

    def test_no_daemon_url_exits_2(self, capsys):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-why"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_why(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "PALACE_DAEMON_URL" in err

    def test_get_drawer_unreachable_exits_2(self, capsys):
        from mempalace import cli

        def boom(req, timeout=None):
            raise ConnectionError("daemon down")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=boom):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_why(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "daemon" in err.lower()

    def test_drawer_not_found_exits_2(self, capsys):
        from mempalace import cli

        responder = _make_responder(
            drawer_payload={"error": "Drawer not found: drawer_x_123"},
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_why(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "Drawer not found" in err

    def test_search_unreachable_exits_2(self, capsys):
        """If /mcp succeeds for get_drawer/cypher but blows up on
        mempalace_search, the report can't complete — exit 1."""
        from mempalace import cli

        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            if url.endswith("/cypher"):
                return _FakeResp(json.dumps({"rows": []}).encode())
            if url.endswith("/mcp"):
                body = json.loads(req.data.decode())
                tool = body["params"]["name"]
                if tool == "mempalace_get_drawer":
                    return _FakeResp(_mcp_envelope(_DRAWER))
                if tool == "mempalace_search":
                    call_count["n"] += 1
                    raise ConnectionError("search down")
            raise AssertionError(f"unexpected {url}")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_why(_args())
        assert ex.value.code == 2
        assert call_count["n"] >= 1


class TestParserAcceptsWhy:
    """Argparse wiring — flags propagate onto the Namespace."""

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_why") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_positional_drawer_id(self):
        ns = self._parse(["mempalace", "why", "drawer_x"])
        assert ns is not None
        assert ns.command == "why"
        assert ns.drawer_id == "drawer_x"

    def test_neighbors_and_entities_flags(self):
        ns = self._parse(["mempalace", "why", "drawer_x", "--neighbors", "8", "--entities", "20"])
        assert ns is not None
        assert ns.neighbors == 8
        assert ns.entities == 20

    def test_negative_neighbors_rejected(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "why", "drawer_x", "--neighbors", "-1"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_format_json(self):
        ns = self._parse(["mempalace", "why", "drawer_x", "--format", "json"])
        assert ns is not None
        assert ns.format == "json"

    def test_json_shorthand(self):
        ns = self._parse(["mempalace", "why", "drawer_x", "--json"])
        assert ns is not None
        assert ns.json is True

    def test_missing_drawer_id_argparse(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "why"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2
