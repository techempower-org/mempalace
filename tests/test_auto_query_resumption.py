"""Session-resumption signal for auto-query (techempower-org/mempalace#364).

Measured on origin/main, 2026-09-10, turn 5, wing ``memorypalace`` with
recent drawers — every way a person asks to be picked back up scored zero
and fired nothing::

    where were we                          0   no
    what were we doing                     0   no
    pick up where we left off              0   no
    what's the team status                 0   no
    catch me up                            0   no
    what was I doing                       0   no

``_check_resumption`` was purely positional (turn 1 + known wing + recent
drawers), so a person asking mid-session — the case that matters after a
compaction — carried no signal at all. Four of those six were also dropped
by the hook's shell pre-filter before Python ever ran, so the signal and the
pre-filter have to move together.
"""

import os
import re
import subprocess

import pytest

from mempalace.auto_query import SessionState
from mempalace.auto_query.router import pick_tool
from mempalace.auto_query.signals import extract_signals, resumption_phrase

WINGS = {"memorypalace", "2g", "ha"}

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "hooks", "palace-auto-query.sh")
)

RESUMPTION_PROMPTS = [
    "where were we",
    "where were we?",
    "where was I",
    "what were we doing",
    "what were we working on",
    "pick up where we left off",
    "what's the team status",
    "what is the project status",
    "which PRs are open",
    "which issues are still open",
    "catch me up",
    "give me a recap",
    "bring me up to speed",
    "refresh my memory",
    "resume the work",
    "what's left to do",
    "what was I doing",
]

NOT_RESUMPTION_PROMPTS = [
    "continue",
    "status",
    "ok",
    "run the tests please",
    "we were talking about the weather",
    "open the file",
    "left the door open",
]


def _signals(text, turn=5, wing="memorypalace"):
    st = SessionState(turn_index=turn, queried_entities=set(), session_id="s")
    return (
        extract_signals(
            text=text,
            session_state=st,
            project_wing=wing,
            known_wings=WINGS,
            known_entities=None,
            has_recent_drawers=True,
        ),
        st,
    )


# --- the signal -------------------------------------------------------------


@pytest.mark.parametrize("prompt", RESUMPTION_PROMPTS)
def test_resumption_phrases_now_fire_mid_session(prompt):
    sig, st = _signals(prompt)
    assert sig.resumption, prompt
    assert sig.resumption_phrase, prompt
    assert sig.total_score >= 4, prompt
    assert pick_tool(sig, "balanced", st) is not None, prompt


@pytest.mark.parametrize("prompt", NOT_RESUMPTION_PROMPTS)
def test_ordinary_turns_are_not_resumption(prompt):
    sig, _ = _signals(prompt)
    assert not sig.resumption_phrase, prompt


def test_positional_turn_one_resumption_is_unchanged():
    """Turn 1 in a wing with recent drawers still resumes without a phrase."""
    sig, st = _signals("fix the mine please", turn=1)
    assert sig.resumption
    assert sig.resumption_phrase == ""
    assert pick_tool(sig, "balanced", st).tool == "mempalace_diary_read"


def test_a_peer_agents_message_does_not_resume():
    text = (
        "run the tests\n"
        '<cross-session-message from="x" from-name="peer">where were we</cross-session-message>'
    )
    sig, _ = _signals(text)
    assert not sig.resumption_phrase


def test_resumption_phrase_helper_is_exact():
    assert resumption_phrase("so, where were we on the cutover?") == "where were we"
    assert resumption_phrase("nothing to resume here") == ""


# --- the route --------------------------------------------------------------


def test_bare_resumption_searches_the_wings_decision_shaped_content():
    """ "where were we" is not a query. Ask the wing what it decided instead."""
    sig, st = _signals("where were we?")
    call = pick_tool(sig, "balanced", st)
    assert call.tool == "mempalace_search"
    assert call.args["wing"] == "memorypalace"
    assert "where were we" not in call.args["query"]
    assert "decisions" in call.args["query"]


