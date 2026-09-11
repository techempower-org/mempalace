"""Post-compaction re-injection (techempower-org/mempalace#449).

Measured incident (2026-09-04): after a context compaction an agent keeps its
session id, so the auto-query dedupe file
``~/.mempalace/auto_query/injected/<session>.json`` survives — and it lists
exactly the drawers the agent just lost. One live session had accumulated 119
suppressed drawer ids. The dedupe (#429) therefore made post-compaction recall
*worse*, and worse the longer the session ran.

Second cause: the post-compaction hooks injected a pointer ("run
``mempalace wake-up``"), and fleet evidence 2026-09-03 is that agents do not
act on invitations — hooks that inject without asking are the only ones that
land.

These tests pin both halves plus the fail-open behaviour a SessionStart hook
requires: it must never break the session, whatever the daemon is doing.
"""

import io
import json
import os

import pytest

from mempalace import compact_recovery as cr
from mempalace.auto_query import injected as inj


class _Cfg:
    auto_query_enabled = True
    auto_query_mode = "balanced"
    auto_query_depth_cache_ttl = 0
    auto_query_min_similarity = 0.50
    auto_query_min_bm25 = 1.5
    daemon_url = "http://127.0.0.1:1"
    compact_recovery_max_chars = 4000

    def resolve_wing(self, name):
        return name.lower().replace("-", "_").replace(".", "_")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _wakeup(text):
    """Stub of the daemon's ``mempalace_wakeup`` tool result."""

    def _call(tool, args, config, timeout=None):
        assert tool == "mempalace_wakeup"
        return {"text": text, "tokens": len(text) // 4, "wing": args.get("wing", "")}

    return _call


# --- (a) the dedupe file ----------------------------------------------------


def test_clear_injected_drops_the_sessions_dedupe_file(home):
    inj.remember_injected("sess-1", ["d1", "d2", "d3"])
    assert inj.load_injected("sess-1") == {"d1", "d2", "d3"}

    assert inj.clear_injected("sess-1") == 3
    assert inj.load_injected("sess-1") == set()
    assert not os.path.exists(inj.injected_path("sess-1"))


def test_clear_injected_is_a_noop_for_a_session_that_never_fired(home):
    assert inj.clear_injected("never-seen") == 0


def test_clear_injected_touches_only_its_own_session(home):
    inj.remember_injected("sess-1", ["d1"])
    inj.remember_injected("sess-2", ["d2"])
    inj.clear_injected("sess-1")
    assert inj.load_injected("sess-2") == {"d2"}


def test_compaction_unsuppresses_exactly_the_drawers_the_agent_lost(home, monkeypatch):
    """The whole point of #449, end to end through the auto-query filter."""
    from mempalace.auto_query.runner import _filter_search_results

    hit = {
        "drawer_id": "lost-1",
        "similarity": 0.71,
        "text": "the decision the agent just forgot",
        "room": "decisions",
    }
    inj.remember_injected("sess-c", ["lost-1"])
    # Before compaction recovery: suppressed, which is correct mid-context…
    assert _filter_search_results({"results": [hit]}, "sess-c", _Cfg())["results"] == []

    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("## L1 — ESSENTIAL STORY\nstuff"))
    cr.recover(session_id="sess-c", wing="memorypalace", config=_Cfg())

    # …after it, the same drawer is eligible again.
    kept = _filter_search_results({"results": [hit]}, "sess-c", _Cfg())["results"]
    assert [r["drawer_id"] for r in kept] == ["lost-1"]


def test_recover_reports_how_many_drawers_it_unsuppressed(home, monkeypatch):
    inj.remember_injected("sess-n", ["a", "b", "c", "d"])
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("## L1 — ESSENTIAL STORY\nstuff"))
    out = cr.recover(session_id="sess-n", wing="w", config=_Cfg())
    assert out.cleared == 4
    assert out.receipt["cleared"] == 4
    assert "4" in out.system_message


# --- (b) content, not a pointer ---------------------------------------------


def test_recovery_injects_wake_up_content_not_an_invitation(home, monkeypatch):
    story = "## L1 — ESSENTIAL STORY\n[decisions]\n  - pgvector cutover 2026-05-14"
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup(story))
    out = cr.recover(session_id="s", wing="memorypalace", config=_Cfg())

    assert "pgvector cutover 2026-05-14" in out.context
    # An invitation is what #449 is replacing: the block must not be only a
    # command to run. Content first, any pointer strictly after it.
    body = out.context
    assert body.index("pgvector cutover") < len(body)
    assert not body.strip().endswith("mempalace wake-up --wing memorypalace`")


def test_recovery_context_is_capped_to_the_injection_budget(home, monkeypatch):
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("x" * 20000))

    class _Small(_Cfg):
        compact_recovery_max_chars = 500

    out = cr.recover(session_id="s", wing="w", config=_Small())
    assert len(out.context) <= 500 + 400  # budget caps the palace text, not the frame
    assert "…" in out.context or "truncated" in out.context


def test_recovery_names_the_wing_and_the_session(home, monkeypatch):
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("## L1 — ESSENTIAL STORY\ns"))
    out = cr.recover(session_id="s", wing="memorypalace", config=_Cfg())
    assert "memorypalace" in out.context


# --- fail-open --------------------------------------------------------------


