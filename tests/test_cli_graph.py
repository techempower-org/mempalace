"""
test_cli_graph.py — coverage for ``mempalace graph`` (issue #191).

The subcommand is a thin wrapper around the daemon's ``GET /graph?limit=``
REST route, which returns a pre-aggregated palace structural snapshot
(wings, rooms, passive tunnels) plus a KG slice (top-N entities,
sample RELATION/MENTIONS triples, kg_stats). We mock ``_call_daemon_rest``
to avoid spinning up a real daemon — these tests exercise the CLI
surface: flag parsing, limit clamping, output formatting, JSON shape,
and the daemon-down fallback (returns None or raises DaemonError →
stderr message + exit 1).

Recall-preserving by design: ``mempalace graph`` is a structural
snapshot of the palace shape and KG, not a drawer query. Limit only
caps the KG entity sample (and 2x for MENTIONS triples); wings, rooms,
and tunnels always ship in full.
"""

import argparse
import json
from unittest.mock import patch

import pytest


def _make_args(**overrides):
    """Build a Namespace with the defaults the parser would produce."""
    base = {
        "limit": 500,
        "format": None,
        "json": False,
        "quiet": False,
        "palace": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _sample_response(n_entities: int = 2, n_triples: int = 2, n_mentions: int = 2) -> dict:
    """Build a daemon-shaped /graph response."""
    return {
        "wings": {"projects": 100, "memorypalace": 50, "storyvox": 200},
        "rooms": [
            {"wing": "projects", "rooms": {"references": 60, "planning": 40}},
            {"wing": "memorypalace", "rooms": {"sessions": 50}},
            {"wing": "storyvox", "rooms": {"references": 200}},
        ],
        "tunnels": [
            {"room": "references", "wings": ["projects", "storyvox"]},
        ],
        "kg_entities": [
            {
                "id": f"ent{i}",
                "name": f"entity_{i}",
                "type": "entity",
                "properties": {},
            }
            for i in range(n_entities)
        ],
        "kg_triples": [
            {
                "subject": f"subj{i}",
                "predicate": "relates_to",
                "object": f"obj{i}",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.7,
                "source_file": None,
            }
            for i in range(n_triples)
        ],
        "kg_mentions": [
            {
                "subject": f"drawer_{i}",
                "predicate": "MENTIONS",
                "object": f"thing{i}",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.5,
                "source_file": "TECH_IDENT",
            }
            for i in range(n_mentions)
        ],
        "kg_stats": {
            "entities": 768000,
            "triples": 1070000,
            "mentions": 5770000,
            "relationship_types": ["RELATION", "MENTIONS"],
        },
    }


# ── flag propagation to _call_daemon_rest ─────────────────────────────


class TestGraphFlagPropagation:
    def test_default_args_send_limit_500(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["path"] = path
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_graph(_make_args())

        assert captured["path"] == "/graph"
        assert captured["params"]["limit"] == 500

    def test_explicit_limit_propagates(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_graph(_make_args(limit=1000))

        assert captured["params"]["limit"] == 1000

    def test_limit_clamped_to_sanity_max(self):
        """--limit beyond the sanity cap is clamped CLI-side so the
        daemon never sees a request larger than its hard ceiling."""
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_graph(_make_args(limit=999999))

        # cli._GRAPH_LIMIT_MAX matches the daemon's documented ceiling.
        assert captured["params"]["limit"] == cli._GRAPH_LIMIT_MAX

    def test_negative_limit_clamped_to_one(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_graph(_make_args(limit=-100))

        assert captured["params"]["limit"] == 1

    def test_zero_limit_clamped_to_one(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_graph(_make_args(limit=0))

        assert captured["params"]["limit"] == 1


# ── format rendering ──────────────────────────────────────────────────


class TestGraphFormats:
    def test_table_format_is_default(self, capsys):
        from mempalace import cli

        with patch(
            "mempalace.cli._call_daemon_rest",
            return_value=_sample_response(n_entities=3, n_triples=3),
        ):
            cli.cmd_graph(_make_args())

        out = capsys.readouterr().out
        # Palace structure summary block
        assert "Palace structure" in out
        assert "wings:" in out
        assert "rooms:" in out
        assert "tunnels:" in out
        # Top wings render (storyvox has highest count in sample)
        assert "Top wings by drawer count" in out
        assert "storyvox" in out
        # KG block
        assert "Knowledge graph" in out
        assert "entities:" in out
        assert "768000" in out
        assert "1070000" in out
        # Relationship types listed
        assert "RELATION" in out
        # Sample entity + triple sections show up
        assert "Sample entities" in out
        assert "Sample triples" in out
        assert "entity_0" in out
        assert "subj0" in out
        assert "—[relates_to]→" in out

    def test_full_format_has_every_wing_and_triple(self, capsys):
        from mempalace import cli

        resp = _sample_response(n_entities=3, n_triples=4, n_mentions=2)
        with patch("mempalace.cli._call_daemon_rest", return_value=resp):
            cli.cmd_graph(_make_args(format="full"))

        out = capsys.readouterr().out
        # Every wing labelled explicitly (no truncation)
        assert "WINGS" in out
        assert "projects" in out
        assert "memorypalace" in out
        assert "storyvox" in out
        # Rooms per wing
        assert "ROOMS (per wing)" in out
        # Tunnels section
        assert "TUNNELS" in out
        # KG stats labelled
        assert "KG STATS" in out
        # All sampled triples render (n=4 in fixture)
        assert "KG TRIPLES" in out
        for i in range(4):
            assert f"subj{i}" in out
        # Mentions section
        assert "KG MENTIONS" in out
        for i in range(2):
            assert f"drawer_{i}" in out

    def test_json_format_emits_stable_top_level_shape(self, capsys):
        from mempalace import cli

        with patch(
            "mempalace.cli._call_daemon_rest",
            return_value=_sample_response(n_entities=2, n_triples=2),
        ):
            cli.cmd_graph(_make_args(format="json"))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert set(payload.keys()) == {
            "wings",
            "rooms",
            "tunnels",
            "kg_entities",
            "kg_triples",
            "kg_mentions",
            "kg_stats",
        }
        assert payload["wings"]["storyvox"] == 200
        assert len(payload["kg_entities"]) == 2
        assert len(payload["kg_triples"]) == 2
        assert payload["kg_stats"]["entities"] == 768000

    def test_top_level_json_flag_matches_format_json(self, capsys):
        """``--json`` (legacy top-level interop flag) produces the same
        machine-readable output as ``--format=json``."""
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=_sample_response()):
            cli.cmd_graph(_make_args(json=True))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "wings" in payload
        assert "kg_stats" in payload


# ── empty results ─────────────────────────────────────────────────────


class TestGraphEmpty:
    def test_empty_graph_human_renders_zeros(self, capsys):
        """A daemon returning an empty palace + empty KG should still
        produce a coherent summary (zeros, not exceptions)."""
        from mempalace import cli

        empty = {
            "wings": {},
            "rooms": [],
            "tunnels": [],
            "kg_entities": [],
            "kg_triples": [],
            "kg_mentions": [],
            "kg_stats": {"entities": 0, "triples": 0, "mentions": 0, "relationship_types": []},
        }
        with patch("mempalace.cli._call_daemon_rest", return_value=empty):
            cli.cmd_graph(_make_args())

        out = capsys.readouterr().out
        # Header still renders; no "Top wings" / "Sample entities"
        # blocks because the iterators are empty.
        assert "Palace structure" in out
        assert "Knowledge graph" in out
        assert "Top wings by drawer count" not in out
        assert "Sample entities" not in out

    def test_empty_graph_json_still_has_shape(self, capsys):
        from mempalace import cli

        empty = {
            "wings": {},
            "rooms": [],
            "tunnels": [],
            "kg_entities": [],
            "kg_triples": [],
            "kg_mentions": [],
            "kg_stats": {},
        }
        with patch("mempalace.cli._call_daemon_rest", return_value=empty):
            cli.cmd_graph(_make_args(format="json"))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["wings"] == {}
        assert payload["kg_stats"] == {}


# ── daemon-down fallback ──────────────────────────────────────────────


class TestGraphDaemonDown:
    def test_none_response_exits_2_with_stderr(self, capsys):
        """``_call_daemon_rest`` returns None on 404/401/403. ``mempalace
        graph`` should treat this as daemon-down per the team-lead's spec,
        not silently render an empty snapshot."""
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=None):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_graph(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace daemon unreachable" in err
        assert "mempalace status" in err

    def test_daemon_error_exits_2_with_stderr(self, capsys):
        """Network failures (incl. /graph timeouts under backfill load)
        raise DaemonError; same stderr/exit-code contract."""
        from mempalace import cli

        with patch(
            "mempalace.cli._call_daemon_rest",
            side_effect=cli.DaemonError("timed out"),
        ):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_graph(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace daemon unreachable" in err
        assert "timed out" in err

    def test_daemon_error_json_emits_structured_error(self, capsys):
        """With --format=json the failure surfaces as a JSON document on
        stdout — machine callers need a parseable shape, not a stderr
        line."""
        from mempalace import cli

        with patch(
            "mempalace.cli._call_daemon_rest",
            side_effect=cli.DaemonError("connection refused"),
        ):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_graph(_make_args(format="json"))

        assert ex.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "error" in payload
        assert "connection refused" in payload["error"]
        assert payload["source"] == "daemon"

    def test_none_response_json_emits_structured_error(self, capsys):
        """The None branch also speaks JSON when --format=json."""
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=None):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_graph(_make_args(format="json"))

        assert ex.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "error" in payload
        assert "/graph unavailable" in payload["error"]

    def test_inner_error_payload_exits_2(self, capsys):
        """When the daemon returns a JSON error envelope with no payload
        keys (palace unreachable from inside the daemon), surface it as
        exit 2 — same contract as cmd_list/cmd_status."""
        from mempalace import cli

        err_payload = {"error": "palace_unavailable"}
        with patch("mempalace.cli._call_daemon_rest", return_value=err_payload):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_graph(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace_unavailable" in err
