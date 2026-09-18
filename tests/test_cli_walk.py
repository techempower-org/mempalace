"""
test_cli_walk.py — ``mempalace walk`` palace traversal (slice of #191, issue #359).

One verb over two tools: ``--follow palace`` (default) calls
``mempalace_walk_palace`` from a wing/room/entity anchor;
``--follow tunnels`` calls ``mempalace_traverse`` from a room anchor with
``--depth`` as its hop budget.

Issue #359's ``--from <drawer_id>`` is not offered — neither tool accepts a
drawer anchor — so the anchor validation is asserted here instead.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest


def _args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "wing": None,
        "room": None,
        "entity": None,
        "depth": 2,
        "limit": 50,
        "follow": "palace",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_WALK_OK = {
    "start": {"start_wing": "memorypalace"},
    "walk": [
        {"wing": "memorypalace", "room": "decisions", "drawer": "d1", "entity": "pgvector"},
        {"wing": "memorypalace", "room": "problems", "drawer": "d2", "entity": None},
    ],
    "stats": {
        "wings_touched": 1,
        "rooms_touched": 2,
        "drawers_touched": 2,
        "entities_touched": 1,
    },
}

# The real shape, captured from the production daemon on 2026-08-20: a bare
# JSON list of hop records whose fields are themselves lists.
_TRAVERSE_OK = [
    {
        "room": "decisions",
        "wings": ["memorypalace", "palace_daemon", "storyvox"],
        "halls": ["general"],
    },
    {"room": "problems", "wings": ["memorypalace"], "halls": []},
]

# A dict-wrapped variant, for the backends that answer that way.
_TRAVERSE_WRAPPED = {
    "start_room": "decisions",
    "connections": [{"room": "decisions", "wings": ["palace_daemon"], "hops": 1}],
}


def _daemon(payload):
    fake = MagicMock(return_value=payload)
    return (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._call_daemon_tool", fake),
        fake,
    )


class TestWalkAnchors:
    def test_wing_anchor(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WALK_OK)
        with strict, call:
            cli.cmd_walk(_args(wing="memorypalace"))

        name, payload = fake.call_args[0]
        assert name == "mempalace_walk_palace"
        assert payload == {"depth": 2, "limit": 50, "start_wing": "memorypalace"}

    def test_room_anchor(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WALK_OK)
        with strict, call:
            cli.cmd_walk(_args(room="problems", depth=1))

        assert fake.call_args[0][1] == {"depth": 1, "limit": 50, "start_room": "problems"}

    def test_entity_anchor(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WALK_OK)
        with strict, call:
            cli.cmd_walk(_args(entity="pgvector", limit=5))

        assert fake.call_args[0][1] == {"depth": 2, "limit": 5, "start_entity": "pgvector"}

    def test_no_anchor_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_WALK_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_walk(_args())

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "exactly one anchor" in capsys.readouterr().err

    def test_two_anchors_exit_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_WALK_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_walk(_args(wing="memorypalace", room="problems"))

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "exactly one anchor" in capsys.readouterr().err

    def test_depth_and_limit_clamped_to_tool_bounds(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WALK_OK)
        with strict, call:
            cli.cmd_walk(_args(wing="memorypalace", depth=99, limit=99999))

        payload = fake.call_args[0][1]
        assert payload["depth"] == 5
        assert payload["limit"] == 500


class TestWalkOutput:
    def test_table_lists_rows_and_stats(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_WALK_OK)
        with strict, call:
            cli.cmd_walk(_args(wing="memorypalace"))

        out = capsys.readouterr().out
        assert "WALK — start_wing=memorypalace" in out
        assert "room=decisions" in out
        assert "entity=pgvector" in out
        assert "rooms_touched=2" in out

    def test_empty_walk_message(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"start": {"start_wing": "ghost"}, "walk": []})
        with strict, call:
            cli.cmd_walk(_args(wing="ghost"))

        assert "reached nothing" in capsys.readouterr().out

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_WALK_OK)
        with strict, call:
            cli.cmd_walk(_args(wing="memorypalace", format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["stats"]["drawers_touched"] == 2
        assert len(payload["walk"]) == 2

    def test_backend_requirement_error_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"error": "tool_walk_palace requires MEMPALACE_BACKEND=postgres"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_walk(_args(wing="memorypalace"))

        assert exc.value.code == 2
        assert "requires MEMPALACE_BACKEND=postgres" in capsys.readouterr().err

    def test_daemon_unreachable_exits_2(self, capsys):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", side_effect=cli.DaemonError("down")),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_walk(_args(wing="memorypalace"))

        assert exc.value.code == 2
        assert "daemon unreachable" in capsys.readouterr().err

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_walk_palace", return_value=_WALK_OK) as local,
        ):
            cli.cmd_walk(_args(wing="memorypalace"))

        local.assert_called_once_with(depth=2, limit=50, start_wing="memorypalace")


class TestWalkFollowTunnels:
    def test_room_becomes_start_room_and_depth_becomes_max_hops(self):
        from mempalace import cli

        strict, call, fake = _daemon(_TRAVERSE_OK)
        with strict, call:
            cli.cmd_walk(_args(room="decisions", depth=3, follow="tunnels"))

        name, payload = fake.call_args[0]
        assert name == "mempalace_traverse"
        assert payload == {"start_room": "decisions", "max_hops": 3}

    def test_missing_room_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_TRAVERSE_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_walk(_args(wing="memorypalace", follow="tunnels"))

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "--room" in capsys.readouterr().err

    def test_table_renders_bare_list_payload(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_TRAVERSE_OK)
        with strict, call:
            cli.cmd_walk(_args(room="decisions", follow="tunnels"))

        out = capsys.readouterr().out
        assert "TUNNEL WALK — 2 hop(s)" in out
        assert "room=decisions" in out
        assert "wings (3): memorypalace, palace_daemon, storyvox" in out

    def test_table_renders_dict_wrapped_payload(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_TRAVERSE_WRAPPED)
        with strict, call:
            cli.cmd_walk(_args(room="decisions", follow="tunnels"))

        out = capsys.readouterr().out
        assert "TUNNEL WALK — 1 hop(s)" in out
        assert "hops: 1" in out

    def test_long_list_fields_are_previewed(self, capsys):
        from mempalace import cli

        wide = [{"room": "decisions", "wings": [f"w{i}" for i in range(30)]}]
        strict, call, _ = _daemon(wide)
        with strict, call:
            cli.cmd_walk(_args(room="decisions", follow="tunnels"))

        out = capsys.readouterr().out
        assert "wings (30):" in out
        assert "…" in out
        assert "w29" not in out

    def test_empty_traverse_message(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon([])
        with strict, call:
            cli.cmd_walk(_args(room="decisions", follow="tunnels"))

        assert "no connections" in capsys.readouterr().out

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_TRAVERSE_OK)
        with strict, call:
            cli.cmd_walk(_args(room="decisions", follow="tunnels", json=True))

        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert payload[0]["room"] == "decisions"
        assert len(payload) == 2

    def test_local_path_calls_traverse_tool(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_traverse_graph", return_value=_TRAVERSE_OK) as local,
        ):
            cli.cmd_walk(_args(room="decisions", follow="tunnels"))

        local.assert_called_once_with(start_room="decisions", max_hops=2)
