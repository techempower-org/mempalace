"""Tests for the legacy ``hallways`` CLI verb.

Since #407 the plural verb is a deprecated alias that delegates to
``cmd_hallway_list``, so it reaches its rows through the sanitizing
``mempalace_list_hallways`` tool rather than calling ``list_hallways``
directly. These tests therefore patch the tool and pass ``--palace``,
which pins the local route regardless of any ambient
``PALACE_DAEMON_URL``. The daemon route, the wing sanitization and the
``deprecated`` marker are covered in
``tests/test_cli_read_family.py::TestLegacyHallwaysAlias``.
"""

import json
import os
from argparse import Namespace
from unittest.mock import patch

import pytest

import mempalace.hallways as hallways_mod
from mempalace.cli import cmd_hallways


def _args(**overrides):
    defaults = {
        "wing": None,
        "limit": 50,
        "palace": "/selected/palace",
        "json": False,
        "format": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def _rows(*specs):
    return [
        {
            "id": hid,
            "wing": "w",
            "entity_a": a,
            "entity_b": b,
            "co_occurrence_count": count,
        }
        for hid, a, b, count in specs
    ]


def test_lists_sorted_by_count(capsys):
    rows = _rows(("hw-cd", "C", "D", 1), ("hw-ab", "A", "B", 3))
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=rows):
        cmd_hallways(_args())

    out = capsys.readouterr().out
    assert "HALLWAYS — 2" in out
    # Highest co-occurrence first.
    assert out.index("A ↔ B") < out.index("C ↔ D")


def test_respects_limit(capsys):
    rows = _rows(*[(f"hw-{i}", f"E{i}", "X", i) for i in range(5)])
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=rows):
        cmd_hallways(_args(limit=2))

    assert capsys.readouterr().out.count("↔") == 2


def test_negative_limit_shows_nothing_not_tail(capsys):
    rows = _rows(*[(f"hw-{i}", f"E{i}", "X", i) for i in range(5)])
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=rows):
        cmd_hallways(_args(limit=-2))

    out = capsys.readouterr().out
    # A negative limit must not slice from the end (which would print
    # all-but-2), and must not fold into the "0 = all" sentinel either.
    assert out.count("↔") == 0
    assert "no rows shown" in out


def test_zero_limit_shows_everything(capsys):
    """``0`` is the documented show-all sentinel on the surviving verb."""
    rows = _rows(*[(f"hw-{i}", f"E{i}", "X", i) for i in range(5)])
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=rows):
        cmd_hallways(_args(limit=0))

    assert capsys.readouterr().out.count("↔") == 5


def test_empty_message(capsys):
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=[]):
        cmd_hallways(_args(wing="x"))

    out = capsys.readouterr().out
    assert "no hallways" in out
    assert "when you mine" in out


def test_explicit_palace_scopes_hallway_listing(tmp_path):
    """``--palace`` must be in force *while the tool runs* — it is
    applied by redirecting ``MEMPALACE_PALACE_PATH`` for the call, not
    by passing a config object down."""
    selected = tmp_path / "selected" / "palace"
    seen = {}

    def fake_tool(wing=None):
        seen["wing"] = wing
        seen["palace"] = os.environ.get("MEMPALACE_PALACE_PATH")
        return []

    with patch("mempalace.mcp_server.tool_list_hallways", side_effect=fake_tool):
        cmd_hallways(_args(wing="wing_aya", palace=str(selected)))

    assert seen == {"wing": "wing_aya", "palace": str(selected)}


def test_deprecation_notice_on_stderr(capsys):
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=[]):
        cmd_hallways(_args())

    err = capsys.readouterr().err
    assert "deprecated" in err
    assert "hallway list" in err


def test_json_surface_honours_limit_and_marks_deprecation(capsys):
    rows = _rows(("hw-ab", "A", "B", 3), ("hw-cd", "C", "D", 1))
    with patch("mempalace.mcp_server.tool_list_hallways", return_value=rows):
        cmd_hallways(_args(limit=1, json=True))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["total"] == 2
    assert [h["id"] for h in payload["hallways"]] == ["hw-ab"]
    assert payload["deprecated"] == "use `mempalace hallway list`"
    assert captured.err == "", "the JSON surface carries the marker instead of a notice"


def test_tool_error_envelope_exits_2(capsys):
    with patch(
        "mempalace.mcp_server.tool_list_hallways",
        return_value={"error": "wing contains illegal characters"},
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_hallways(_args(wing="../etc"))

    assert exc.value.code == 2
    assert "illegal" in capsys.readouterr().err


def test_prune_spellings_dry_run_then_apply(monkeypatch, capsys):
    calls = []

    def fake_prune(config=None, apply=False):
        calls.append(apply)
        return {
            "total": 10,
            "self_links": 2,
            "duplicates": 1,
            "by_wing": {"w": 3},
            "sample": ["main.zig ↔ src/main.zig"],
            "removed": 3 if apply else 0,
        }

    monkeypatch.setattr(hallways_mod, "prune_spelling_hallways", fake_prune)
    cmd_hallways(Namespace(wing=None, limit=50, prune_spellings=True, yes=False))
    out = capsys.readouterr().out
    assert "3 of 10 hallways are spelling artifacts" in out
    assert "Dry run" in out
    cmd_hallways(Namespace(wing=None, limit=50, prune_spellings=True, yes=True))
    assert "Removed 3." in capsys.readouterr().out
    assert calls == [False, True]


def test_rebuild_recomputes_every_wing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: object())
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: lambda path, name: [("r", "b", "", 1), ("r", "a", "", 1)],
    )
    monkeypatch.setattr(
        hallways_mod,
        "compute_hallways_for_wing",
        lambda wing, col=None, config=None: (
            calls.append(wing) or [{"id": 1}] * (2 if wing == "a" else 1)
        ),
    )
    import contextlib

    locked = []

    @contextlib.contextmanager
    def fake_lock(path):
        locked.append(path)
        calls.append("<lock>")
        yield

    monkeypatch.setattr("mempalace.palace.mine_palace_lock", fake_lock)
    cmd_hallways(Namespace(wing=None, limit=50, rebuild=True, palace="/p"))
    out = capsys.readouterr().out
    # Scan and replace run under the palace writer lock, serialized with mines.
    assert locked == ["/p"] and calls[0] == "<lock>"
    calls.remove("<lock>")
    assert calls == ["a", "b"]
    assert "Rebuilt 3 hallways across 2 wing(s)." in out
    cmd_hallways(Namespace(wing="a", limit=50, rebuild=True, palace="/p"))
    assert calls[-1] == "a"


def test_rebuild_refuses_cleanly_when_the_palace_is_held(monkeypatch, capsys):
    import contextlib

    from mempalace.palace import MineAlreadyRunning

    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: object())

    @contextlib.contextmanager
    def held(path):
        raise MineAlreadyRunning(f"palace {path} is held by PID 42 (hub)")
        yield  # pragma: no cover

    monkeypatch.setattr("mempalace.palace.mine_palace_lock", held)
    with pytest.raises(SystemExit) as exc:
        cmd_hallways(Namespace(wing="a", limit=50, rebuild=True, palace="/p"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "held by PID 42" in err and "Traceback" not in err
