"""test_search_daemon_error_object.py — a daemon error object is an ERROR, never 0 hits.

Measured on production (techempower-org/mempalace#526, PR 4): a saturated daemon
answered ``/search/hybrid`` with HTTP 200 and a JSON-RPC error body::

    {"jsonrpc": "2.0", "id": 1,
     "error": {"code": -32003,
               "message": "daemon busy: 8 MCP tool call(s) in flight (PALACE_MCP_TOOL_MAX_INFLIGHT=8)"}}

The REST transports returned that body verbatim, ``_daemon_search_hybrid`` read
``.get("results") or []`` and the CLI printed **0 hits, exit 1** — "the palace was
reachable and had nothing to say". A busy daemon was indistinguishable from an
empty corpus, and the depth banner would then have said no curated document was
in the top N. That is #526's own error class: a statement about the store that
is really a statement about something else.

Now every transport classifies a 200 error object once (``_raise_if_daemon_error_object``),
``-32003`` / "busy" raises ``DaemonBusyError`` and ``_fail_daemon`` renders it under
#536's contract with ``code: "daemon_busy"`` as a dict LITERAL (the contract test
walks literals only), exit 2. Where the failing call was an OPTIMISATION — the
deeper fetch, the auto-mode hybrid fallback — the real hits are still returned
and the daemon's own words travel in ``warnings``, which the header prints, so
"no curated document in the top N" is never read without "deeper fetch
unavailable" beside it.
"""

import argparse
import ast
import json
import pathlib
from unittest.mock import patch

import pytest

CLI = pathlib.Path(__file__).resolve().parents[1] / "mempalace" / "cli.py"

BUSY_MSG = "daemon busy: 8 MCP tool call(s) in flight (PALACE_MCP_TOOL_MAX_INFLIGHT=8)"
BUSY_BODY = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": BUSY_MSG}}
OTHER_BODY = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "internal failure"}}
_T = "a session transcript discussing the parameter handler at length"


class _Resp:
    status = 200

    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _hits(n):
    return {
        "results": [
            {
                "id": f"d{i}",
                "wing": "2g",
                "room": "problems",
                "snippet": f"{_T} {i}",
                "source_file": f"/p/s{i}.jsonl",
                "rank": 0.5 - i * 0.01,
            }
            for i in range(1, n + 1)
        ]
    }


def _args(mode, fmt="json", limit=3):
    return argparse.Namespace(
        query="cmhs inbound parameter handler binary",
        wing="2g",
        room=None,
        results=limit,
        limit=limit,
        palace=None,
        mode=mode,
        tags=None,
        format=fmt,
        json=False,
        quiet=False,
    )


def _run(capsys, mode, bodies, fmt="json"):
    """Drive cmd_search with the transport answering ``bodies`` in order."""
    from mempalace import cli

    env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
    responses = [_Resp(b) for b in bodies]
    code = None
    with (
        patch.dict("os.environ", env, clear=True),
        patch("mempalace.cli.urlopen_with_wake", side_effect=responses) as m,
    ):
        try:
            cli.cmd_search(_args(mode, fmt))
        except SystemExit as e:
            code = e.code
    out, err = capsys.readouterr()
    return code, out, err, m


class TestErrorObjectIsAnError:
    def test_hybrid_json_busy_is_exit_2_with_the_busy_code(self, capsys):
        code, out, _, _ = _run(capsys, "hybrid", [BUSY_BODY])
        assert code == 2, "a busy daemon is 'could not run' (2), never 'no results' (1)"
        payload = json.loads(out)
        assert payload["code"] == "daemon_busy"
        assert payload["source"] == "daemon"
        assert BUSY_MSG in payload["error"], "the daemon's own words, verbatim"
        assert "results" not in payload, "an error payload carries no result list at all"

    def test_hybrid_prose_busy_says_so_on_stderr_and_prints_no_results_header(self, capsys):
        code, out, err, _ = _run(capsys, "hybrid", [BUSY_BODY], fmt="table")
        assert code == 2
        assert BUSY_MSG in err
        assert "Results for" not in out
        assert "curated" not in out, "the depth banner never fires on an error path"

    def test_fast_json_busy_is_exit_2_with_the_busy_code(self, capsys):
        code, out, _, _ = _run(capsys, "fast", [BUSY_BODY])
        assert code == 2
        assert json.loads(out)["code"] == "daemon_busy"

    def test_a_non_busy_error_object_is_daemon_error(self, capsys):
        code, out, _, _ = _run(capsys, "hybrid", [OTHER_BODY])
        assert code == 2
        payload = json.loads(out)
        assert payload["code"] == "daemon_error"
        assert "internal failure" in payload["error"]


