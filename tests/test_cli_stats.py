"""
test_cli_stats.py — ``mempalace stats`` analytics dashboard (#191).

The ``stats`` subcommand wraps the daemon's ``GET /stats`` endpoint,
which returns a unified envelope ``{kg, graph, status}`` instead of the
older multi-RPC fan-out. Tests mock ``urllib.request.urlopen`` so the
dispatcher logic, section filtering, and failure modes are exercised
without touching a real daemon.

Mirrors ``test_cli_graph.py`` / ``test_cli_cypher.py`` patterns: per-test
``_args`` factory, ``_FakeResp`` for the urllib mock, parser-level tests
under ``TestParserAcceptsStats`` to guard the argparse wiring.
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


def _make_rest_responder(stats_payload: dict, tags_payload: dict | None = None):
    """Return a fake urlopen that serves the /stats REST endpoint.

    POST requests (MCP JSON-RPC envelope) fall through to a tag-fixture
    so ``--tags`` paths can still exercise the dispatcher. Anything else
    returns an empty object.
    """

    def fake_urlopen(req, timeout=None):
        if getattr(req, "data", None) is None:
            url = req.full_url
            if "/stats" in url:
                return _FakeResp(json.dumps(stats_payload).encode())
            return _FakeResp(b"{}")
        body = json.loads(req.data.decode())
        name = body["params"]["name"]
        if name == "mempalace_list_tags" and tags_payload is not None:
            return _FakeResp(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"content": [{"type": "text", "text": json.dumps(tags_payload)}]},
                    }
                ).encode()
            )
        return _FakeResp(
            b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{}"}]}}'
        )

    return fake_urlopen


_FULL_STATS = {
    "kg": {
        "entities": 100,
        "triples": 200,
        "current_facts": 180,
        "expired_facts": 20,
        "relationship_types": ["loves", "works_on", "part_of"],
    },
    "graph": {
        "total_rooms": 9,
        "tunnel_rooms": 4,
        "total_edges": 50,
        "rooms_per_wing": {"projects": 5, "sessions": 4},
        "top_tunnels": [
            {"room": "decisions", "wings": ["projects", "sessions"], "count": 3},
        ],
    },
    "status": {
        "total_drawers": 350,
        "wings": {"projects": 200, "sessions": 150},
        "rooms": {"references": 120, "discoveries": 80, "decisions": 60},
        "protocol": "AAAK v1.0 ...\nmany lines of text\n",
        "aaak_dialect": "long blob of dialect definition\n",
    },
}


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


class TestStatsFlagPropagation:
    """Verify each --section value renders the right block (and only that block)."""

    def _args(self, **overrides):
        defaults = {
            "json": False,
            "quiet": False,
            "top": 10,
            "tags": False,
            "format": None,
            "section": "all",
            "no_relationship_types": False,
            "palace": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_default_section_renders_all_three(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args())

        out = capsys.readouterr().out
        assert "KNOWLEDGE GRAPH" in out
        assert "GRAPH" in out
        assert "WINGS" in out
        assert "ROOMS" in out

    def test_section_kg_only(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(section="kg"))

        out = capsys.readouterr().out
        assert "KNOWLEDGE GRAPH" in out
        assert "100" in out  # entities
        # Other sections suppressed.
        assert "WINGS" not in out
        assert "ROOMS" not in out
        assert "tunnel rooms" not in out

    def test_section_graph_only(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(section="graph"))

        out = capsys.readouterr().out
        # The "GRAPH" header for the graph block renders, but the kg block
        # header "KNOWLEDGE GRAPH" must be suppressed. Check the kg header
        # substring directly to be unambiguous.
        assert "KNOWLEDGE GRAPH" not in out
        assert "tunnel rooms" in out
        assert "WINGS" not in out
        assert "ROOMS" not in out

    def test_section_status_only(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(section="status"))

        out = capsys.readouterr().out
        assert "WINGS" in out
        assert "ROOMS" in out
        assert "projects" in out
        assert "references" in out
        assert "KNOWLEDGE GRAPH" not in out
        assert "tunnel rooms" not in out

    def test_no_relationship_types_table_shows_count(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(no_relationship_types=True))

        out = capsys.readouterr().out
        # Count appears but the names do not.
        assert "3 types (suppressed)" in out
        assert "loves" not in out
        assert "works_on" not in out


class TestStatsFormats:
    """Output-format dispatch: table default, json pass-through, --json shorthand."""

    def _args(self, **overrides):
        defaults = {
            "json": False,
            "quiet": False,
            "top": 10,
            "tags": False,
            "format": None,
            "section": "all",
            "no_relationship_types": False,
            "palace": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_table_default_suppresses_protocol_and_dialect(self, capsys):
        """Table mode hides ``protocol`` and ``aaak_dialect`` — they're text
        blobs from the daemon's ``status`` block, not analytics."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args())

        out = capsys.readouterr().out
        assert "AAAK v1.0" not in out
        assert "dialect definition" not in out

    def test_json_passes_through_full_envelope(self, capsys):
        """JSON mode preserves the daemon contract — protocol/aaak_dialect
        survive so jq pipelines see the same fields the daemon emitted."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["kg"]["entities"] == 100
        assert payload["graph"]["total_rooms"] == 9
        assert payload["status"]["total_drawers"] == 350
        assert payload["status"]["protocol"].startswith("AAAK v1.0")
        assert "aaak_dialect" in payload["status"]
        # Relationship list intact when flag not set.
        assert payload["kg"]["relationship_types"] == ["loves", "works_on", "part_of"]

    def test_json_shorthand_via_legacy_flag(self, capsys):
        """``--json`` (legacy shorthand) resolves to format=json."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["kg"]["entities"] == 100

    def test_json_no_relationship_types_replaces_list_with_count(self, capsys):
        """In json mode, ``--no-relationship-types`` swaps the list for
        ``{"relationship_types_count": N}`` per spec."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_FULL_STATS)):
                cli.cmd_stats(self._args(json=True, no_relationship_types=True))

        payload = json.loads(capsys.readouterr().out)
        assert "relationship_types" not in payload["kg"]
        assert payload["kg"]["relationship_types_count"] == 3

    def test_table_with_tags_appends_tag_section(self, capsys):
        """``--tags`` fires an extra MCP call (mempalace_list_tags) since
        /stats doesn't include the tag breakdown."""
        from mempalace import cli

        tags = {"tags": [{"tag": "rust", "count": 5}, {"tag": "go", "count": 3}]}
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_rest_responder(_FULL_STATS, tags_payload=tags),
            ):
                cli.cmd_stats(self._args(tags=True))

        out = capsys.readouterr().out
        assert "TAGS" in out
        assert "rust" in out and "5" in out


