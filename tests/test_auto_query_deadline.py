"""The auto-query classifier honours the hook's deadline.

The palace-auto-query hook kills the classifier at its budget (4 s), but the
classifier allowed its daemon calls 2 s (wings) + 2 s (fast route) + 8 s
(hybrid). A hybrid search slower than ~3.5 s was killed mid-call: no result
and no receipt saying what was slow. That was the residual "palace slow"
fire, measured 2026-10-12 at light load with no KG timeout.

With MEMPALACE_AQ_DEADLINE set, every daemon call is capped to the time left
and skipped when none is left, so the classifier degrades itself and still
prints a receipt. Unset, nothing changes.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from mempalace.auto_query import MCPCall, runner

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_deadline(monkeypatch):
    monkeypatch.delenv("MEMPALACE_AQ_DEADLINE", raising=False)


def _set_deadline(monkeypatch, seconds_from_now):
    monkeypatch.setenv("MEMPALACE_AQ_DEADLINE", repr(time.time() + seconds_from_now))


# --- deadline_timeout -------------------------------------------------------------


def test_without_a_deadline_the_default_is_unchanged(no_deadline):
    assert runner.deadline_timeout(8) == 8
    assert runner.deadline_timeout(2) == 2


def test_a_far_deadline_keeps_the_default(monkeypatch):
    _set_deadline(monkeypatch, 60)
    assert runner.deadline_timeout(8) == 8


def test_a_near_deadline_caps_the_timeout(monkeypatch):
    _set_deadline(monkeypatch, 1.0)
    got = runner.deadline_timeout(8)
    assert got is not None and 0.4 < got <= 0.7, got


def test_a_passed_deadline_means_skip(monkeypatch):
    _set_deadline(monkeypatch, -1)
    assert runner.deadline_timeout(8) is None


def test_a_garbage_deadline_is_ignored(monkeypatch):
    monkeypatch.setenv("MEMPALACE_AQ_DEADLINE", "soon")
    assert runner.deadline_timeout(8) == 8


# --- each daemon call is bounded ---------------------------------------------------


class _Cfg:
    daemon_url = "http://127.0.0.1:9"


def _recording_urlopen(seen):
    def fake(req, timeout=None):
        seen.append(timeout)
        raise urllib.error.URLError("stub")

    return fake


@pytest.mark.parametrize("call", ["mcp", "fast", "wings"])
def test_every_daemon_call_passes_a_capped_timeout(monkeypatch, call):
    from mempalace.auto_query import __main__ as aq_main

    _set_deadline(monkeypatch, 1.0)
    seen = []
    with patch("urllib.request.urlopen", _recording_urlopen(seen)):
        if call == "mcp":
            runner._call_mcp(MCPCall("mempalace_search", {"query": "q"}), _Cfg())
        elif call == "fast":
            runner._call_fast_search("q", "w", 3, _Cfg())
        else:
            aq_main._fetch_wings_from_daemon(_Cfg())
    assert len(seen) == 1, f"{call}: precondition, exactly one call made"
    assert seen[0] <= 0.7, f"{call}: timeout {seen[0]} outlives the deadline"


@pytest.mark.parametrize("call", ["mcp", "fast", "wings"])
def test_no_call_is_made_once_the_deadline_has_passed(monkeypatch, call):
    from mempalace.auto_query import __main__ as aq_main

    _set_deadline(monkeypatch, -1)
    seen = []
    with patch("urllib.request.urlopen", _recording_urlopen(seen)):
        if call == "mcp":
            out = runner._call_mcp(MCPCall("mempalace_search", {"query": "q"}), _Cfg())
        elif call == "fast":
            out = runner._call_fast_search("q", "w", 3, _Cfg())
        else:
            out = aq_main._fetch_wings_from_daemon(_Cfg())
    assert seen == [], f"{call}: no call may be made past the deadline"
    assert out in (None, set())


# --- end to end: the real classifier against a daemon that hangs ------------------


class _Hang(BaseHTTPRequestHandler):
    def _hang(self):
        time.sleep(6)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        except OSError:
            pass

    do_GET = _hang
    do_POST = _hang

    def log_message(self, *a):
        pass


@pytest.fixture
def hanging_daemon():
    srv = HTTPServer(("127.0.0.1", 0), _Hang)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# An explicit recall needs a "?" (signals._check_explicit). It takes the hybrid
# /mcp path, the call that outlived the hook budget.
_PROMPT = "remind me what the drain mine heartbeat was for in palace-daemon?"


def _classify(daemon_url, deadline_s, tmp_path):
    env = dict(
        os.environ,
        PALACE_DAEMON_URL=daemon_url,
        PALACE_API_KEY="x",
        HOME=str(tmp_path),
        PYTHONPATH=str(REPO),
        # HOME is empty, so there is no config: enable it as the live config does.
        AUTO_QUERY_ENABLED="1",
        AUTO_QUERY_MODE="balanced",
    )
    env.pop("MEMPALACE_AQ_DEADLINE", None)
    if deadline_s is not None:
        env["MEMPALACE_AQ_DEADLINE"] = repr(time.time() + deadline_s)
    t0 = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mempalace.auto_query",
            "--prompt",
            _PROMPT,
            "--wing",
            "memorypalace",
            "--session-id",
            "deadline-e2e",
            "--turn",
            "2",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return time.monotonic() - t0, proc


def test_the_classifier_finishes_by_the_deadline_with_a_receipt(hanging_daemon, tmp_path):
    elapsed, proc = _classify(hanging_daemon, 2.0, tmp_path)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert elapsed < 3.0, f"took {elapsed:.1f}s against a 2.0s deadline"
    receipt = [ln for ln in proc.stderr.splitlines() if ln.startswith("RECEIPT: ")]
    assert receipt, "a degraded run must still say what happened:\n" + proc.stderr[-400:]
    assert json.loads(receipt[0][len("RECEIPT: ") :]).get("error"), receipt[0]


def test_without_a_deadline_the_classifier_waits_out_the_hang(hanging_daemon, tmp_path):
    """Positive control: the same hang, no deadline, outlives a 4 s hook budget."""
    elapsed, _ = _classify(hanging_daemon, None, tmp_path)
    assert elapsed > 4.0, f"precondition: the hang must outlast the hook budget ({elapsed:.1f}s)"
