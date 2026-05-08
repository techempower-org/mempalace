"""
test_mcp_server_daemon.py — daemon-routing for the MCP server.

Mirrors the ``PALACE_DAEMON_URL`` pattern that ``hooks_cli.py`` already
implements (see ``tests/test_hooks_cli.py`` daemon-routed mining tests).
When ``PALACE_DAEMON_URL`` is set and ``PALACE_DAEMON_STRICT != "0"``,
``handle_request`` must forward the entire JSON-RPC envelope to the
daemon's ``/mcp`` endpoint and return its response verbatim — never
opening a local chromadb client.

The single chokepoint is ``handle_request``; routing every JSON-RPC
method through the daemon is functionally equivalent to per-handler
gates and avoids 30+ duplicated branches.
"""

import json
from unittest.mock import patch


# ── _daemon_strict gate ─────────────────────────────────────────────────


class TestDaemonStrictGate:
    def test_returns_true_when_url_set_and_strict_default(self):
        from mempalace.mcp_server import _daemon_strict

        with patch.dict("os.environ", {"PALACE_DAEMON_URL": "http://x:8085"}, clear=True):
            assert _daemon_strict() is True

    def test_returns_false_when_url_unset(self):
        from mempalace.mcp_server import _daemon_strict

        with patch.dict("os.environ", {}, clear=True):
            assert _daemon_strict() is False

    def test_returns_false_when_url_blank(self):
        from mempalace.mcp_server import _daemon_strict

        with patch.dict("os.environ", {"PALACE_DAEMON_URL": "   "}, clear=True):
            assert _daemon_strict() is False

    def test_strict_zero_disables_routing(self):
        """PALACE_DAEMON_STRICT=0 is the escape hatch matching hooks_cli."""
        from mempalace.mcp_server import _daemon_strict

        env = {"PALACE_DAEMON_URL": "http://x:8085", "PALACE_DAEMON_STRICT": "0"}
        with patch.dict("os.environ", env, clear=True):
            assert _daemon_strict() is False


# ── _forward_to_daemon helper ───────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class TestForwardToDaemon:
    def test_posts_to_mcp_endpoint_with_api_key(self):
        from mempalace.mcp_server import _forward_to_daemon

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["api_key"] = req.get_header("X-api-key")
            captured["method"] = req.get_method()
            return _FakeResp(b'{"jsonrpc":"2.0","id":1,"result":{}}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_API_KEY": "k123"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                resp = _forward_to_daemon(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                )

        assert captured["url"] == "http://daemon.example:8085/mcp"
        assert captured["method"] == "POST"
        assert captured["api_key"] == "k123"
        body = json.loads(captured["body"].decode())
        assert body["method"] == "tools/list"
        assert resp["result"] == {}

    def test_strips_trailing_slash_from_url(self):
        from mempalace.mcp_server import _forward_to_daemon

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResp(b'{"jsonrpc":"2.0","id":1,"result":{}}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085/"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                _forward_to_daemon({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

        assert captured["url"] == "http://daemon.example:8085/mcp"

    def test_omits_api_key_header_when_unset(self):
        from mempalace.mcp_server import _forward_to_daemon

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["api_key"] = req.get_header("X-api-key")
            return _FakeResp(b'{"jsonrpc":"2.0","id":1,"result":{}}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                _forward_to_daemon({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

        assert captured["api_key"] is None

    def test_returns_jsonrpc_error_on_network_failure(self):
        """Daemon-strict means daemon-only — failure must surface to the
        caller as JSON-RPC error, never silently fall through to local.
        Mirrors the consumer-facing contract of clients/mempalace-mcp.py.
        """
        from mempalace.mcp_server import _forward_to_daemon

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
                resp = _forward_to_daemon(
                    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}
                )

        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 7
        assert "error" in resp
        assert resp["error"]["code"] == -32000
        assert "daemon" in resp["error"]["message"].lower()

    def test_returns_jsonrpc_error_on_invalid_response(self):
        from mempalace.mcp_server import _forward_to_daemon

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", return_value=_FakeResp(b"not json")):
                resp = _forward_to_daemon(
                    {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}}
                )

        assert "error" in resp
        assert resp["id"] == 9


# ── handle_request integration ──────────────────────────────────────────


class TestHandleRequestForwarding:
    def test_forwards_tools_call_when_daemon_strict(self):
        """tools/call must go over HTTP, not invoke any TOOLS handler."""
        from mempalace import mcp_server

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"jsonrpc":"2.0","id":42,"result":{"forwarded":true}}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                # If the gate fails, this would call tool_search → open
                # local chromadb. The fake_urlopen running proves it
                # didn't reach the local handler.
                resp = mcp_server.handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 42,
                        "method": "tools/call",
                        "params": {"name": "mempalace_search", "arguments": {"query": "x"}},
                    }
                )

        assert captured["url"] == "http://daemon.example:8085/mcp"
        assert captured["body"]["params"]["name"] == "mempalace_search"
        assert resp["result"] == {"forwarded": True}

    def test_forwards_initialize_when_daemon_strict(self):
        """Even initialize is forwarded — the daemon's /mcp negotiates
        protocolVersion against its own SUPPORTED_PROTOCOL_VERSIONS, so
        the client always sees the daemon's authoritative answer.
        """
        from mempalace import mcp_server

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",'
                b'"capabilities":{"tools":{}},"serverInfo":{"name":"mempalace","version":"x"}}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                resp = mcp_server.handle_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                )

        assert resp["result"]["serverInfo"]["name"] == "mempalace"

    def test_local_path_when_daemon_unset(self):
        """Without PALACE_DAEMON_URL, behavior is unchanged: handle_request
        responds to initialize locally without any HTTP."""
        from mempalace import mcp_server

        with patch.dict("os.environ", {}, clear=True):
            with patch("urllib.request.urlopen") as mock_open:
                resp = mcp_server.handle_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                )

        mock_open.assert_not_called()
        assert resp["result"]["serverInfo"]["name"] == "mempalace"

    def test_strict_zero_keeps_local_path(self):
        """Escape hatch: PALACE_DAEMON_STRICT=0 disables routing even
        when PALACE_DAEMON_URL is set."""
        from mempalace import mcp_server

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_DAEMON_STRICT": "0"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen") as mock_open:
                resp = mcp_server.handle_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
                )

        mock_open.assert_not_called()
        assert resp["result"] == {}

    def test_forwarded_error_propagates(self):
        """When the daemon answers with a JSON-RPC error, surface it
        unmodified — don't synthesize success."""
        from mempalace import mcp_server

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":3,"error":{"code":-32601,"message":"Unknown tool: x"}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                resp = mcp_server.handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "x", "arguments": {}},
                    }
                )

        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_no_local_handler_invoked_in_strict_mode(self):
        """Spy on TOOLS dict — daemon-strict path must never call any
        local handler (which would open chromadb / hit local palace)."""
        from mempalace import mcp_server

        def fake_urlopen(req, timeout=None):
            return _FakeResp(b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}')

        # Replace every handler with a sentinel that fails the test if invoked.
        sentinels = {
            name: {**spec, "handler": _fail_handler(name)}
            for name, spec in mcp_server.TOOLS.items()
        }

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch.object(mcp_server, "TOOLS", sentinels):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    mcp_server.handle_request(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "mempalace_status", "arguments": {}},
                        }
                    )


def _fail_handler(name):
    def _raise(*a, **kw):
        raise AssertionError(f"local handler {name!r} ran in daemon-strict mode")

    return _raise
