"""#408: session_id is threaded from tool_checkpoint's diary into tool_diary_write
and declared in both tools' input schemas (it was a write-only dead slot)."""

from unittest.mock import patch

from mempalace import mcp_server


def _tool(name):
    return (
        next(t for t in mcp_server.TOOLS_LIST if t["name"] == name)
        if hasattr(mcp_server, "TOOLS_LIST")
        else mcp_server.TOOLS[name]
    )


def test_schemas_declare_session_id():
    for name in ("mempalace_checkpoint", "mempalace_diary_write"):
        tool = _tool(name)
        props = tool["input_schema"]["properties"]
        if name == "mempalace_checkpoint":
            assert "session_id" in props["diary"]["properties"]
        else:
            assert "session_id" in props


def test_checkpoint_passes_session_id_to_diary_write():
    captured = {}

    def fake_diary_write(**kw):
        captured.update(kw)
        return {"success": True}

    with (
        patch.object(mcp_server, "tool_diary_write", side_effect=fake_diary_write),
        patch.object(
            mcp_server, "tool_check_duplicate", return_value={"is_duplicate": False}, create=True
        ),
        patch.object(
            mcp_server,
            "tool_add_drawer",
            return_value={"success": True, "drawer_id": "d"},
            create=True,
        ),
    ):
        mcp_server.tool_checkpoint(
            items=[],
            diary={"agent_name": "claude-code", "entry": "SESSION:x", "session_id": "sess-123"},
        )
    assert captured.get("session_id") == "sess-123"


# ── #408 ask 2: the drawers filed in the same call carry the id too ────
#
# #437 landed asks 1 and 3 (forward the diary's session_id to
# tool_diary_write, declare it in both input schemas). Ask 2 was left:
# "Attach it to the drawers filed in the same call, so a session's diary
# entry and its drawers can be recovered together rather than the summary
# alone." tool_add_drawer had no session_id parameter at all, so the
# drawers a checkpoint filed carried no session metadata.
#
# These tests assert against real drawer metadata in a temp palace, not a
# mocked call: the merged test above proves the argument is passed, which
# is a different claim from the id being stored.

import pytest  # noqa: E402

from mempalace.config import MAX_NAME_LENGTH  # noqa: E402


def _patch_mcp_server(monkeypatch, config, kg):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)


def _meta(drawer_id):
    """Stored metadata for one drawer, read straight off the collection."""
    from mempalace.mcp_server import _get_collection

    got = _get_collection().get(ids=[drawer_id], include=["metadatas"])
    assert got["ids"], f"drawer {drawer_id} was not stored"
    return got["metadatas"][0]


