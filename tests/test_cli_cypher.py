"""
test_cli_cypher.py — coverage for ``mempalace cypher`` (issue #191).

The subcommand wraps the daemon's ``POST /cypher`` endpoint, which
executes arbitrary Cypher against the AGE knowledge graph inside a
``READ ONLY`` postgres transaction. Write verbs fail server-side with
SQLSTATE 25006 → HTTP 403 — the CLI surfaces that as a friendly
"this endpoint is read-only" hint with exit 2, distinct from the
generic daemon-down exit 1.

These tests mock ``_post_cypher`` (the lower-level HTTP helper) instead
of spinning up a real daemon — we exercise CLI surface: flag parsing,
output formatting (table / json / csv), JSON shape stability, and the
full failure-mode matrix (unreachable / 401 / 403 read-only / 404 /
inner-error).

Recall-preserving by construction: ``mempalace cypher`` is read-only
and never drops a row — limit is advisory; the server is the authority.
"""

import argparse
import csv
import io
import json
from unittest.mock import patch

import pytest


def _make_args(**overrides):
    """Build a Namespace with the defaults the parser would produce."""
    base = {
        "query": "MATCH (e:Entity) RETURN e.name AS name LIMIT 3",
        "graph": "mempalace_kg",
        "limit": None,
        "format": None,
        "json": False,
        "quiet": False,
        "palace": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _sample_rows(n: int = 2) -> list[dict]:
    """Build canonical Cypher result rows (name + score columns)."""
    return [{"name": f"entity_{i}", "score": round(0.9 - 0.1 * i, 2)} for i in range(n)]


def _sample_response(n: int = 2, **extras) -> dict:
    """Build a daemon-shaped /cypher response envelope."""
    payload = {"rows": _sample_rows(n), "count": n}
    payload.update(extras)
    return payload


# ── flag propagation to _post_cypher ──────────────────────────────────


class TestCypherFlagPropagation:
    def test_positional_query_reaches_daemon(self):
        from mempalace import cli

        captured = {}

        def fake_post(body):
            captured["body"] = body
            return _sample_response(), None

        with patch("mempalace.cli._post_cypher", side_effect=fake_post):
            cli.cmd_cypher(_make_args(query="MATCH (n) RETURN n LIMIT 5"))

        assert captured["body"]["cypher"] == "MATCH (n) RETURN n LIMIT 5"

    def test_default_graph_is_mempalace_kg(self):
        from mempalace import cli

        captured = {}

        def fake_post(body):
            captured["body"] = body
            return _sample_response(), None

        with patch("mempalace.cli._post_cypher", side_effect=fake_post):
            cli.cmd_cypher(_make_args(graph=None))

        # When --graph is omitted, the CLI fills in the canonical default.
        assert captured["body"]["graph"] == cli._CYPHER_DEFAULT_GRAPH
        assert captured["body"]["graph"] == "mempalace_kg"

    def test_graph_override_passes_through(self):
        from mempalace import cli

        captured = {}

        def fake_post(body):
            captured["body"] = body
            return _sample_response(), None

        with patch("mempalace.cli._post_cypher", side_effect=fake_post):
            cli.cmd_cypher(_make_args(graph="experiment_graph"))

        assert captured["body"]["graph"] == "experiment_graph"

    def test_empty_query_exits_2_without_calling_daemon(self, capsys):
        from mempalace import cli

        calls = []

        def fake_post(body):
            calls.append(body)
            return _sample_response(), None

        with patch("mempalace.cli._post_cypher", side_effect=fake_post):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args(query=""))

        assert ex.value.code == 2
        assert calls == []  # daemon never contacted
        err = capsys.readouterr().err
        assert "missing required positional QUERY" in err

    def test_whitespace_only_query_also_exits_2(self):
        from mempalace import cli

        calls = []

        def fake_post(body):
            calls.append(body)
            return _sample_response(), None

        with patch("mempalace.cli._post_cypher", side_effect=fake_post):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args(query="   \t\n  "))

        assert ex.value.code == 2
        assert calls == []


# ── format rendering ──────────────────────────────────────────────────


