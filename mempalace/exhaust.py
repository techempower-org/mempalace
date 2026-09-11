"""One definition of "this drawer is the palace's own exhaust, not knowledge".

Every retrieval path needs the same answer, and until now three of them
answered separately: ``auto_query.runner`` had a room set and a prefix tuple,
``layers.Layer1`` had its own copy of both, and nothing anywhere knew about a
diff hunk. A predicate that is duplicated is a predicate that drifts, and the
drift showed up exactly where it hurt most — see below.

WHAT COUNTS AS EXHAUST, and why each shape is here (all measured on the live
palace, wings ``memorypalace`` and ``2g``, 2026-09-11):

* **Bookkeeping rooms and diary ids** — ``AUTO-SAVE:…`` checkpoints, session
  manifests, compaction summaries. The original finding (#421/#423): L1 led
  with them and agents read the palace as their own exhaust.

* **Harness prompt-echo** — the instruction text the harness mirrors back into
  the transcript. ``> you can check for comments``,
  ``> <command-message>exit</command-message>``,
  ``> <teammate-message teammate_id="team-lead" …``. It is *our* prose, filed
  as if it were the user's memory.

* **Diff scaffolding and diff bodies** — ``Changed files (you may Read these
  and any other file in the repo):``, ``Unified diff (only + lines are new):``,
  ``=== DIFF: tools/bringup-full.sh ===``, ``@@ -89,17 +89,27 @@ …``, and the
  ``+``-prefixed hunk bodies that follow. A patch fragment is never the
  essential story of a wing; the review *conclusion* is, and that survives.

* **Tool-call payloads** — ``{"success":true,"message":"Message sent to
  team-lead's inbox","msg_id":…,"routing":{…}}``. The receipt of an action, not
  the action's content.

WHY THIS MATTERS MORE SINCE #458: post-compaction recovery injects wake-up L1
as the *only* context an agent gets back. Before #458 a noisy L1 was an
annoyance you could scroll past; now it is the whole inheritance. Measured
before this change, ``memorypalace``'s L1 spent 9 of its 16 story lines on one
code review's diff — prompt, scaffolding, four hunk fragments and a truncated
byte-string — and ``2g``'s spent two on a SendMessage receipt and a quoted
teammate block.

THE BIAS IS DELIBERATE AND ONE-DIRECTIONAL: these predicates must never eat a
real finding. Every rule below is anchored on a shape the harness emits and a
human would not write — a diff header, a JSON receipt, a quoted harness tag —
rather than on topic, length or "looks technical". Where a rule could plausibly
catch human prose (a markdown blockquote, a fenced AT-command dump), it is
narrowed until it cannot.

⚠️ HOW TO CHECK A CHANGE TO THIS FILE, learned the hard way. The first audit ran
over 1,134 drawers — 0.67% of the two wings — and reported "zero findings lost".
A full-population pass over 168,390 drawers then found 19 that were lost, and
the sample had contained ZERO instances of the shape responsible, so no care in
reading its drop list could have surfaced them. A sample tells you a rule is
wrong; only the population tells you it is right. Audit the whole wing, and read
the drop list rather than the pass rate.
"""

import re

__all__ = [
    "EXHAUST_ROOMS",
    "EXHAUST_PREFIXES",
    "is_exhaust_text",
    "is_exhaust",
]

# Rooms that hold bookkeeping rather than knowledge.
EXHAUST_ROOMS = frozenset({"sessions", "diary", "checkpoint"})

# Whole-drawer opening lines that are never knowledge.
EXHAUST_PREFIXES = (
    "AUTO-SAVE:",
    "Session manifest",
    "> This session is being continued",
    # Prompt-echo — the harness's own instruction text mirrored back
    # (fleet finding, gnome-speaks-46, 2026-09-03).
    "Investigate per the method",
    "Get started. Read",
    "You are working ",
    "You are triaging ",
    # Diff scaffolding, verbatim from live drawers in both wings (#461).
    "Changed files (you may Read",
    "Unified diff (only",
    "=== DIFF:",
)

# Harness tags that mark a block as *not the user's words*. Mirrors
# auto_query.signals._FOREIGN_BLOCK_RE, which strips these from a prompt before
# signal extraction; here they mark a whole drawer as echo.
_HARNESS_TAG_RE = re.compile(
    r"<(cross-session-message|teammate-message|system-reminder|local-command-caveat"
    r"|command-name|command-message|command-args|local-command-stdout|task-notification"
    r"|bash-input|bash-stdout|bash-stderr)\b",
    re.IGNORECASE,
)

# A unified-diff hunk header: "@@ -89,17 +89,27 @@ …". Anchored at the start of
# the drawer, so a finding that *quotes* a hunk mid-paragraph is untouched.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")

# A tool-call receipt. Matched as a substring because these drawers often open
# with a transcript arrow ("→ {"success":true,…").
_TOOL_PAYLOAD_RE = re.compile(r'\{"success":\s*(?:true|false),\s*"message":\s*"Message sent to')