class TestOptimisationsDegradeWithTheDaemonsWords:
    def test_busy_deeper_fetch_keeps_the_shallow_hits_and_warns(self, capsys):
        """Producer: the deeper call answers busy. The shallow hits are real and
        are returned (exit by results); curated_first_rank is null because the
        depth was NOT checked, and the warning says so in the daemon's words."""
        code, out, _, m = _run(capsys, "fast", [_hits(3), BUSY_BODY])
        assert code == 0
        data = json.loads(out)
        assert len(data["results"]) == 3
        assert data["curated_first_rank"] is None
        assert any("deeper fetch unavailable" in w and BUSY_MSG in w for w in data["warnings"])
        assert m.call_count == 2

    def test_the_header_prints_that_warning(self, capsys):
        """Consumer: the same warning reaches the reader of the default view,
        beside the depth statement it qualifies."""
        code, out, _, _ = _run(capsys, "fast", [_hits(3), BUSY_BODY], fmt="table")
        assert code is None or code == 0
        assert "! deeper fetch unavailable" in out
        assert BUSY_MSG in out

    def test_busy_hybrid_fallback_in_auto_mode_keeps_bm25_hits_and_warns(self, capsys):
        # fast under-shoots the limit (1 of 3) so auto tries hybrid, which is busy.
        # The deeper fetch is skipped: 1 curated-free hit fires it first, so the
        # second body is the deeper GET and the third is the hybrid POST.
        code, out, _, m = _run(capsys, "auto", [_hits(1), BUSY_BODY, BUSY_BODY])
        assert code == 0
        data = json.loads(out)
        assert len(data["results"]) == 1
        assert data["source"] == "bm25-fast"
        assert any("hybrid fallback unavailable" in w and BUSY_MSG in w for w in data["warnings"])
        assert m.call_count == 3


class TestBusyIsDocumentedAndEmittedAsALiteral:
    def test_daemon_busy_is_in_the_header_contract_block(self):
        header = CLI.read_text().split("\n", 200)[:200]
        block = "\n".join(header)
        assert "daemon_busy" in block, "the documented set in cli.py's header must name it"

    def test_daemon_busy_is_emitted_as_a_dict_literal(self):
        """The converse of the contract test, for THIS key only: it is emitted
        by a dict literal the AST walker can see, so documenting it is not dead
        vocabulary. Computed emitters are invisible to that walker by design."""
        emitted = set()
        for node in ast.walk(ast.parse(CLI.read_text())):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "code":
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            emitted.add(v.value)
        assert "daemon_busy" in emitted

    def test_the_contract_tests_documented_set_names_it(self):
        from tests.test_cli_daemon_error_contract import BRANCHABLE_CODES

        assert "daemon_busy" in BRANCHABLE_CODES


class TestClassifier:
    def test_result_bodies_pass_through_untouched(self):
        from mempalace import cli

        for body in ({"results": []}, {"result": {"content": []}}, [], None, {"results": [1]}):
            assert cli._raise_if_daemon_error_object(body, "/x") is body

    def test_an_error_body_that_also_has_results_is_a_result(self):
        """The payload decides: a warnings-style ``error`` beside real results is
        not an outage, and the caller's own ``"error" in data`` check still runs."""
        from mempalace import cli

        body = {"results": [1], "error": "partial"}
        assert cli._raise_if_daemon_error_object(body, "/x") is body

    def test_busy_by_code_and_by_message(self):
        from mempalace import cli

        with pytest.raises(cli.DaemonBusyError):
            cli._raise_if_daemon_error_object(BUSY_BODY, "/search/hybrid")
        with pytest.raises(cli.DaemonBusyError):
            cli._raise_if_daemon_error_object(
                {"error": {"code": -32000, "message": "Daemon BUSY, retry shortly"}}, "/x"
            )
        with pytest.raises(cli.DaemonError) as ei:
            cli._raise_if_daemon_error_object(OTHER_BODY, "/x")
        assert not isinstance(ei.value, cli.DaemonBusyError)
        assert str(ei.value).startswith("daemon error"), (
            "_fail_daemon's reachable/unreachable predicate keys on this prefix"
        )

    def test_a_string_error_is_also_an_error(self):
        from mempalace import cli

        with pytest.raises(cli.DaemonError):
            cli._raise_if_daemon_error_object({"error": "boom"}, "/x")


def test_mcp_tool_call_busy_is_the_same_exception(monkeypatch):
    from mempalace import cli

    monkeypatch.setenv("PALACE_DAEMON_URL", "http://daemon.example:8085")
    with patch("mempalace.cli.urlopen_with_wake", return_value=_Resp(BUSY_BODY)):
        with pytest.raises(cli.DaemonBusyError):
            cli._call_daemon_tool("mempalace_search", {"query": "q"})