def test_daemon_down_still_clears_the_dedupe_and_never_raises(home, monkeypatch):
    inj.remember_injected("sess-d", ["a", "b"])

    def _boom(tool, args, config, timeout=None):
        return None

    monkeypatch.setattr(cr, "_call_daemon_tool", _boom)
    out = cr.recover(session_id="sess-d", wing="w", config=_Cfg())
    assert out.cleared == 2
    assert inj.load_injected("sess-d") == set()
    # No content to inject, so no additionalContext — but the human still sees
    # a receipt saying the palace was unreachable.
    assert out.context == ""
    assert "unreachable" in out.system_message


# --- hook envelope ----------------------------------------------------------


def test_hook_payload_is_a_sessionstart_envelope(home, monkeypatch):
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("## L1 — ESSENTIAL STORY\nx"))
    out = cr.recover(session_id="s", wing="w", config=_Cfg())
    payload = out.hook_payload()
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "ESSENTIAL STORY" in payload["hookSpecificOutput"]["additionalContext"]
    assert payload["systemMessage"].startswith("✦ palace")


def test_hook_payload_omits_additional_context_when_there_is_none(home, monkeypatch):
    monkeypatch.setattr(cr, "_call_daemon_tool", lambda *a, **k: None)
    out = cr.recover(session_id="s", wing="w", config=_Cfg())
    assert "hookSpecificOutput" not in out.hook_payload()
    assert out.hook_payload()["systemMessage"]


# --- the CLI the hook calls -------------------------------------------------


def _run(monkeypatch, capsys, stdin_json, argv=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stdin_json)))
    rc = cr.main(argv or [])
    return rc, capsys.readouterr().out


def test_main_reads_session_and_wing_from_the_hook_stdin(home, monkeypatch, capsys):
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("## L1 — ESSENTIAL STORY\nx"))
    monkeypatch.setattr(cr, "_load_config", lambda: _Cfg())
    rc, out = _run(
        monkeypatch,
        capsys,
        {"session_id": "abc", "cwd": "/home/jp/Projects/MemoryPalace", "source": "compact"},
    )
    assert rc == 0
    payload = json.loads(out)
    assert "ESSENTIAL STORY" in payload["hookSpecificOutput"]["additionalContext"]
    assert "memorypalace" in payload["systemMessage"]


def test_main_stays_silent_when_the_session_did_not_compact(home, monkeypatch, capsys):
    monkeypatch.setattr(cr, "_load_config", lambda: _Cfg())
    called = []
    monkeypatch.setattr(cr, "_call_daemon_tool", lambda *a, **k: called.append(1))
    rc, out = _run(monkeypatch, capsys, {"session_id": "abc", "cwd": "/x", "source": "startup"})
    assert rc == 0
    assert out == ""
    assert called == []


def test_main_flags_override_stdin(home, monkeypatch, capsys):
    monkeypatch.setattr(cr, "_call_daemon_tool", _wakeup("## L1 — ESSENTIAL STORY\nx"))
    monkeypatch.setattr(cr, "_load_config", lambda: _Cfg())
    rc, out = _run(
        monkeypatch,
        capsys,
        {"session_id": "abc", "cwd": "/x", "source": "startup"},
        argv=["--session-id", "zzz", "--wing", "ha", "--source", "compact"],
    )
    assert rc == 0
    assert "ha" in json.loads(out)["systemMessage"]


def test_main_never_fails_the_hook_on_garbage_stdin(home, monkeypatch, capsys):
    monkeypatch.setattr(cr, "_load_config", lambda: _Cfg())
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert cr.main([]) == 0
    assert capsys.readouterr().out == ""


# --- the budget knob --------------------------------------------------------


def test_budget_is_configurable_and_defaults_to_4000(home, monkeypatch):
    from mempalace.config import MempalaceConfig

    monkeypatch.delenv("COMPACT_RECOVERY_MAX_CHARS", raising=False)
    assert MempalaceConfig().compact_recovery_max_chars == 4000
    monkeypatch.setenv("COMPACT_RECOVERY_MAX_CHARS", "1200")
    assert MempalaceConfig().compact_recovery_max_chars == 1200
    monkeypatch.setenv("COMPACT_RECOVERY_MAX_CHARS", "nonsense")
    assert MempalaceConfig().compact_recovery_max_chars == 4000


def test_clear_reports_zero_when_the_file_could_not_be_removed(home, monkeypatch):
    """The count is the effect, not the intent — never claim a clear that failed."""
    inj.remember_injected("sess-ro", ["a", "b"])

    def _refuse(_path):
        raise OSError("read-only")

    monkeypatch.setattr(inj.os, "remove", _refuse)
    assert inj.clear_injected("sess-ro") == 0
    assert inj.load_injected("sess-ro") == {"a", "b"}


def test_wakeup_timeout_stays_far_under_the_hooks_own_ceiling():
    """A sleeping palace host must not eat the SessionStart hook's budget.

    familiar is Slumber-Ward sleepable and a sleeping host blackholes the SYN
    instead of refusing it, so ``connect()`` blocks for the whole timeout —
    measured 4,074 ms, which was 81% of the hook's 5 s ceiling for a call that
    answers in 33-48 ms when the host is awake. The ceiling is sized for the
    sleeping case, not the happy one.
    """
    assert cr.WAKEUP_TIMEOUT_S <= 2, "a sleeping host would eat the hook's budget"
    # …and still leaves ~30x headroom over the observed 48 ms round trip.
    assert cr.WAKEUP_TIMEOUT_S >= 0.5
