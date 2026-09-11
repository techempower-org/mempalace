"""
test_cli_daemon.py — CLI subcommand daemon routing.

Mirrors the gate in ``mempalace.hooks_cli`` and ``mempalace.mcp_server``:
when ``PALACE_DAEMON_URL`` is set and ``PALACE_DAEMON_STRICT != "0"``,
``cmd_status``, ``cmd_search``, and ``cmd_mine`` route to the daemon
(via ``/mcp`` JSON-RPC for read paths, ``/mine`` for write) instead of
opening a local chromadb client.

The local-path tests (``tests/test_cli.py``) keep working because
``tests/conftest.py`` scrubs ``PALACE_DAEMON_URL`` for the test
session — these tests opt back in via ``patch.dict``.
"""

import argparse
import json
import urllib.error
from unittest.mock import MagicMock, patch

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


def _rest_fastpath_404(req):
    """Raise HTTPError(404) for REST fast-path GETs so cmd_search/cmd_status
    fall through to the MCP POST envelope these tests actually verify."""
    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)


# ── _daemon_strict ──────────────────────────────────────────────────────


class TestDaemonStrictGate:
    def test_returns_true_when_url_set(self):
        from mempalace.cli import _daemon_strict

        with patch.dict("os.environ", {"PALACE_DAEMON_URL": "http://x:8085"}, clear=True):
            assert _daemon_strict() is True

    def test_returns_false_when_url_unset(self, tmp_path):
        from mempalace.cli import _daemon_strict
        from mempalace.config import MempalaceConfig

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "mempalace.cli.MempalaceConfig", lambda: MempalaceConfig(config_dir=str(tmp_path))
            ),
        ):
            assert _daemon_strict() is False

    def test_strict_zero_disables(self):
        from mempalace.cli import _daemon_strict

        env = {"PALACE_DAEMON_URL": "http://x:8085", "PALACE_DAEMON_STRICT": "0"}
        with patch.dict("os.environ", env, clear=True):
            assert _daemon_strict() is False


# ── _call_daemon_tool ──────────────────────────────────────────────────


class TestCallDaemonTool:
    def test_posts_jsonrpc_tools_call_with_api_key(self):
        from mempalace.cli import _call_daemon_tool

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["api_key"] = req.get_header("X-api-key")
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"total_drawers\\": 7}"}]}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_API_KEY": "k"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = _call_daemon_tool("mempalace_status", {})

        assert captured["url"] == "http://daemon.example:8085/mcp"
        assert captured["body"]["params"]["name"] == "mempalace_status"
        assert captured["api_key"] == "k"
        assert result == {"total_drawers": 7}

    def test_raises_on_jsonrpc_error(self):
        from mempalace.cli import _call_daemon_tool, DaemonError

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Unknown tool"}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(DaemonError):
                    _call_daemon_tool("bogus_tool", {})

    def test_raises_on_network_failure(self):
        from mempalace.cli import _call_daemon_tool, DaemonError

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
                with pytest.raises(DaemonError):
                    _call_daemon_tool("mempalace_status", {})


# ── _post_daemon_mine_cli ──────────────────────────────────────────────


class TestPostDaemonMineCli:
    def test_posts_to_mine_endpoint(self):
        from mempalace.cli import _post_daemon_mine_cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0, "stdout": "mined ok"}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_API_KEY": "k"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w", mode="convos")

        assert ok is True
        assert captured["url"] == "http://daemon.example:8085/mine"
        assert captured["body"] == {
            "dir": "/some/dir",
            "wing": "w",
            "mode": "convos",
            "background": False,
        }

    def test_returns_false_on_failure(self, capsys):
        from mempalace.cli import _post_daemon_mine_cli

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
                ok = _post_daemon_mine_cli("/some/dir", wing="w")

        assert ok is False
        # CLI users get errors on stderr — silent swallow is for hooks only.
        err = capsys.readouterr().err
        assert "boom" in err or "daemon" in err.lower()

    def test_surfaces_http_error_detail(self, capsys):
        """A 4xx from the daemon must print the response body's detail, not
        a bare 'HTTP Error 400'. The body says WHY — e.g. the daemon host
        cannot see a client-only path — and the user needs that to act."""
        import io
        import urllib.error

        from mempalace.cli import _post_daemon_mine_cli

        body = b'{"detail": "Directory does not exist: /home/u/.claude/projects/x/scratch/notes"}'

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, io.BytesIO(body))

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/home/u/.claude/projects/x/scratch/notes", wing="w")

        assert ok is False
        err = capsys.readouterr().err
        assert "Directory does not exist" in err
        # Path-not-visible gets an actionable hint about sync/staging.
        assert "sync" in err.lower()


