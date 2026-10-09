"""The no-curated banner names the depth EXAMINED, not the limit (#549).

Observed live on 2026-09-19: ``mempalace search … --wing 2g --limit 3`` ran
#534's deep fetch to 30, found no curated document, truncated to 3, and then
printed "no curated document in the top 3", because the banner read
``len(results)`` after truncation. The true and stronger statement is
"in the top 30". ``depth_examined`` now records the pre-truncation depth on
both routes, and the banner uses it.

Helpers are copied from test_search_depth.py rather than imported: ``tests``
is a namespace package, and a sibling import resolves to the editable
install's main checkout, not this tree (#546).
"""

import argparse
import json
from unittest.mock import patch

import pytest

_TRANSCRIPT = "a session transcript discussing the parameter handler at length"
_CURATED = "the curated finding that records the bounds and the correction"


def _hit(i, kind, text):
    return {
        "id": f"d{i}",
        "wing": "2g",
        "room": "problems",
        "snippet": f"{text} number {i}",
        "source_file": ("/p/findings.md" if kind == "file" else f"/p/s{i}.jsonl"),
        "rank": 0.6 - i * 0.01,
    }


def _payload(n, curated_at=None):
    rows = []
    for i in range(1, n + 1):
        kind = "file" if (curated_at and i == curated_at) else "transcript"
        rows.append(_hit(i, kind, _CURATED if kind == "file" else _TRANSCRIPT))
    return {"results": rows}


def _args(fmt, mode, limit=3):
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


def _run(capsys, payloads, fmt="json", mode="fast", limit=3):
    from mempalace import cli

    env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
    # The fast route GETs through _call_daemon_rest; the hybrid route POSTs
    # through _post_daemon_rest. Each test's call-count precondition proves the
    # mock actually sat on the path taken.
    target = "_call_daemon_rest" if mode == "fast" else "_post_daemon_rest"
    with (
        patch.dict("os.environ", env, clear=True),
        patch(f"mempalace.cli.{target}", side_effect=payloads) as m,
    ):
        try:
            cli.cmd_search(_args(fmt, mode, limit))
        except SystemExit:
            pass
    return capsys.readouterr(), m


@pytest.mark.parametrize("mode", ["fast", "hybrid"])
def test_json_reports_the_deep_depth_when_the_deep_fetch_ran(capsys, mode):
    out, m = _run(capsys, [_payload(3), _payload(30)], mode=mode)
    assert m.call_count == 2, "precondition: the deep fetch must have run"
    data = json.loads(out.out)
    assert len(data["results"]) == 3, "results are still truncated to the limit"
    assert data["depth_examined"] == 30


@pytest.mark.parametrize("mode", ["fast", "hybrid"])
def test_banner_names_the_examined_depth_not_the_limit(capsys, mode):
    out, _ = _run(capsys, [_payload(3), _payload(30)], fmt="table", mode=mode)
    assert "no curated document in the top 30" in out.out, out.out
    assert "no curated document in the top 3\n" not in out.out


@pytest.mark.parametrize("mode", ["fast", "hybrid"])
def test_without_a_deep_fetch_the_depth_is_the_shallow_list(capsys, mode):
    out, m = _run(capsys, [_payload(3, curated_at=2)], mode=mode)
    assert m.call_count == 1, "precondition: curated in the shallow list, no deep fetch"
    assert json.loads(out.out)["depth_examined"] == 3


def test_banner_falls_back_to_the_list_length_without_the_field():
    """Older daemons or other callers that never set the field keep the old wording."""
    import io
    from contextlib import redirect_stdout

    from mempalace import cli

    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_search_header(
            "q", {"results": _payload(3)["results"]}, None, None, [], False, False
        )
    assert "no curated document in the top 3" in buf.getvalue()