class TestCheckpointDrawerSessionId:
    def test_filed_drawers_carry_the_session_id(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_checkpoint

        out = tool_checkpoint(
            items=[{"wing": "w", "room": "sessions", "content": "a verbatim exchange"}],
            diary={"agent_name": "claude-code", "entry": "SESSION:x", "session_id": "sess-abc"},
        )

        assert out["added"], out
        assert _meta(out["added"][0]["drawer_id"])["session_id"] == "sess-abc"

    def test_diary_entry_and_its_drawers_share_one_id(self, monkeypatch, config, palace_path, kg):
        """The point of ask 2: recover the summary and the batch together."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_checkpoint

        out = tool_checkpoint(
            items=[
                {"wing": "w", "room": "sessions", "content": "first exchange"},
                {"wing": "w", "room": "sessions", "content": "second exchange"},
            ],
            diary={"agent_name": "claude-code", "entry": "SESSION:y", "session_id": "sess-xyz"},
        )

        assert out["diary"]["success"] is True
        drawer_ids = [a["drawer_id"] for a in out["added"]]
        assert len(drawer_ids) == 2
        assert {_meta(d)["session_id"] for d in drawer_ids} == {"sess-xyz"}
        assert _meta(out["diary"]["entry_id"])["session_id"] == "sess-xyz"

    def test_no_session_id_stores_no_key(self, monkeypatch, config, palace_path, kg):
        """Absent stays absent — hook-written drawers predate the field."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_checkpoint

        out = tool_checkpoint(
            items=[{"wing": "w", "room": "sessions", "content": "no session here"}],
            diary={"agent_name": "claude-code", "entry": "SESSION:z"},
        )

        assert "session_id" not in _meta(out["added"][0]["drawer_id"])

    def test_add_drawer_takes_session_id_directly(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(
            wing="w", room="sessions", content="direct call", session_id="sess-direct"
        )

        assert res["success"] is True
        assert _meta(res["drawer_id"])["session_id"] == "sess-direct"

    def test_add_drawer_schema_declares_session_id(self):
        """An undeclared-but-reachable parameter is the same dead slot
        #408 was filed about — no MCP client could set it."""
        from mempalace import mcp_server

        props = mcp_server.TOOLS["mempalace_add_drawer"]["input_schema"]["properties"]
        assert "session_id" in props


class TestSessionIdSanitization:
    """#408's open maintainer question: free-form or validated.

    Validated, mirroring the charset the live Stop/PreCompact hook
    already applies in ``palace-daemon/clients/hook.py``
    (``_sanitize_session_id``: ``re.sub(r"[^a-zA-Z0-9_-]", "", ...)``).
    Once the field is an indexed query key it is a query surface, and
    every other name-shaped field on this path is validated.

    Stripping rather than rejecting is deliberate: a malformed
    ``session_id`` must never fail the write and lose the memory, which
    is the same call the batch loop already makes for a dedup error.
    """

    @pytest.mark.parametrize(
        "raw,stored",
        [
            ("5f3a91c2-7b1e-4d8a-9c3f-2e6b8a1d4f70", "5f3a91c2-7b1e-4d8a-9c3f-2e6b8a1d4f70"),
            ("sess_123-ABC", "sess_123-ABC"),
            ("sess/../../etc/passwd", "sessetcpasswd"),
            ("sess id with spaces", "sessidwithspaces"),
            ("drop';--table", "drop--table"),  # `-` is in the hook's charset
        ],
    )
    def test_charset_matches_the_live_hook(self, monkeypatch, config, palace_path, kg, raw, stored):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(wing="w", room="sessions", content=f"content {raw}", session_id=raw)

        assert _meta(res["drawer_id"])["session_id"] == stored

    @pytest.mark.parametrize("raw", ["", "   ", "!!!", "///", None, 42, [], {}])
    def test_nothing_usable_omits_the_key_rather_than_storing_a_placeholder(
        self, monkeypatch, config, palace_path, kg, raw
    ):
        """An absent key is honest; ``"unknown"`` would be a real id that
        matches nothing (the hook needs a filename, metadata does not)."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(
            wing="w", room="sessions", content=f"content for {raw!r}", session_id=raw
        )

        assert res["success"] is True, "a bad session_id must never lose the memory"
        assert "session_id" not in _meta(res["drawer_id"])

    def test_over_length_is_capped_not_rejected(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(
            wing="w", room="sessions", content="long id", session_id="s" * (MAX_NAME_LENGTH + 50)
        )

        assert res["success"] is True
        assert _meta(res["drawer_id"])["session_id"] == "s" * MAX_NAME_LENGTH

    def test_diary_write_sanitizes_too(self, monkeypatch, config, palace_path, kg):
        """Both write paths share one rule, or the indexed key has two
        shapes depending on which tool wrote it."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_diary_write

        res = tool_diary_write(
            agent_name="claude-code",
            entry="SESSION:sanitized",
            session_id="sess/../x y",
        )

        assert res["success"] is True
        assert _meta(res["entry_id"])["session_id"] == "sessxy"


class TestSessionIdReachableOverMcp:
    """Declaring the field is not the same claim as a client being able
    to set it — #408's ask 3 is about the writers "in practice", which
    reach these tools over JSON-RPC, not by a direct Python call.
    """

    @staticmethod
    def _call(server, name, arguments):
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert "error" not in response, response
        import json

        return json.loads(response["result"]["content"][0]["text"])

    def test_add_drawer_over_json_rpc_stores_the_id(self, monkeypatch, config, palace_path, kg):
        from mempalace import mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        result = self._call(
            mcp_server,
            "mempalace_add_drawer",
            {
                "wing": "w",
                "room": "sessions",
                "content": "filed by an MCP client",
                "session_id": "sess-over-rpc",
            },
        )

        assert result["success"] is True
        assert _meta(result["drawer_id"])["session_id"] == "sess-over-rpc"

    def test_checkpoint_over_json_rpc_threads_the_id_to_both(
        self, monkeypatch, config, palace_path, kg
    ):
        from mempalace import mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        result = self._call(
            mcp_server,
            "mempalace_checkpoint",
            {
                "items": [{"wing": "w", "room": "sessions", "content": "rpc exchange"}],
                "diary": {
                    "agent_name": "claude-code",
                    "entry": "SESSION:rpc",
                    "session_id": "sess-rpc-both",
                },
            },
        )

        assert _meta(result["added"][0]["drawer_id"])["session_id"] == "sess-rpc-both"
        assert _meta(result["diary"]["entry_id"])["session_id"] == "sess-rpc-both"


class TestUnknownPlaceholderRejected:
    """The live hook's own fallback is ``return sanitized or "unknown"``
    (``palace-daemon/clients/hook.py::_sanitize_session_id``), so once the
    hook starts sending the field it can send the literal ``"unknown"``.

    Mirroring the hook's charset without mirroring that fallback would
    store ``"unknown"`` as a real session id — and then every session
    whose raw id sanitized to nothing pools under one name, which is the
    exact harm ``sanitize_session_id``'s docstring refuses a placeholder
    for. Rejecting the sentinel is what makes the "omit, don't
    substitute" rule actually hold end to end.
    """

    @pytest.mark.parametrize("raw", ["unknown", "UNKNOWN", "Unknown", "unknown!!", "  unknown  "])
    def test_sanitizer_refuses_the_placeholder(self, raw):
        from mempalace.config import sanitize_session_id

        assert sanitize_session_id(raw) == "", f"{raw!r} must not become a session id"

    def test_unknown_is_not_written_to_drawer_metadata(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(
            wing="w", room="sessions", content="filed by the hook fallback", session_id="unknown"
        )

        assert res["success"] is True
        assert "session_id" not in _meta(res["drawer_id"])

    def test_a_real_id_containing_unknown_still_stores(self, monkeypatch, config, palace_path, kg):
        """Only the bare sentinel is refused — not every id mentioning it."""
        from mempalace.config import sanitize_session_id

        assert sanitize_session_id("unknown-7f3a") == "unknown-7f3a"
        assert sanitize_session_id("sess-unknown-2") == "sess-unknown-2"
        # Boundary held deliberately narrow: only the sentinel itself is
        # refused, so the guard can never swallow an id a real writer
        # emits. No writer produces "-unknown-".
        assert sanitize_session_id("-unknown-") == "-unknown-"
