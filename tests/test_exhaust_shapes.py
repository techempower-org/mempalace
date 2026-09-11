"""Wake-up L1 must not lead with the harness's own exhaust (#461).

Every string below is VERBATIM from a live drawer in the production palace
(wings ``memorypalace`` and ``2g``, read-only listing, 2026-09-11). Nothing here
is invented, because the shapes that matter are the ones the harness actually
emits, and a filter tuned against imagined text is a filter tuned against
nothing.

MEASURED BEFORE: ``memorypalace``'s L1 spent 9 of its 16 story lines on a single
code review — the prompt, two scaffolding lines, four diff-hunk fragments and a
truncated byte-string — and ``2g``'s spent two on a SendMessage receipt and a
quoted teammate block. #436 filtered diary/manifest/compaction shapes; none of
these are those.

WHY THE POSITIVE CONTROLS ARE THE POINT OF THIS FILE. A first version of the
filter dropped 8 real findings and a CLAUDE.md chunk, because:

  * JP's house style states a distilled lesson AS a one-line blockquote
    (``> **A DORMANT DETECTOR AND AN ABSENT FAULT PRODUCE THE SAME SILENCE.**``),
    which is structurally identical to an echoed one-line prompt; and
  * every markdown bullet list starts with ``- ``, which a diff-body detector
    that counts removed lines reads as a patch.

Both were caught only by running the predicate over all 1,134 live drawers and
reading every single drop. The controls below are those exact survivors.
"""

import pytest

from mempalace.exhaust import is_exhaust, is_exhaust_text

# ── VERBATIM EXHAUST — live drawer text that must be dropped ────────────────

PROMPT_ECHO = [
    "> you can check for comments",
    "> skip",
    "> isit com1?",
    "> which pricess are we waiting on?",
    "> why do you say those endpoitns aren't available?",
    "> sim card is in the slot tell me about the enewest sim write for the iphone 5",
    "> Review this change for security vulnerabilities.",
    "> # /loop — schedule a recurring or self-paced prompt",
]

HARNESS_BLOCKS = [
    "> <command-message>exit</command-message>",
    '> <teammate-message teammate_id="team-lead" summary="Morpheus: port kg_canonical_* for #281">',
    '> <teammate-message teammate_id="aurora-3" color="pink">',
    "> Another Claude session sent a message:",
]

DIFF_SCAFFOLDING = [
    "Changed files (you may Read these and any other file in the repo):",
    "Unified diff (only + lines are new):",
    "=== DIFF: tools/bringup-full.sh ===",
    "@@ -89,17 +89,27 @@ dmi 'get 1684' | grep -q -- '= -1' && echo \"  *** FAIL\"",
    '@@ -38,23 +38,41 @@ if [ "${1:-}" = "--p2k" ] && [ "$pid" != "6401" ]; then',
]

DIFF_BODIES = [
    # A chunk that starts mid-token and continues into the hunk body — the
    # shape that produced four of memorypalace's 16 L1 lines.
    "t in range(0, min(64, len(blob))):\n+    recs, end = try_parse(start)\n"
    "+    if len(recs) > len(best):\n+        best, best_start = recs, start",
    '16,\n+6\n+],\n+"1334": [\n+"dhcpDnsServers",\n+6,',
]

TOOL_PAYLOADS = [
    '→ {"success":true,"message":"Message sent to team-lead\'s inbox",'
    '"msg_id":"a1dcbc14-490e-4a67-bb09-efd9fee441cc","routing":{"sender":"luna-cli-drawers"}}',
    "me/jp/tts-recordings/tts_20260528-084229.mp3 (18.7s, 195477 bytes) → "
    '{"success":true,"message":"Message sent to team-lead\'s inbox","routing":{"sender":"nebula"}}',
]

# ── VERBATIM POSITIVE CONTROLS — live drawer text that must SURVIVE ─────────
# The eight blockquoted findings the first filter ate, plus prose shapes.

