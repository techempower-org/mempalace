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