class TestPostDaemonMineCliBackground:
    """The CLI poster must be able to ask for a queued mine (#456).

    ``hooks_cli._post_daemon_mine`` has sent ``background: True`` since #433;
    the CLI copy never did, so every caller of it — ``replay`` above all —
    waited for the palace write lock. Measured 2026-09-10: ``mempalace
    replay`` ran 120 s under checkpoint load and drained ONE request (34 →
    33) while the daemon's mine queue sat at 142-207 with the drainer holding
    the flock.
    """

    @staticmethod
    def _capture(response: bytes = b'{"returncode": 0}', status: int = 200):
        captured = {}

        class _Resp:
            def __init__(self):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return response

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _Resp()

        return captured, fake_urlopen

    def test_background_key_is_always_on_the_wire(self):
        """Present even when false, so its absence cannot creep back in.

        The defect was an omitted key, not a wrong value: a body without
        ``background`` and a body with ``background: false`` behave
        identically at the daemon (the field defaults to ``False``), which is
        exactly why the omission survived from #433 to #456 unnoticed.
        """
        from mempalace.cli import _post_daemon_mine_cli

        captured, fake_urlopen = self._capture()
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w", mode="convos")

        assert ok is True
        assert "background" in captured["body"]
        assert captured["body"]["background"] is False

    def test_background_true_is_sent(self):
        from mempalace.cli import _post_daemon_mine_cli

        captured, fake_urlopen = self._capture()
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w", background=True)

        assert ok is True
        assert captured["body"]["background"] is True

    def test_202_queued_is_success_and_prints_the_daemon_note(self, capsys):
        """The daemon answers 202 with a systemMessage; show it, not raw JSON."""
        from mempalace.cli import _post_daemon_mine_cli

        body = json.dumps(
            {
                "queued": True,
                "reason": "background",
                "systemMessage": "Mine queued — running in the background on the palace host.",
            }
        ).encode()
        _captured, fake_urlopen = self._capture(response=body, status=202)
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w", background=True)

        assert ok is True
        out = capsys.readouterr().out
        assert "queued" in out.lower()
        assert "running in the background on the palace host" in out

    def test_synchronous_response_still_reports_accepted(self, capsys):
        from mempalace.cli import _post_daemon_mine_cli

        _captured, fake_urlopen = self._capture(response=b'{"returncode": 0, "stdout": "mined"}')
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w")

        assert ok is True
        assert "accepted" in capsys.readouterr().out.lower()

    def test_non_json_body_does_not_crash(self, capsys):
        """An older daemon (or a proxy) may answer plain text."""
        from mempalace.cli import _post_daemon_mine_cli

        _captured, fake_urlopen = self._capture(response=b"OK, mining")
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w")

        assert ok is True
        assert "OK, mining" in capsys.readouterr().out


class TestCmdMineBackgroundFlag:
    """``--background`` must reach the palace daemon's /mine too (#456)."""

    @staticmethod
    def _args(**overrides):
        defaults = {
            "dir": "/home/u/proj",
            "mode": "convos",
            "wing": "myproj",
            "agent": None,
            "limit": None,
            "dry_run": False,
            "no_gitignore": False,
            "include_ignored": None,
            "redetect_origin": False,
            "extract": None,
            "palace": None,
            "daemon": False,
            "background": False,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_background_is_forwarded_to_the_daemon(self):
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"queued": true, "systemMessage": "Mine queued"}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(self._args(background=True))

        assert ex.value.code == 0
        assert captured["body"]["background"] is True

    def test_default_mine_still_asks_for_a_synchronous_run(self):
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(self._args())

        assert ex.value.code == 0
        assert captured["body"]["background"] is False

    def test_background_without_daemon_still_rejected_off_the_daemon_route(self, capsys):
        """With no daemon-strict routing, --background needs --daemon."""
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with patch("mempalace.cli._daemon_strict", return_value=False):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(self._args(background=True))

        assert ex.value.code == 2
        assert "--background requires --daemon" in capsys.readouterr().err

    def test_background_with_explicit_palace_is_rejected(self, capsys):
        """--palace opts out of daemon routing, so --background has no route."""
        from mempalace import cli

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_mine(self._args(background=True, palace="/tmp/p"))

        assert ex.value.code == 2
        assert "--background requires --daemon" in capsys.readouterr().err


# ── cmd_status routing ─────────────────────────────────────────────────


class TestCmdStatusDaemon:
    def test_routes_to_daemon_when_strict(self, capsys):
        """cmd_status must NOT call miner.status when daemon-strict; it
        prints a daemon-sourced summary instead."""
        from mempalace import cli

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":'
                b'"{\\"total_drawers\\": 42, \\"wings\\": {\\"projects\\": 30, \\"sessions\\": 12}}"}]}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None)

        mock_miner = MagicMock()
        with patch.dict("os.environ", env, clear=True):
            with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    cli.cmd_status(args)

        out = capsys.readouterr().out
        assert "42" in out
        assert "projects" in out
        assert "30" in out
        # Local fallback must not run.
        mock_miner.status.assert_not_called()

    def test_local_path_when_daemon_unset(self):
        """Without the env var, cmd_status delegates to miner.status as before."""
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
                mock_cfg.return_value.palace_path = "/local/palace"
                mock_cfg.return_value.daemon_strict = (
                    False  # #49: prevent MagicMock-truthy daemon route
                )
                args = argparse.Namespace(palace=None)
                mock_miner = MagicMock()
                with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
                    cli.cmd_status(args)
                    mock_miner.status.assert_called_once_with(palace_path="/local/palace")


# ── cmd_search routing ─────────────────────────────────────────────────