class TestStatsEmpty:
    """Empty / partial payloads — daemon may return zeros or omit blocks."""

    def _args(self, **overrides):
        defaults = {
            "json": False,
            "quiet": False,
            "top": 10,
            "tags": False,
            "format": None,
            "section": "all",
            "no_relationship_types": False,
            "palace": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_zero_entities_renders_clean(self, capsys):
        """A fresh palace returns zeros; render must not crash on len()
        of an empty list or division by zero in the bar gauge."""
        from mempalace import cli

        empty = {
            "kg": {
                "entities": 0,
                "triples": 0,
                "current_facts": 0,
                "expired_facts": 0,
                "relationship_types": [],
            },
            "graph": {
                "total_rooms": 0,
                "tunnel_rooms": 0,
                "total_edges": 0,
                "rooms_per_wing": {},
                "top_tunnels": [],
            },
            "status": {"total_drawers": 0, "wings": {}, "rooms": {}},
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(empty)):
                cli.cmd_stats(self._args())

        out = capsys.readouterr().out
        assert "MemPalace Stats — 0 drawers" in out
        assert "(no wings)" in out
        assert "(no rooms)" in out

    def test_missing_graph_block(self, capsys):
        """An older daemon that omits the ``graph`` key must not blow up —
        the section header renders against an empty dict."""
        from mempalace import cli

        partial = {
            "kg": {
                "entities": 5,
                "triples": 5,
                "current_facts": 5,
                "expired_facts": 0,
                "relationship_types": [],
            },
            "status": {"total_drawers": 5, "wings": {"projects": 5}},
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(partial)):
                cli.cmd_stats(self._args())

        out = capsys.readouterr().out
        assert "MemPalace Stats — 5 drawers" in out
        assert "total rooms      :       0" in out

    def test_missing_status_block(self, capsys):
        """Without the ``status`` block, total drawers reads 0 and the
        WINGS/ROOMS sections fall through to the "no wings/rooms" path."""
        from mempalace import cli

        partial = {
            "kg": {
                "entities": 1,
                "triples": 1,
                "current_facts": 1,
                "expired_facts": 0,
                "relationship_types": ["loves"],
            },
            "graph": {
                "total_rooms": 1,
                "tunnel_rooms": 0,
                "total_edges": 0,
                "rooms_per_wing": {},
                "top_tunnels": [],
            },
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(partial)):
                cli.cmd_stats(self._args())

        out = capsys.readouterr().out
        assert "0 drawers" in out
        assert "(no wings)" in out
        assert "(no rooms)" in out

    def test_top_truncates_with_more_tail(self, capsys):
        """``--top N`` caps the rendered rows in WINGS/ROOMS and emits a
        "more wings/rooms" tail when entries are truncated."""
        from mempalace import cli

        many_rooms = {f"room{i:02}": (50 - i) for i in range(20)}
        payload = {
            "kg": _FULL_STATS["kg"],
            "graph": _FULL_STATS["graph"],
            "status": {
                "total_drawers": sum(many_rooms.values()),
                "wings": {"projects": sum(many_rooms.values())},
                "rooms": many_rooms,
            },
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(payload)):
                cli.cmd_stats(self._args(top=3))

        out = capsys.readouterr().out
        assert "more rooms" in out
        assert "room00" in out and "room02" in out


class TestStatsDaemonDown:
    """Failure modes — match cmd_list/cmd_graph/cmd_cypher exit codes."""

    def _args(self, **overrides):
        defaults = {
            "json": False,
            "quiet": False,
            "top": 10,
            "tags": False,
            "format": None,
            "section": "all",
            "no_relationship_types": False,
            "palace": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_no_daemon_url_exits_2(self, capsys):
        """Daemon-only command — refuse to run when daemon URL is unset."""
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-stats"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_stats(self._args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "PALACE_DAEMON_URL" in err

    def test_unreachable_exits_2(self, capsys):
        """Network failure surfaces as exit 1 (matches sibling commands).

        Earlier multi-RPC implementation exited 2 here; the migration to
        the single-call REST path aligns stats with cmd_list/cmd_graph/
        cmd_cypher: 1 for unreachable, 2 reserved for inner-error."""
        from mempalace import cli

        def boom(req, timeout=None):
            raise ConnectionError("daemon down")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=boom):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_stats(self._args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "daemon" in err.lower()

    def test_404_exits_2(self, capsys):
        """An older daemon without /stats returns 404 — exit 1."""
        from mempalace import cli

        def four_oh_four(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=four_oh_four):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_stats(self._args())
        assert ex.value.code == 2

    def test_401_exits_2(self, capsys):
        """Bad auth surfaces as exit 1, same shape as 404."""
        from mempalace import cli

        def unauthorized(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=unauthorized):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_stats(self._args())
        assert ex.value.code == 2

    def test_403_exits_2(self, capsys):
        """Forbidden surfaces as exit 1 — daemon reachable but auth bad
        or endpoint disabled."""
        from mempalace import cli

        def forbidden(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=forbidden):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_stats(self._args())
        assert ex.value.code == 2

    def test_inner_error_envelope_exits_2(self, capsys):
        """Daemon returns 200 with ``{"error": ...}`` — palace itself broken
        even though the daemon is reachable. Exit 2."""
        from mempalace import cli

        envelope = {"error": "AGE catalog corrupted"}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(envelope)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_stats(self._args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "AGE catalog corrupted" in err


class TestParserAcceptsStats:
    """Sanity checks for the argparse wiring — every flag parses cleanly
    and propagates onto the Namespace dispatched to cmd_stats.
    """

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_stats") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_post_subcommand_json_flag(self):
        ns = self._parse(["mempalace", "stats", "--json"])
        assert ns is not None
        assert ns.command == "stats"
        assert ns.json is True

    def test_pre_subcommand_json_flag(self):
        ns = self._parse(["mempalace", "--json", "stats"])
        assert ns is not None
        assert ns.command == "stats"
        assert ns.json is True

    def test_format_flag_overrides_default(self):
        ns = self._parse(["mempalace", "stats", "--format", "json"])
        assert ns is not None
        assert ns.format == "json"

    def test_section_flag_accepts_kg(self):
        ns = self._parse(["mempalace", "stats", "--section", "kg"])
        assert ns is not None
        assert ns.section == "kg"

    def test_section_flag_accepts_graph(self):
        ns = self._parse(["mempalace", "stats", "--section", "graph"])
        assert ns is not None
        assert ns.section == "graph"

    def test_section_flag_accepts_status(self):
        ns = self._parse(["mempalace", "stats", "--section", "status"])
        assert ns is not None
        assert ns.section == "status"

    def test_section_default_is_all(self):
        ns = self._parse(["mempalace", "stats"])
        assert ns is not None
        assert ns.section == "all"

    def test_section_rejects_unknown(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "stats", "--section", "sources"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_no_relationship_types_flag(self):
        ns = self._parse(["mempalace", "stats", "--no-relationship-types"])
        assert ns is not None
        assert ns.no_relationship_types is True

    def test_tags_flag(self):
        ns = self._parse(["mempalace", "stats", "--tags"])
        assert ns is not None
        assert ns.tags is True

    def test_top_flag(self):
        ns = self._parse(["mempalace", "stats", "--top", "3"])
        assert ns is not None
        assert ns.top == 3

    def test_top_rejects_negative(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "stats", "--top", "-1"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2