class TestCypherFormats:
    def test_table_format_is_default(self, capsys):
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(_sample_response(n=3), None)):
            cli.cmd_cypher(_make_args())

        out = capsys.readouterr().out
        # Header columns appear and rows render their values.
        assert "name" in out
        assert "score" in out
        assert "entity_0" in out
        assert "entity_1" in out
        assert "entity_2" in out
        # Row-count footer.
        assert "3 rows" in out

    def test_json_format_emits_stable_top_level_shape(self, capsys):
        from mempalace import cli

        resp = _sample_response(n=2, elapsed_ms=42)
        with patch("mempalace.cli._post_cypher", return_value=(resp, None)):
            cli.cmd_cypher(_make_args(format="json"))

        out = capsys.readouterr().out
        payload = json.loads(out)
        # Required top-level keys for downstream scripting.
        assert "rows" in payload
        assert "count" in payload
        assert "graph" in payload
        assert payload["count"] == 2
        assert payload["graph"] == "mempalace_kg"
        assert len(payload["rows"]) == 2
        # Extra daemon metadata is preserved (forward-compat).
        assert payload.get("elapsed_ms") == 42

    def test_top_level_json_flag_matches_format_json(self, capsys):
        """--json (legacy top-level interop flag) should produce the same
        machine-readable output as --format=json."""
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(_sample_response(n=1), None)):
            cli.cmd_cypher(_make_args(json=True))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "rows" in payload
        assert "count" in payload
        assert payload["count"] == 1

    def test_csv_format_pipes_cleanly(self, capsys):
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(_sample_response(n=2), None)):
            cli.cmd_cypher(_make_args(format="csv"))

        out = capsys.readouterr().out
        # csv.DictReader round-trip is the cleanest contract test.
        reader = csv.DictReader(io.StringIO(out))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["name"] == "entity_0"
        assert rows[1]["name"] == "entity_1"
        # CSV preserves the column header line.
        assert reader.fieldnames == ["name", "score"]

    def test_table_format_renders_nested_values_as_json(self, capsys):
        """Cypher can return list/dict values (e.g. node properties).
        Table cells must stringify them safely instead of crashing."""
        from mempalace import cli

        resp = {
            "rows": [{"name": "X", "tags": ["a", "b", "c"]}],
            "count": 1,
        }
        with patch("mempalace.cli._post_cypher", return_value=(resp, None)):
            cli.cmd_cypher(_make_args())

        out = capsys.readouterr().out
        # The list should appear as a JSON-encoded string in the cell.
        assert '["a", "b", "c"]' in out


# ── empty results ─────────────────────────────────────────────────────


class TestCypherEmpty:
    def test_empty_rows_human_message(self, capsys):
        from mempalace import cli

        empty = {"rows": [], "count": 0}
        with patch("mempalace.cli._post_cypher", return_value=(empty, None)):
            cli.cmd_cypher(_make_args())

        out = capsys.readouterr().out
        assert "No rows" in out

    def test_empty_rows_json_still_has_shape(self, capsys):
        from mempalace import cli

        empty = {"rows": [], "count": 0}
        with patch("mempalace.cli._post_cypher", return_value=(empty, None)):
            cli.cmd_cypher(_make_args(format="json"))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["rows"] == []
        assert payload["count"] == 0
        assert payload["graph"] == "mempalace_kg"


# ── daemon-down fallback ──────────────────────────────────────────────


class TestCypherDaemonDown:
    def test_daemon_error_exits_2_with_stderr(self, capsys):
        """Network failure → DaemonError → human-readable stderr + exit 1."""
        from mempalace import cli

        with patch(
            "mempalace.cli._post_cypher",
            side_effect=cli.DaemonError("connection refused"),
        ):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace daemon unreachable" in err
        assert "connection refused" in err

    def test_daemon_error_json_emits_structured_error(self, capsys):
        """With --format=json, the failure surfaces as a JSON document on
        stdout — machine callers need a parseable shape."""
        from mempalace import cli

        with patch(
            "mempalace.cli._post_cypher",
            side_effect=cli.DaemonError("timeout"),
        ):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args(format="json"))

        assert ex.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "error" in payload
        assert "timeout" in payload["error"]

    def test_403_read_only_exits_2_with_hint(self, capsys):
        """SQLSTATE 25006 → HTTP 403 — write verbs in a read-only txn.
        CLI must surface the read-only hint, not just a bare error."""
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(None, 403)):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args(query="CREATE (n:Entity {name:'x'})"))

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "read-only" in err
        assert "MATCH" in err and "RETURN" in err  # the rewrite hint

    def test_403_read_only_json_includes_status(self, capsys):
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(None, 403)):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args(format="json", query="DELETE (n)"))

        assert ex.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == 403
        assert "read-only" in payload["error"]

    def test_404_exits_2_endpoint_missing(self, capsys):
        """Older daemon without /cypher → 404 → same shape as unreachable."""
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(None, 404)):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "404" in err
        assert "unreachable" in err

    def test_401_auth_failure_exits_2(self):
        """Missing/bad x-api-key → 401 → exit 1."""
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(None, 401)):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args())

        assert ex.value.code == 2

    def test_503_non_postgres_exits_2_with_status(self, capsys):
        """Daemon on non-postgres backend → 503; same failure shape."""
        from mempalace import cli

        with patch("mempalace.cli._post_cypher", return_value=(None, 503)):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args(format="json"))

        assert ex.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == 503

    def test_inner_error_envelope_exits_2(self, capsys):
        """Daemon returned 200 with an ``error`` body and no rows — palace
        unreachable from inside the daemon, etc. Match cmd_graph's exit 2."""
        from mempalace import cli

        err_payload = {"error": "kg_disabled", "source": "daemon"}
        with patch("mempalace.cli._post_cypher", return_value=(err_payload, None)):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_cypher(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "kg_disabled" in err

    def test_inner_error_with_rows_is_not_an_error(self):
        """Defensive: if the daemon ever ships both ``rows`` and a
        warning-shaped ``error`` together, treat it as success — rows
        being present means the query ran."""
        from mempalace import cli

        # Should NOT raise SystemExit — rows present.
        mixed = {"rows": _sample_rows(1), "error": "deprecation_notice"}
        with patch("mempalace.cli._post_cypher", return_value=(mixed, None)):
            cli.cmd_cypher(_make_args(format="json"))
        # No assertion needed; the test passes if no SystemExit raised.