class TestCmdSearchDaemon:
    def test_routes_to_daemon_when_strict(self, capsys):
        from mempalace import cli

        # mempalace_search returns a search-shaped dict; the inner JSON
        # text is exactly what tool_search produces.
        inner = {
            "results": [
                {
                    "wing": "projects",
                    "room": "memorypalace",
                    "source_file": "/path/to/file.md",
                    "similarity": 0.91,
                    "text": "matching content here",
                }
            ],
            "warnings": [],
        }
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            }
        ).encode()

        def fake_urlopen(req, timeout=None):
            if getattr(req, "data", None) is None:
                return _rest_fastpath_404(req)
            captured_body = json.loads(req.data.decode())
            assert captured_body["params"]["name"] == "mempalace_search"
            assert captured_body["params"]["arguments"]["query"] == "graphql"
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(query="graphql", wing=None, room=None, results=5, palace=None)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_search(args)

        out = capsys.readouterr().out
        assert "graphql" in out
        assert "matching content here" in out

    def test_sends_limit_not_max_results(self):
        """Regression for #129: CLI must send ``limit`` (MCP tool_search
        parameter name), not ``max_results`` — the daemon rejects the
        latter with ``-32602: Unknown parameter 'max_results'``.
        """
        from mempalace import cli

        inner = {"results": [], "warnings": []}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            }
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            if getattr(req, "data", None) is None:
                return _rest_fastpath_404(req)
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(query="q", wing=None, room=None, results=7, palace=None)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_search(args)

        arguments = captured["body"]["params"]["arguments"]
        assert arguments["limit"] == 7
        assert "max_results" not in arguments

    def test_local_path_when_daemon_unset(self):
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
                mock_cfg.return_value.palace_path = "/local/palace"
                mock_cfg.return_value.daemon_strict = (
                    False  # #49: prevent MagicMock-truthy daemon route
                )
                args = argparse.Namespace(query="x", wing=None, room=None, results=5, palace=None)
                with patch("mempalace.searcher.search") as mock_search:
                    cli.cmd_search(args)
                    mock_search.assert_called_once()


# ── cmd_search auto-with-fallback (techempower-org/mempalace#283) ──────


class TestCmdSearchAutoFallback:
    """``--mode=auto`` runs BM25-fast first, then falls back to hybrid
    (vector + AGE graph) when BM25 under-shoots the requested limit —
    so semantic-only / paraphrased queries don't silently return zero
    hits. The fallback is signalled in the ``source`` field so the
    ``[source]`` banner stays honest.
    """

    @staticmethod
    def _args(query="q", results=5):
        return argparse.Namespace(
            query=query, wing=None, room=None, results=results, palace=None, mode="auto", tags=None
        )

    def test_falls_back_to_hybrid_on_zero_bm25_hits(self):
        from mempalace import cli

        fast_result = {"results": [], "query": "q", "source": "bm25-fast"}
        hybrid_result = {
            "results": [{"wing": "w", "room": "r", "source_file": "f", "text": "match"}],
            "source": "hybrid",
        }

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._daemon_search_fast", return_value=fast_result) as m_fast,
            patch("mempalace.cli._daemon_search_hybrid", return_value=hybrid_result) as m_hyb,
        ):
            cli.cmd_search(self._args())

        m_fast.assert_called_once()
        m_hyb.assert_called_once()
        assert hybrid_result["source"] == "hybrid (auto-fallback from bm25-fast)"

    def test_falls_back_when_bm25_returns_fewer_than_limit(self):
        from mempalace import cli

        fast_result = {
            "results": [{"text": "one"}],
            "query": "q",
            "source": "bm25-fast",
        }
        hybrid_result = {
            "results": [{"text": "one"}, {"text": "two"}, {"text": "three"}],
            "source": "hybrid",
        }

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._daemon_search_fast", return_value=fast_result),
            patch("mempalace.cli._daemon_search_hybrid", return_value=hybrid_result) as m_hyb,
        ):
            cli.cmd_search(self._args(results=5))

        m_hyb.assert_called_once()
        assert hybrid_result["source"] == "hybrid (auto-fallback from bm25-fast)"

    def test_no_fallback_when_bm25_meets_limit(self):
        from mempalace import cli

        fast_result = {
            "results": [{"text": "a"}, {"text": "b"}],
            "query": "q",
            "source": "bm25-fast",
        }

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._daemon_search_fast", return_value=fast_result) as m_fast,
            patch("mempalace.cli._daemon_search_hybrid") as m_hyb,
        ):
            cli.cmd_search(self._args(results=2))

        m_fast.assert_called_once()
        m_hyb.assert_not_called()
        assert fast_result["source"] == "bm25-fast"

    def test_both_empty_reports_fallback_and_carries_hybrid_warnings(self):
        """Both arms empty: ``source`` stays the stable "bm25-fast", the fact
        that hybrid was tried goes in ``fallback``, and hybrid's warnings are
        carried — a bare "0 results" under a bm25-fast banner read as "no
        fallback happened" and hid the daemon's filtered-kNN diagnostic
        (fleet incident 2026-09-03)."""
        from mempalace import cli

        fast_result = {"results": [], "query": "q", "source": "bm25-fast"}
        hybrid_result = {
            "results": [],
            "source": "hybrid",
            "warnings": [
                "13014 drawers in scope; vector ranked only 0 within the distance threshold"
            ],
        }

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._daemon_search_fast", return_value=fast_result),
            patch("mempalace.cli._daemon_search_hybrid", return_value=hybrid_result) as m_hyb,
        ):
            cli.cmd_search(self._args())

        m_hyb.assert_called_once()
        assert fast_result["source"] == "bm25-fast"
        assert "hybrid" in fast_result["fallback"] and "0" in fast_result["fallback"]
        assert fast_result["warnings"] == hybrid_result["warnings"]

    def test_keeps_bm25_when_hybrid_also_empty(self):
        """When the fallback retry also returns nothing, keep BM25's empty
        result rather than discarding it — the user gets the original
        "No results" message instead of a hybrid-source ghost banner.
        """
        from mempalace import cli

        fast_result = {"results": [], "query": "q", "source": "bm25-fast"}
        hybrid_result = {"results": [], "source": "hybrid"}

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._daemon_search_fast", return_value=fast_result),
            patch("mempalace.cli._daemon_search_hybrid", return_value=hybrid_result),
        ):
            cli.cmd_search(self._args())

        # Source label stays the BM25 banner — no rescue happened.
        assert fast_result["source"] == "bm25-fast"

    def test_no_fallback_when_room_filter_set(self):
        """The auto-fallback only fires on the unfiltered branch — the
        ``not args.room and not tags`` gate above the fast call still
        guards both BM25 and the fallback. With ``--room`` set, the call
        falls through to ``mempalace_search`` (hybrid via MCP) unchanged.
        """
        from mempalace import cli

        args = argparse.Namespace(
            query="q",
            wing=None,
            room="memorypalace",
            results=5,
            palace=None,
            mode="auto",
            tags=None,
        )

        inner = {"results": [{"text": "from mcp"}], "warnings": []}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            }
        ).encode()

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._daemon_search_fast") as m_fast,
            patch("mempalace.cli._daemon_search_hybrid") as m_hyb,
            patch("urllib.request.urlopen", return_value=_FakeResp(body)),
        ):
            cli.cmd_search(args)

        m_fast.assert_not_called()
        m_hyb.assert_not_called()