# An echoed one-line prompt ("> you can check for comments", "> isit com1?").
#
# ⚠️ THIS IS THE RULE THAT ALMOST ATE THE KNOWLEDGE. A first version dropped
# ANY one-line quote under 200 chars, and on the live corpus that removed eight
# real findings — JP's house style states a distilled lesson AS a blockquote:
#     > **"An absence of rejection at layer N is evidence about its inputs,
#     >   never its logic."**
#     > **A DORMANT DETECTOR AND AN ABSENT FAULT PRODUCE THE SAME SILENCE.**
# The separator that actually distinguishes the two classes is FORMATTING, not
# length: a distilled finding carries markdown emphasis (**bold**, ### heading,
# `code`, *"quote"*), and harness prompt-echo is plain, often lowercase and
# typo-ridden ("> why do you say those endpoitns aren't available?"). Verified
# on 1,134 live drawers with both classes present: 0 emphasized lines dropped,
# 7 plain echoes dropped.
#
# It is a heuristic and it is the weakest rule in this file, so it is bounded
# hard: one line only, short, and plain. Its worst case costs L1 a single
# plain-prose blockquote; the alternative costs seven echoed prompts, and one
# of them was the FIRST line of memorypalace's story.
_ONE_LINE_QUOTE_MAX = 200
_EMPHASIS_RE = re.compile(r"\*\*|###|`|\*\"")

# Harness echoes that are not tags and not quotes.
_ECHO_PREFIXES = (
    "> Another Claude session sent a message:",
    "Another Claude session sent a message:",
)

# How many added-source lines OUTSIDE ANY FENCED BLOCK make a drawer a patch
# body rather than a document that contains one.
#
# ⚠️ TWO THINGS THIS RULE GOT WRONG, BOTH FOUND BY AUDIT RATHER THAN BY READING.
#
# 1. COUNT "+" ONLY, NEVER "-". A first version counted both, and every markdown
#    bullet list starts with "- ", so a CLAUDE.md chunk ("- Use HTTP API
#    endpoints for realm operations…") read as a patch. Removed lines are
#    evidence of a diff only alongside a hunk header, which _HUNK_RE catches on
#    its own.
#
# 2. IGNORE "+" INSIDE ``` FENCES. A full-population audit (168,390 drawers)
#    found 19 real findings dropped, every one a fenced block in which "+" is
#    not a diff marker — AT-command responses (``+CREG: 0,1``, ``+CSQ: 27,99``)
#    and struct offsets (``+0x40 (64) LAC``), which are unavoidable in the 2g
#    wing's reverse-engineering work. Fencing is what separates the two classes:
#    a patch body arrives RAW from the harness, unfenced and usually under a
#    "=== DIFF:" banner, while a human pastes machine output INSIDE a fence.
#
#    ⛔ Do NOT "improve" this into a "+"-ratio threshold. It was tested: a ratio
#    rescues 191 drawers, including genuine diff hunks — it buys the 19 back by
#    giving up the rule. Fencing rescues exactly the 19, with zero collateral.
#
#    A fence does NOT launder an announced diff: _HUNK_RE and the "=== DIFF:"
#    prefix are checked before this and are unaffected by fencing.
_DIFF_BODY_MIN_LINES = 3
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _looks_like_diff_body(lines):
    # type: (list) -> bool
    """True when at least three UNFENCED lines are added source (a patch body).

    Deliberately a count, not a proportion — see _DIFF_BODY_MIN_LINES. The
    threshold is on unfenced "+" lines only, so pasted protocol output in a
    code block never reaches it however long it runs.
    """
    marked = 0
    in_fence = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            marked += 1
            if marked >= _DIFF_BODY_MIN_LINES:
                return True
    return False


def is_exhaust_text(text):
    # type: (str) -> bool
    """True when the drawer's CONTENT is bookkeeping, echo or a patch fragment."""
    raw = (text or "").lstrip()
    if not raw:
        return False
    if raw.startswith(EXHAUST_PREFIXES) or raw.startswith(_ECHO_PREFIXES):
        return True

    lines = raw.splitlines()
    first = lines[0] if lines else ""

    if _HUNK_RE.match(first):
        return True
    if _TOOL_PAYLOAD_RE.search(raw):
        return True

    if first.startswith(">"):
        # A quoted harness tag is echo, however long it runs…
        if _HARNESS_TAG_RE.search(first):
            return True
        # …and so is a drawer that is nothing but one short, UNFORMATTED quoted
        # line. The emphasis check is what keeps a distilled finding (which is
        # also a one-line blockquote) — see _EMPHASIS_RE above.
        if len(lines) == 1 and len(first) <= _ONE_LINE_QUOTE_MAX and not _EMPHASIS_RE.search(first):
            return True

    return _looks_like_diff_body(lines)


def is_exhaust(text, room="", drawer_id=""):
    # type: (str, str, str) -> bool
    """Full predicate: bookkeeping room, diary id, or an exhaust-shaped body."""
    if str(room or "") in EXHAUST_ROOMS:
        return True
    if str(drawer_id or "").startswith("diary_"):
        return True
    return is_exhaust_text(text)