REAL_FINDINGS = [
    '> **"An absence of rejection at layer N is evidence about its inputs, never its logic."**',
    "> **A DORMANT DETECTOR AND AN ABSENT FAULT PRODUCE THE SAME SILENCE.**",
    "> ### **GPS is not what stops us today. It is what we hit NEXT.**",
    "> **`show hnb` = 2 is TRUE while the condition it stood for is FALSE.**",
    '> *"`ls /var/ipaccess/cores/applications/` names every app that actually died."*',
    '> ⇒ ***"The config is in a file"* and *"the config is READ FROM that file"* '
    "are different claims — and only the second is a persistence answer.**",
    "> - Surface-normalize each raw via `normalize_predicate` upfront; drop the `None`s",
    "> **\"My first instinct was 'hourly cron'. Then I computed the base rate.\"**",
    "**Why:** On 2026-05-07 in memorypalace I committed three commits to `main` locally, "
    'then on JP\'s "go ahead na push" pushed them directly to `origin/main`.',
    "- **Overall:** 38.3% (115/300) across 1,000 sessions, 625 facts, 10 simulated months",
    "**Step 5**: Port the test files. Tests live outside the package.",
    # A markdown bullet list — three "-" lines, which a naive diff detector eats.
    "S custom properties for theme switching.\n"
    "- Use HTTP API endpoints for realm operations, not raw bash (map_server.py :80)\n"
    "- Token-conscious: keep searches targeted\n"
    "- Always backup before destructive infra ops",
]


@pytest.mark.parametrize("text", PROMPT_ECHO)
def test_prompt_echo_is_exhaust(text):
    assert is_exhaust_text(text), text


@pytest.mark.parametrize("text", HARNESS_BLOCKS)
def test_quoted_harness_block_is_exhaust(text):
    assert is_exhaust_text(text), text


@pytest.mark.parametrize("text", DIFF_SCAFFOLDING)
def test_diff_scaffolding_is_exhaust(text):
    assert is_exhaust_text(text), text


@pytest.mark.parametrize("text", DIFF_BODIES)
def test_diff_body_is_exhaust(text):
    assert is_exhaust_text(text), text


@pytest.mark.parametrize("text", TOOL_PAYLOADS)
def test_tool_call_receipt_is_exhaust(text):
    assert is_exhaust_text(text), text


# ── the class a 1,134-drawer sample could not see ───────────────────────────
# A full-population audit (168,390 drawers) found 19 real findings dropped, and
# EVERY one was a fenced code block in which "+" is not a diff marker: AT-command
# responses and struct offsets, which are unavoidable in the 2g wing's
# reverse-engineering work. My sample contained ZERO instances of any of these
# shapes, so no amount of care in reading its drop list could have caught them.
FENCED_NOT_DIFF = [
    '### Registration check\n\n```\nAT+CREG?\n+CREG: 0,1\n+CSQ: 27,99\n+COPS: 0,0,"Test"\n```\n\n'
    "Confirms the modem attached before the bearer came up.",
    "### SIB1 layout\n\n```\n+0x40 (64) LAC\n+0x42 (66) CI\n+0x44 (68) RAC\n```\n\n"
    "Offsets are from the start of the record, not the file.",
    "The responses we care about:\n\n```text\n+CME ERROR: 10\n+CPIN: SIM PIN\n+CFUN: 1\n```",
]


@pytest.mark.parametrize("text", REAL_FINDINGS)
def test_a_real_finding_always_survives(text):
    """The one-directional bias: this filter may under-drop, never over-drop."""
    assert not is_exhaust_text(text), text


@pytest.mark.parametrize("text", FENCED_NOT_DIFF)
def test_plus_inside_a_fence_is_not_a_diff_marker(text):
    """Inside ``` a leading "+" is protocol output, not a patch.

    This is the rule's real boundary: a diff body in this corpus arrives raw
    from the harness (unfenced, usually under a "=== DIFF:" header), while a
    human pastes machine output INSIDE a fence. Fencing is therefore the signal
    that separates them — and unlike a "+"-ratio threshold (tested: rescues 191
    drawers, including genuine diff hunks) it rescues exactly the 19 and
    nothing else.
    """
    assert not is_exhaust_text(text), text


def test_a_fenced_diff_still_counts_when_it_is_announced():
    """Fencing hides "+" lines; it does not hide a hunk header or a DIFF banner."""
    assert is_exhaust_text("=== DIFF: a.py ===\n```\n+one\n+two\n+three\n```")
    assert is_exhaust_text("@@ -1,2 +1,9 @@ def f():\n```\n+x\n```")


def test_an_emphasised_one_line_quote_is_never_echo():
    """The separator is FORMATTING, not length — the bug the corpus audit found.

    Both are one short quoted line. Only one of them is the harness talking.
    """
    assert is_exhaust_text("> isit com1?")
    assert not is_exhaust_text(
        "> **`show hnb` = 2 is TRUE while the condition it stood for is FALSE.**"
    )