def test_a_resumption_that_names_a_topic_searches_that_topic():
    sig, st = _signals("where were we on the pgvector cutover and the hnsw index?")
    call = pick_tool(sig, "balanced", st)
    assert call.tool == "mempalace_search"
    assert "pgvector cutover" in call.args["query"]
    assert "where were we" not in call.args["query"]


@pytest.mark.parametrize(
    "prompt,topic",
    [
        ("catch me up on 2g", "2g"),
        ("where were we on pgvector", "pgvector"),
        ("what's the status of #449", "#449"),
        ("bring me up to speed on searcher.py", "searcher.py"),
    ],
)
def test_a_one_word_topic_is_a_topic(prompt, topic):
    """The names people resume on are single tokens — a wing, an issue, a file.

    Requiring two surviving words discarded exactly those and sent the turn
    to the generic depth query instead of to the thing the user just named.
    """
    sig, st = _signals(prompt)
    call = pick_tool(sig, "balanced", st)
    assert call.args["query"] == topic
    assert "decisions problems findings" not in call.args["query"]


def test_an_empty_remainder_still_falls_back():
    """Only a resumption that names nothing at all takes the generic query."""
    for bare in ("where were we", "catch me up", "give me a recap", "resume the work"):
        sig, st = _signals(bare)
        assert "decisions problems findings" in pick_tool(sig, "balanced", st).args["query"], bare


def test_resumption_outranks_the_periodic_depth_refresh():
    """On a depth turn the explicit ask wins — it is the better query."""
    sig, st = _signals("catch me up on the daemon deploy", turn=10)
    assert sig.depth_fire
    call = pick_tool(sig, "balanced", st)
    assert "catch me up" not in call.args["query"]
    assert "daemon deploy" in call.args["query"]


def test_resumption_over_fetches_so_the_quality_gate_has_room():
    from mempalace.auto_query.router import DEPTH_FETCH

    sig, st = _signals("where were we?")
    assert pick_tool(sig, "balanced", st).args["limit"] >= DEPTH_FETCH


# --- the shell pre-filter has to let them through ---------------------------


def _prefilter_patterns():
    """The three grep -E patterns the hook uses as its pre-filter."""
    src = open(HOOK, encoding="utf-8").read()
    pats = re.findall(r"grep -q(i?)E '([^']+)'", src)
    assert pats, "hook pre-filter patterns not found"
    # Keep each pattern's own case sensitivity: only the recall pattern is
    # -i, and folding case would make "[A-Z][a-zA-Z]{2,}" match anything.
    return [("-qiE" if flag else "-qE", pat) for flag, pat in pats]


@pytest.mark.skipif(
    not os.path.exists(HOOK) or subprocess.run(["which", "grep"]).returncode != 0,
    reason="needs the hook and grep",
)
@pytest.mark.parametrize("prompt", RESUMPTION_PROMPTS)
def test_hook_prefilter_passes_every_resumption_prompt(prompt):
    """The shell filter must be a SUPERSET of the Python signal set.

    Four of these were dropped before Python ran, so the Python-side signal
    alone would never have fired in production.
    """
    hit = False
    for flag, pat in _prefilter_patterns():
        r = subprocess.run(["grep", flag, pat], input=prompt, text=True, capture_output=True)
        if r.returncode == 0:
            hit = True
            break
    assert hit, "hook pre-filter drops {!r} before Python runs".format(prompt)


def test_decision_log_records_why_the_resumption_fired():
    from mempalace.auto_query.runner import _serialize_signals

    sig, _ = _signals("catch me up on the daemon deploy")
    assert _serialize_signals(sig)["resumption_phrase"] == "catch me up"
    positional, _ = _signals("fix the mine please", turn=1)
    assert _serialize_signals(positional)["resumption_phrase"] == ""