# ── cmd_mine routing ───────────────────────────────────────────────────


class TestCmdMineDaemon:
    def test_routes_projects_mode_to_daemon(self):
        """cmd_mine in projects mode routes to /mine with mode=projects.

        sys.exit(0) is called on success, so the test must wrap in
        pytest.raises(SystemExit). That double-checks the success
        contract: routing succeeded → exit 0.
        """
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(
            dir="/home/u/proj",
            mode="projects",
            wing=None,
            agent=None,
            limit=None,
            dry_run=False,
            no_gitignore=False,
            include_ignored=None,
            redetect_origin=False,
            extract=None,
            palace=None,
        )

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(args)

        assert ex.value.code == 0
        assert captured["body"]["mode"] == "projects"
        # Normalize separators so the assertion holds on Windows, where
        # Path.expanduser().resolve() returns a backslash-prefixed path
        # like "D:\\home\\u\\proj".
        assert captured["body"]["dir"].replace("\\", "/").endswith("/home/u/proj")

    def test_routes_convos_mode_to_daemon(self):
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(
            dir="/home/u/.claude/projects/-x",
            mode="convos",
            wing="myproj",
            agent=None,
            limit=None,
            dry_run=False,
            extract=None,
            palace=None,
            redetect_origin=False,
            no_gitignore=False,
            include_ignored=None,
        )

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(args)

        assert ex.value.code == 0
        assert captured["body"]["mode"] == "convos"
        assert captured["body"]["wing"] == "myproj"


# ── cmd_wakeup routing (#285) ──────────────────────────────────────────


class TestCmdWakeupDaemon:
    def test_routes_to_daemon_when_strict(self, capsys):
        """cmd_wakeup must call the daemon's mempalace_wakeup tool when
        daemon-strict; it must not import .layers.MemoryStack."""
        from mempalace import cli

        inner = {"text": "L0\nidentity here\n\nL1\nessential", "tokens": 42, "wing": None}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            }
        ).encode()

        def fake_urlopen(req, timeout=None):
            captured_body = json.loads(req.data.decode())
            assert captured_body["params"]["name"] == "mempalace_wakeup"
            assert captured_body["params"]["arguments"] == {}
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing=None)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_wakeup(args)

        out = capsys.readouterr().out
        assert "L0" in out
        assert "identity here" in out
        assert "Wake-up text (~42 tokens):" in out

    def test_forwards_wing_argument(self):
        from mempalace import cli

        inner = {"text": "wing-scoped wake", "tokens": 8, "wing": "memorypalace"}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing="memorypalace")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_wakeup(args)

        assert captured["body"]["params"]["arguments"] == {"wing": "memorypalace"}

    def test_local_path_when_palace_arg_given(self):
        """--palace argument keeps the local fallback even when daemon-strict
        is on — for forensic reads of archived palaces."""
        from mempalace import cli

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace="/local/palace", wing=None)

        mock_stack = MagicMock()
        mock_stack.wake_up.return_value = "local wake text"
        mock_layers = MagicMock()
        mock_layers.MemoryStack.return_value = mock_stack

        with patch.dict("os.environ", env, clear=True):
            with patch.dict("sys.modules", {"mempalace.layers": mock_layers}):
                cli.cmd_wakeup(args)

        # MemoryStack was constructed with the supplied palace path
        mock_layers.MemoryStack.assert_called_once_with(palace_path="/local/palace")
        mock_stack.wake_up.assert_called_once_with(wing=None)

    def test_daemon_error_exits_nonzero(self, capsys):
        from mempalace import cli

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("daemon unreachable")

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing=None)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_wakeup(args)

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "ERROR" in err