def test_removed_lines_alone_are_not_a_diff():
    """Markdown bullets start with '-'. Counting them as a patch ate CLAUDE.md."""
    bullets = "- first point\n- second point\n- third point\n- fourth point"
    assert not is_exhaust_text(bullets)
    added = "+ first\n+ second\n+ third"
    assert is_exhaust_text(added)


def test_a_finding_that_quotes_a_hunk_mid_paragraph_survives():
    """_HUNK_RE is anchored: describing a diff is not being one."""
    text = (
        "The regression is in the hunk at `@@ -89,17 +89,27 @@` — the guard moved "
        "above the early return, so the check never runs."
    )
    assert not is_exhaust_text(text)


def test_room_and_drawer_id_rules_still_apply():
    assert is_exhaust("anything at all", room="diary")
    assert is_exhaust("anything at all", drawer_id="diary_2g_2026")
    assert not is_exhaust("a real decision", room="decisions", drawer_id="drawer_x")


def test_l1_and_auto_query_cannot_disagree():
    """One vocabulary, two consumers — the drift this change exists to end.

    L1's copy never learned the prompt-echo prefixes that auto-query grew in
    #445, and neither ever learned about diff fragments (#461).
    """
    from mempalace.auto_query.runner import _is_exhaust
    from mempalace.layers import _is_exhaust_drawer

    for text in PROMPT_ECHO + DIFF_SCAFFOLDING + TOOL_PAYLOADS:
        assert _is_exhaust({"text": text, "room": "problems"}), text
        assert _is_exhaust_drawer(text, {"room": "problems"}), text
    for text in REAL_FINDINGS:
        assert not _is_exhaust({"text": text, "room": "problems"}), text
        assert not _is_exhaust_drawer(text, {"room": "problems"}), text


# ── the budget: filtering harder must REFILL L1, not shrink it ──────────────


def _mock_col(docs, metas):
    """One page of results at offset 0, empty after — mirrors test_layers.py."""
    from unittest.mock import MagicMock

    col = MagicMock()
    data = {"documents": docs, "metadatas": metas}
    empty = {"documents": [], "metadatas": []}
    col.get.side_effect = lambda **kw: data if kw.get("offset", 0) == 0 else empty
    return col


def _render_l1(docs, metas):
    from unittest.mock import patch

    from mempalace.layers import Layer1

    with (
        patch("mempalace.layers.MempalaceConfig") as cfg,
        patch("mempalace.layers._get_collection", return_value=_mock_col(docs, metas)),
    ):
        cfg.return_value.palace_path = "/fake"
        return Layer1(palace_path="/fake").generate()


def test_dropping_exhaust_refills_l1_instead_of_shrinking_it():
    """The budget is MAX_DRAWERS of STORY, not of whatever came back first.

    Filtering happens before the top-N cut, so a stricter filter pulls deeper
    into the pool rather than leaving L1 short. If that order ever inverts,
    this change would quietly halve the only context a compacted agent gets
    (#458), which is the opposite of its purpose.
    """
    from mempalace.layers import Layer1

    n = Layer1.MAX_DRAWERS
    # Exhaust sorts FIRST by recency, so a naive "take N then filter" would
    # return almost nothing.
    docs, metas = [], []
    for i in range(n):
        docs.append("=== DIFF: tools/f%d.sh ===" % i)
        metas.append({"room": "problems", "filed_at": "2026-09-11T23:%02d:00" % i})
    for i in range(n + 5):
        docs.append("**Finding %d:** the binding alone is the fix, measured on card #11." % i)
        metas.append({"room": "problems", "filed_at": "2026-09-10T10:%02d:00" % i})

    out = _render_l1(docs, metas)
    assert "=== DIFF:" not in out
    kept = out.count("**Finding ")
    assert kept >= n, "L1 shrank to %d entries instead of refilling to %d" % (kept, n)


def test_l1_still_fits_its_character_budget():
    from mempalace.layers import Layer1

    docs = ["**Finding %d:** %s" % (i, "x" * 400) for i in range(40)]
    metas = [{"room": "problems", "filed_at": "2026-09-%02dT00:00:00" % (i + 1)} for i in range(40)]
    out = _render_l1(docs, metas)
    story = out.split("## L1 — ESSENTIAL STORY", 1)[-1]
    assert len(story) <= Layer1.MAX_CHARS + 400, len(story)