# ── cmd_mined routing (#285) ───────────────────────────────────────────


class TestCmdMinedDaemon:
    def test_routes_to_daemon_when_strict(self, capsys):
        from mempalace import cli

        inner = {
            "sources_by_wing": {
                "myproj": {
                    "sources": [
                        {"source_file": "/path/a.jsonl", "drawer_count": 12},
                        {"source_file": "/path/b.jsonl", "drawer_count": 5},
                    ],
                    "total_sources": 2,
                    "total_drawers": 17,
                    "truncated": False,
                }
            },
            "wing_filter": None,
            "total_wings": 1,
            "total_sources": 2,
        }
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing=None, limit=10, json=False)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_mined(args)

        assert captured["body"]["params"]["name"] == "mempalace_mined"
        out = capsys.readouterr().out
        assert "myproj" in out
        assert "/path/a.jsonl" in out
        assert "12" in out

    def test_forwards_wing_and_limit(self):
        from mempalace import cli

        inner = {"sources_by_wing": {}, "wing_filter": "x", "total_wings": 0, "total_sources": 0}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing="x", limit=5, json=False)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_mined(args)

        assert captured["body"]["params"]["arguments"] == {"wing": "x", "limit": 5}

    def test_json_passthrough(self, capsys):
        """--json emits the daemon's payload verbatim without the human
        block layout."""
        from mempalace import cli

        inner = {
            "sources_by_wing": {"w": {"sources": [], "total_sources": 0, "total_drawers": 0}},
            "wing_filter": None,
            "total_wings": 1,
            "total_sources": 0,
        }
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing=None, limit=10, json=True)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_mined(args)

        out = capsys.readouterr().out
        # JSON output starts with "{"
        parsed = json.loads(out)
        assert parsed["sources_by_wing"]["w"]["total_sources"] == 0
        # And does NOT include the human header
        assert "MemPalace Mined" not in out

    def test_daemon_error_exits_nonzero(self, capsys):
        from mempalace import cli

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("daemon unreachable")

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None, wing=None, limit=10, json=False)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mined(args)

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "ERROR" in err


# ── cmd_rooms routing (#285, daemon PR #96) ────────────────────────────


class TestCmdRoomsDaemon:
    def test_list_routes_to_daemon(self, capsys):
        from mempalace import cli

        inner = [
            {
                "name": "architecture",
                "description": "designs",
                "added_at": "2026-05-14 16:57:00.602177+00:00",
            },
            {
                "name": "decisions",
                "description": "trade-offs",
                "added_at": "2026-05-14 16:57:00.602177+00:00",
            },
        ]
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="list")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_rooms(args)

        assert captured["body"]["params"]["name"] == "mempalace_rooms_list"
        out = capsys.readouterr().out
        assert "architecture" in out
        assert "designs" in out
        assert "2026-05-14" in out

    def test_list_empty(self, capsys):
        from mempalace import cli

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "[]"}]}},
        ).encode()

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="list")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_rooms(args)

        assert "no canonical rooms registered" in capsys.readouterr().out

    def test_add_routes_to_daemon(self, capsys):
        from mempalace import cli

        inner = {"action": "added", "name": "experiments"}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="add", name="experiments", description="dragon-tests")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_rooms(args)

        assert captured["body"]["params"]["name"] == "mempalace_rooms_add"
        assert captured["body"]["params"]["arguments"] == {
            "name": "experiments",
            "description": "dragon-tests",
        }
        out = capsys.readouterr().out
        assert "added canonical room 'experiments'" in out

    def test_add_reports_update_action(self, capsys):
        """When the daemon returns action='updated', the CLI prints 'updated'."""
        from mempalace import cli

        inner = {"action": "updated", "name": "experiments"}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="add", name="experiments", description="new desc")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_rooms(args)

        out = capsys.readouterr().out
        assert "updated description for canonical room 'experiments'" in out

    def test_add_rejects_invalid_name_locally(self, capsys):
        """Client-side validation (alphanumeric/underscore only) short-circuits
        before any daemon call — hyphens fail the `replace('_','').isalnum()`
        check and exit with code 1 without contacting the daemon."""
        from mempalace import cli

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="add", name="bad-name", description=None)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen") as mock_urlopen:
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_rooms(args)
                mock_urlopen.assert_not_called()
        assert ex.value.code == 1
        assert "must be lowercase snake_case" in capsys.readouterr().out

    def test_rename_routes_to_daemon(self, capsys):
        from mempalace import cli

        inner = {"old": "experiments", "new": "labs", "affected_drawers": 42}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="rename", old="experiments", new="labs")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_rooms(args)

        assert captured["body"]["params"]["arguments"] == {"old": "experiments", "new": "labs"}
        out = capsys.readouterr().out
        assert "renamed canonical room 'experiments' → 'labs'" in out
        assert "42 drawers" in out

    def test_remove_routes_to_daemon(self, capsys):
        from mempalace import cli

        inner = {"name": "stale_room", "removed": True}
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            },
        ).encode()

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="remove", name="stale_room")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_rooms(args)

        assert captured["body"]["params"]["arguments"] == {"name": "stale_room"}
        out = capsys.readouterr().out
        assert "removed canonical room 'stale_room'" in out

    def test_remove_refused_when_drawers_still_reference(self, capsys):
        """Daemon -32602 with 'affected_drawers=N' surfaces as exit 1 with
        the daemon's error message."""
        from mempalace import cli

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": -32602,
                    "message": "cannot remove 'busy_room' — 17 drawers still reference it",
                },
            }
        ).encode()

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(rooms_cmd="remove", name="busy_room")

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_rooms(args)

        assert ex.value.code == 1
        err = capsys.readouterr().err
        assert "busy_room" in err
        assert "17 drawers" in err


# ── search provenance rendering (techempower-org/mempalace#451) ─────────


# Text fixtures for the near-duplicate ordering tests below. ``_NEAR_CLAIM``
# appears verbatim in both the curated card and the transcript quoting it —
# the real shape from the 2g corpus that #451 item F was measured on.
_NEAR_CLAIM = (
    "the five handsets refused one cell and the fault was never inside "
    "their modems because a documented refuser camped and carried a call"
)
CARD = "REFUTED 2026-09-06 " + _NEAR_CLAIM + " this correction supersedes the headline"
TRANSCRIPT = "and then I told the team " + _NEAR_CLAIM + " which is what the log shows"
UNRELATED = (
    "the postgres backend scrubs lone surrogates and nul bytes at every bind "
    "site so a mined corpus cannot abort the whole batch on one stray byte"
)


class TestCmdSearchProvenance:
    """Every search hit carries ``source_kind`` and, when the CLI can tell,
    ``source_stale`` — and the renderers say so.

    A transcript hit is a *quoted copy* of what someone said at the time. When
    the curated document has since corrected the claim (2g/CLAUDE.md carried a
    REFUTED banner for two days while search kept returning the transcript
    that first stated it), the reader needs to see which shape they are
    holding before they act on it.
    """

    @staticmethod
    def _args(fmt="json", results=2):
        return argparse.Namespace(
            query="five handsets refuse the network",
            wing="2g",
            room=None,
            results=results,
            limit=None,
            palace=None,
            mode="auto",
            tags=None,
            format=fmt,
            json=False,
            quiet=False,
        )

    @staticmethod
    def _fast_payload():
        """The shape GET /search/fast returns: basename-only source files."""
        return {
            "results": [
                {
                    "id": "drawer_2g_general_aaa",
                    "wing": "2g",
                    "room": "general",
                    "snippet": "FIVE HANDSETS REFUSE THIS NETWORK",
                    "source_file": "5f9a-1a2b.jsonl",
                    "created_at": "2026-09-03T23:14:00",
                    "rank": 0.4812,
                },
                {
                    "id": "drawer_2g_general_bbb",
                    "wing": "2g",
                    "room": "general",
                    "snippet": "the fault is inside their modems",
                    "source_file": "7c21-8e06.jsonl",
                    "created_at": "2026-09-04T01:02:00",
                    "rank": 0.4501,
                },
            ]
        }

    def _run(self, fmt, capsys):
        from mempalace import cli

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=self._fast_payload()),
        ):
            try:
                cli.cmd_search(self._args(fmt=fmt))
            except SystemExit:
                pass
        return capsys.readouterr()

    def test_json_output_carries_source_kind(self, capsys):
        payload = json.loads(self._run("json", capsys).out)
        assert [h["source_kind"] for h in payload["results"]] == ["transcript", "transcript"]

    def test_json_omits_source_stale_for_basename_only_paths(self, capsys):
        """A hit whose only path is a bare basename cannot be located on disk,
        so staleness is undecidable — and an undecidable answer must never be
        rendered as "not stale". (Most real hits DO carry an absolute
        ``source_path`` and do resolve; this fixture pins the other case.)"""
        payload = json.loads(self._run("json", capsys).out)
        assert all("source_stale" not in h for h in payload["results"])

    def test_table_output_prints_the_transcript_caveat(self, capsys):
        out = self._run("table", capsys).out
        assert "⚠ quoted copy from a session transcript" in out

    def test_table_header_warns_when_nothing_curated_matched(self, capsys):
        out = self._run("table", capsys).out
        assert "! all 2 hits are session-transcript copies" in out

    def test_compact_output_tags_transcript_hits(self, capsys):
        out = self._run("compact", capsys).out
        assert out.count("⟨transcript⟩") == 2

    def test_mcp_envelope_route_is_annotated_too(self, capsys):
        """``--mode`` values the REST fast path can't serve fall through to the
        MCP envelope; ``--format json`` must carry the fields there as well."""
        from mempalace import cli

        envelope = {
            "results": [
                {
                    "id": "diary_2g_20260906_1",
                    "wing": "2g",
                    "room": "general",
                    "text": "checkpoint",
                    "source_file": None,
                    "created_at": "2026-09-06T10:00:00",
                }
            ]
        }
        args = self._args(fmt="json")
        args.room = "general"  # forces _daemon_search_auto to decline
        args.mode = "auto"

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_tool", return_value=envelope),
        ):
            with pytest.raises(SystemExit):
                cli.cmd_search(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["source_kind"] == "diary"

    def test_header_line_absent_when_a_curated_hit_is_present(self, capsys):
        """One curated document in the set means the reader has somewhere
        authoritative to look — no blanket warning."""
        from mempalace import cli

        payload = self._fast_payload()
        payload["results"][1]["source_file"] = "user_jp_profile.md"
        payload["results"][1]["source_path"] = "/home/jp/.claude/x/memory/user_jp_profile.md"

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=payload),
        ):
            try:
                cli.cmd_search(self._args(fmt="table"))
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "session-transcript copies" not in out
        assert "⚠ quoted copy from a session transcript" in out

    def test_compact_tag_derives_kind_for_an_unannotated_hit(self):
        """``table`` derives a missing kind via ``provenance_note``; ``compact``
        must not be the one renderer that silently drops the caveat."""
        from mempalace import cli

        assert cli._provenance_tag({"source_file": "abc.jsonl"}) == " ⟨transcript⟩"
        assert cli._provenance_tag({"source_file": "/p/CLAUDE.md"}) == ""

    def test_compact_tag_omits_stale_for_transcripts(self):
        """Mirrors provenance_note: a growing session transcript is expected,
        so ⟨stale⟩ would fire on every open session and add nothing."""
        from mempalace import cli

        hit = {"source_kind": "transcript", "source_stale": True}
        assert cli._provenance_tag(hit) == " ⟨transcript⟩"

    def test_fast_route_orders_curated_above_its_near_duplicate(self, capsys):
        """#451 item F, FAIL-D: on bm25-fast the curated card measured rank 13
        while a transcript quoting it sat at rank 1."""
        from mempalace import cli

        payload = {
            "results": [
                {
                    "id": "t",
                    "wing": "2g",
                    "room": "problems",
                    "snippet": TRANSCRIPT,
                    "source_file": "/p/s.jsonl",
                    "rank": 0.051,
                },
                {
                    "id": "f",
                    "wing": "2g",
                    "room": "problems",
                    "snippet": CARD,
                    "source_file": "/p/CLAUDE.md",
                    "rank": 0.017,
                },
            ]
        }
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=payload),
        ):
            with pytest.raises(SystemExit):
                cli.cmd_search(self._args(fmt="json", results=2))
        out = json.loads(capsys.readouterr().out)
        assert [h["id"] for h in out["results"]] == ["f", "t"]

    def test_fast_route_leaves_transcript_only_recall_alone(self, capsys):
        """THE BOUND, at the route level: no curated near-duplicate, no reorder."""
        from mempalace import cli

        payload = {
            "results": [
                {
                    "id": "t",
                    "wing": "2g",
                    "room": "p",
                    "snippet": UNRELATED,
                    "source_file": "/p/s.jsonl",
                    "rank": 0.051,
                },
                {
                    "id": "f",
                    "wing": "2g",
                    "room": "p",
                    "snippet": CARD,
                    "source_file": "/p/CLAUDE.md",
                    "rank": 0.017,
                },
            ]
        }
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=payload),
        ):
            with pytest.raises(SystemExit):
                cli.cmd_search(self._args(fmt="json", results=2))
        out = json.loads(capsys.readouterr().out)
        assert [h["id"] for h in out["results"]] == ["t", "f"]

    def test_mcp_envelope_route_applies_the_preference(self, capsys):
        from mempalace import cli

        envelope = {
            "results": [
                {
                    "id": "t",
                    "wing": "2g",
                    "room": "p",
                    "text": TRANSCRIPT,
                    "source_file": "s.jsonl",
                },
                {"id": "f", "wing": "2g", "room": "p", "text": CARD, "source_file": "CLAUDE.md"},
            ]
        }
        args = self._args(fmt="json")
        args.room = "p"
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_tool", return_value=envelope),
        ):
            with pytest.raises(SystemExit):
                cli.cmd_search(args)
        out = json.loads(capsys.readouterr().out)
        assert [h["id"] for h in out["results"]] == ["f", "t"]

    # ── conditional widen (#451 item F, the fetched-window bound) ──────

    @staticmethod
    def _fast_payload_n(items):
        return {"results": items}

    @staticmethod
    def _t(i, text):
        return {
            "id": f"t{i}",
            "wing": "2g",
            "room": "p",
            "snippet": text,
            "source_file": f"/p/s{i}.jsonl",
            "rank": 0.05,
        }

    @staticmethod
    def _f(i, text):
        return {
            "id": f"f{i}",
            "wing": "2g",
            "room": "p",
            "snippet": text,
            "source_file": "/p/CLAUDE.md",
            "rank": 0.01,
        }

    def test_no_widen_when_a_curated_hit_is_already_present(self):
        """THE COMMON CASE COSTS NOTHING: exactly one daemon call."""
        from mempalace import cli

        payload = self._fast_payload_n([self._t(1, UNRELATED), self._f(1, CARD)])
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=payload) as m,
        ):
            cli._daemon_search_fast("q", 2, wing="2g")
        assert m.call_count == 1

    def test_widens_once_when_nothing_curated_matched(self):
        """The broken case pays exactly one extra call, at 2x the limit."""
        from mempalace import cli

        narrow = self._fast_payload_n([self._t(i, UNRELATED) for i in range(2)])
        wide = self._fast_payload_n([self._t(i, UNRELATED) for i in range(3)] + [self._f(1, CARD)])
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", side_effect=[narrow, wide]) as m,
        ):
            cli._daemon_search_fast("q", 2, wing="2g")
        assert m.call_count == 2
        assert m.call_args_list[0].args[1]["limit"] == 2
        assert m.call_args_list[1].args[1]["limit"] == 4

    def test_widen_surfaces_a_curated_near_duplicate_inside_the_limit(self):
        """The point of widening: the card sits outside the first window, is a
        near-duplicate of a hit inside it, and must end up above the cut."""
        from mempalace import cli

        narrow = self._fast_payload_n([self._t(0, TRANSCRIPT), self._t(1, UNRELATED)])
        wide = self._fast_payload_n(
            [self._t(0, TRANSCRIPT), self._t(1, UNRELATED), self._t(2, UNRELATED), self._f(1, CARD)]
        )
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", side_effect=[narrow, wide]),
        ):
            data = cli._daemon_search_fast("q", 2, wing="2g")
        ids = [h["id"] for h in data["results"]]
        assert ids[0] == "f1", f"curated hit must clear the cut, got {ids}"
        assert len(ids) == 2, "result is truncated back to the requested limit"

    def test_widen_that_finds_nothing_curated_still_reports_honestly(self, capsys):
        """A widened window with no curated hit must leave the header firing."""
        from mempalace import cli

        narrow = self._fast_payload_n([self._t(i, UNRELATED) for i in range(2)])
        wide = self._fast_payload_n([self._t(i, UNRELATED) for i in range(4)])
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", side_effect=[narrow, wide]),
        ):
            try:
                cli.cmd_search(self._args(fmt="table", results=2))
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "no curated document matched" in out, (
            "a widened window that still found nothing curated must keep saying so"
        )

    def test_widen_tolerates_a_failed_second_call(self):
        """The extra fetch is an optimisation; losing it must not lose the search."""
        from mempalace import cli

        narrow = self._fast_payload_n([self._t(i, UNRELATED) for i in range(2)])
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", side_effect=[narrow, cli.DaemonError("boom")]),
        ):
            data = cli._daemon_search_fast("q", 2, wing="2g")
        assert len(data["results"]) == 2

    def test_no_widen_when_the_limit_already_exceeds_the_cap(self):
        """At a large limit the widened window would not be wider — skip it."""
        from mempalace import cli

        payload = self._fast_payload_n([self._t(i, UNRELATED) for i in range(3)])
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=payload) as m,
        ):
            cli._daemon_search_fast("q", 40, wing="2g")
        assert m.call_count == 1

    def test_compact_tag_marks_diary_hits(self):
        """The table renderer gets a diary note; compact must not be the one
        renderer that stays silent about an unciteable hit."""
        from mempalace import cli

        assert cli._provenance_tag({"source_kind": "diary"}) == " ⟨diary⟩"

    def test_compact_tag_reports_stale_for_a_curated_file(self):
        from mempalace import cli

        hit = {"source_kind": "file", "source_stale": True}
        assert cli._provenance_tag(hit) == " ⟨stale⟩"

    def test_header_warns_when_only_transcripts_and_diaries_matched(self, capsys):
        """The real #451 query returns transcripts plus a palace diary chunk.

        Neither shape is a document anyone maintains, so a later correction
        could not be in this result set either — the warning has to fire here
        too, with wording that does not overclaim "all transcripts".
        """
        from mempalace import cli

        payload = self._fast_payload()
        payload["results"][1] = {
            "id": "diary_2g_20260906_074548_chunk_000020",
            "wing": "2g",
            "room": "diary",
            "snippet": "OVERTURNED: five handsets refused ONE CELL",
            "source_file": None,
            "rank": 0.4501,
        }

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=payload),
        ):
            try:
                cli.cmd_search(self._args(fmt="table"))
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "! none of the 2 hits came from a curated document" in out
        assert "all 2 hits are session-transcript copies" not in out

    def test_quiet_suppresses_the_header_but_keeps_per_hit_notes(self, capsys):
        from mempalace import cli

        args = self._args(fmt="table")
        args.quiet = True

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=self._fast_payload()),
        ):
            try:
                cli.cmd_search(args)
            except SystemExit:
                pass

        out = capsys.readouterr().out
        assert "session-transcript copies" not in out
        assert "⚠ quoted copy from a session transcript" in out
