"""Signal extraction for auto-query context classifier.

Pure functions — no I/O, no MCP calls. All external state
(wings, entities, drawer existence) is passed in by the caller.
"""

import re

from mempalace.auto_query import Signal, SignalSet


# Entity regex — same pattern as searcher.py:1191
_ENTITY_REGEX = re.compile(r"\b([A-Z][a-zA-Z0-9_]+(?:\s+[A-Z][a-zA-Z0-9_]+)*)\b")

# Temporal signal patterns (spec section 1.1.2)
_TEMPORAL_RE = re.compile(
    r"\b(last\s+(?:time|week|session|night|run|sprint)"
    r"|earlier\s+(?:today|this\s+week)"
    r"|yesterday"
    r"|that\s+time\s+we"
    r"|when\s+(?:did|we)\s+"
    r"|previously"
    r"|recently"
    r"|a\s+while\s+ago"
    r"|(?:a\s+)?few\s+days\s+ago"
    r"|back\s+when"
    r"|used\s+to"
    r"|before\s+we)\b",
    re.IGNORECASE,
)

# Explicit hint patterns (spec section 1.1.4)
_EXPLICIT_RE = re.compile(
    r"\b(remind\s+me"
    r"|(?:do\s+you\s+)?remember"
    r"|do\s+(?:we|you)\s+(?:have|know)"
    r"|did\s+(?:we|you)(?:\s+ever)?"
    r"|what\s+(?:did|was|were)\s+(?:we|the)"
    r"|history\s+of"
    r"|prior\s+to"
    r"|earlier\s+we"
    r"|have\s+we\s+ever"
    r"|recall"
    r"|was\s+there"
    r"|were\s+there"
    r"|check\s+(?:if|whether))\b",
    re.IGNORECASE,
)

# Session-resumption patterns (#364). "Where were we" is how a person asks to
# be picked back up — most often right after a compaction, which is exactly
# when the palace has something to say and the context does not. Measured on
# 2026-09-10 all of these scored 0 mid-session and fired nothing.
#
# Kept deliberately tight: bare "status", "continue" and "ok" are ordinary
# turns, not resumptions, and a signal that fires on them is noise.
_RESUMPTION_RE = re.compile(
    r"(where\s+(?:were|was)\s+(?:we|i|you)"
    r"|where\s+we\s+left\s+off"
    r"|pick(?:ing)?\s+up\s+where"
    r"|what\s+(?:were|was)\s+(?:we|i)\s+(?:doing|working\s+on|in\s+the\s+middle\s+of)"
    r"|what(?:'s|\s+is|\s+was)\s+the\s+(?:\w+\s+)?status\b"
    r"|which\s+(?:prs?|pull\s+requests|issues|tickets|branches|agents|lanes)"
    r"\s+(?:are|were)\s+(?:still\s+)?(?:open|running|left)"
    r"|catch\s+me\s+up"
    r"|bring\s+me\s+up\s+to\s+speed"
    r"|(?:give\s+me\s+a\s+|quick\s+)?recap\b"
    r"|refresh\s+(?:my|your)\s+memory"
    r"|resume\s+(?:the|our|this)\s+(?:work|session|task)"
    r"|what(?:'s|\s+is)\s+(?:still\s+)?(?:left|outstanding|remaining|in\s+flight)\b"
    r"|what\s+(?:still\s+)?(?:remains|is\s+pending))",
    re.IGNORECASE,
)


# Blocks that arrive inside the user-prompt payload but are NOT the user's
# words: cross-session / teammate messages, harness reminders, local-command
# echoes. Extracting entities from these fired the auto-query on a *peer's*
# prose ("Decline", "Write", "Reply" → 5 unrelated drawers) — fleet check-in,
# 2026-09-03. Strip them before any signal extraction.
_FOREIGN_BLOCK_RE = re.compile(
    r"<(cross-session-message|teammate-message|system-reminder|local-command-caveat"
    r"|command-name|command-message|command-args|local-command-stdout|task-notification"
    r"|bash-input|bash-stdout|bash-stderr)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def strip_foreign_blocks(text):
    # type: (str) -> str
    """Remove peer-message / harness blocks so only the user's words remain."""
    if not text or "<" not in text:
        return text or ""
    return _FOREIGN_BLOCK_RE.sub(" ", text)


# Identifier shapes — the retrievable knowledge in a debugging corpus is
# mostly identifiers (``EF_FPLMN``, ``NSAPI``, ``rejectCause``, ``CPE710``,
# ``com.apple.commcenter``, ``0x807``), which the capitalized-noun-phrase regex
# never matched (fleet check-in, 2026-09-03: "matching prose, and the
# retrievable knowledge in this project is all identifiers").
_IDENT_RES = (
    re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b"),  # EF_FPLMN, IP_VERSION_MISMATCH
    re.compile(r"\b[A-Z]{3,}[0-9]*\b"),  # NSAPI, CGDCONT, IMSI
    re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b"),  # rejectCause, sysvinit? (camelCase)
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-zA-Z0-9]+)+\b"),  # IuUP, PalaceDaemon (PascalCase)
    re.compile(r"\b0x[0-9a-fA-F]{2,}\b"),  # hex codes
    re.compile(r"\b[A-Za-z]+[0-9]+[A-Za-z0-9]*(?:[,.][0-9]+)?\b"),  # CPE710, iPhone2,1, ESP32
    re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"),  # snake_case: hook_silent_save
    re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*){2,}\b"),  # com.apple.commcenter
)

# ALLCAPS / code-shaped tokens that are harness or format vocabulary, not
# domain identifiers.
_IDENT_BLOCKLIST = frozenset(
    {
        "OK",
        "TODO",
        "FIXME",
        "NOTE",
        "IMPORTANT",
        "WARNING",
        "ERROR",
        "README",
        "JSON",
        "YAML",
        "HTML",
        "HTTP",
        "HTTPS",
        "URL",
        "URI",
        "API",
        "CLI",
        "MCP",
        "PATH",
        "HEAD",
        "TBD",
        "TLDR",
        "UTC",
        "PDT",
        "PST",
        "EST",
        "GMT",
        "NULL",
        "TRUE",
        "FALSE",
        "NONE",
        "AND",
        "THE",
        "FOR",
        "NOT",
        "YES",
        "ALL",
        "ANY",
        "NEW",
        "OLD",
        "PDF",
        "PNG",
        "JPG",
        "SVG",
        "CSS",
        "SQL",
        "SSH",
        "USB",
        "RAM",
        "CPU",
        "GPU",
        "LAN",
        "WAN",
        "DNS",
        "IP",
        "EOF",
        "ASCII",
        "UTF8",
        "ISO",
        "GNOME",
        "LTS",
        "CI",
        "PR",
        "ID",
        "AM",
        "PM",
        "MD",
        "TXT",
        "LOG",
        "ENV",
        "STDOUT",
        "STDERR",
        "ASAP",
        "FYI",
        "ETA",
        "AKA",
        "IMO",
        "LOL",
        "CLAUDE",
        "MEMORY",
        "SUDO",
        "UNIX",
        "LINUX",
        "MAC",
        "WIN",
        "GIT",
        "PIP",
        "NPM",
    }
)
_MAX_IDENTIFIER_SIGNALS = 3
_MIN_IDENT_LEN = 3

# Generic-English / harness words that the capitalized-noun regex catches at
# sentence starts and in pasted tool output. Fleet-observed misfires:
# Goal, You, ONE, Hold, Decline, Write, Reply, Context, Full, Lint, Commit.
_GENERIC_CAPS = {
    "Goal",
    "You",
    "Your",
    "One",
    "Hold",
    "Decline",
    "Write",
    "Reply",
    "Context",
    "Full",
    "Lint",
    "Commit",
    "Fault",
    "Note",
    "Still",
    "Since",
    "After",
    "Before",
    "While",
    "Because",
    "Both",
    "Each",
    "Every",
    "Some",
    "Many",
    "Most",
    "Other",
    "Same",
    "Such",
    "Very",
    "Well",
    "Even",
    "Ever",
    "Never",
    "Always",
    "Maybe",
    "Perhaps",
    "Sure",
    "Okay",
    "Thanks",
    "Thank",
    "Hi",
    "Hey",
    "Hello",
    "Good",
    "Great",
    "Nice",
    "Right",
    "Left",
    "First",
    "Second",
    "Next",
    "Last",
    "Final",
    "New",
    "Old",
    "Read",
    "Edit",
    "Bash",
    "Update",
    "Check",
    "Fix",
    "Run",
    "Add",
    "Remove",
    "Make",
    "Use",
    "Try",
    "See",
    "Look",
    "Find",
    "Get",
    "Set",
    "Put",
    "Keep",
    "Stop",
    "Start",
    "Done",
    "Todo",
    "Summary",
    "Result",
    "Results",
    "Error",
    "Warning",
    "Output",
    "Input",
    "File",
    "Files",
    "Line",
    "Lines",
    "Code",
    "Test",
    "Tests",
    "Hook",
    "Hooks",
    "Skill",
    "Skills",
    "Plugin",
    "Session",
    "Sessions",
    "Claude",
    "Assistant",
    "User",
    "System",
    "Tool",
    "Tools",
    "Message",
    "Messages",
    "Ask",
    "Answer",
    "Question",
    "Status",
    "Report",
    "Plan",
    "Step",
    "Steps",
    "Part",
    "Item",
    "Items",
    "List",
    "Name",
    "Type",
    "Value",
    "Data",
    "Info",
    "Detail",
    "Details",
    "Example",
    "Issue",
    "Issues",
    "Problem",
    "Problems",
    "Fixed",
    "Merged",
    "Open",
    "Closed",
    "Ready",
    "Yes",
    "Nope",
    "Yep",
    "Cool",
    "Love",
    "Please",
    "Sorry",
    "Hmm",
    "Wow",
    "Honest",
    "Honestly",
    "Quick",
    "Fast",
    "Slow",
    "Big",
    "Small",
    "Long",
    "Short",
    "High",
    "Low",
    "More",
    "Less",
    "Much",
    "Little",
    "Few",
    "Several",
    "Something",
    "Anything",
    "Nothing",
    "Everything",
    "Someone",
    "Anyone",
    "Today",
    "Tomorrow",
    "Tonight",
    "Morning",
    "Evening",
    "Night",
    "Week",
    "Month",
    "Year",
    "Day",
    "Time",
    "Times",
}

# Stopwords — common capitalized words that are NOT entities
_STOPWORDS = {
    "The",
    "This",
    "That",
    "These",
    "Those",
    "What",
    "When",
    "Where",
    "Which",
    "Who",
    "How",
    "Why",
    "Yes",
    "No",
    "True",
    "False",
    "None",
    "And",
    "But",
    "Or",
    "Not",
    "Also",
    "Just",
    "Only",
    "Can",
    "Could",
    "Would",
    "Should",
    "Will",
    "May",
    "Must",
    "Let",
    "Here",
    "There",
    "Now",
    "Then",
    # Path / orchestration noise — capitalized non-entities common in JP's
    # prompts and pasted tool output (e.g. ~/Projects paths, "run in the
    # Background"). These previously mis-fired auto-query on pure noise.
    "Projects",
    "Background",
    "Wait",
    "Monitor",
    "Poll",
    "Watch",
    "Build",
    "Agent",
    "Agents",
    "Phase",
    "Please",
    "Ran",
    "Research",
    "Robust",
    "Task",
    "Clean",
}

# Alias map: JP-vocabulary → canonical wing. An alias only fires if its
# target wing is actually present in known_wings.
_WING_ALIASES = {
    "mempalace": "memorypalace",
    "memory palace": "memorypalace",
}

# Common English words that are also wing names — never lowercase-match
# these, or they fire on ordinary prose ("in general", "the projects list").
_WING_MATCH_BLOCKLIST = frozenset(
    {
        "general",
        "projects",
        "sessions",
        "update",
        "test",
        "tools",
        "watch",
        "claude",
        "oracle",
    }
)

# Minimum length for a lowercase wing/alias surface form to be matchable.
# Below this, short names ("ha", "jp", "sdp") false-match inside words.
_MIN_LOWERCASE_WING_LEN = 5

# Max results per signal class
_MAX_ENTITY_SIGNALS = 5
_MAX_TEMPORAL_SIGNALS = 3


def extract_signals(
    text,  # type: str
    session_state,  # type: SessionState
    project_wing,  # type: str
    known_wings,  # type: set
    known_entities=None,  # type: Optional[set]
    has_recent_drawers=False,  # type: bool
):
    # type: (...) -> SignalSet
    """Extract auto-query signals from a user message.

    Pure function — no I/O, no MCP calls. All external state
    (wings, entities, drawer existence) is passed in by the caller.
    """
    # Only the user's own words count as signal — never a peer agent's.
    text = strip_foreign_blocks(text)

    entity_signals = _extract_entity_signals(text, session_state, known_wings, known_entities)
    identifier_signals = _extract_identifier_signals(text, session_state)
    temporal_signals = _extract_temporal_signals(text)
    phrase = resumption_phrase(text)
    resumption = _check_resumption(
        session_state, project_wing, known_wings, has_recent_drawers, phrase
    )
    explicit = _check_explicit(text)

    total = (
        sum(s.score for s in entity_signals)
        + sum(s.score for s in identifier_signals)
        + sum(s.score for s in temporal_signals)
    )
    if resumption:
        total += 4
    if explicit:
        total += 5

    # Compound bonus: entity + temporal together (spec section 1.1.2)
    if entity_signals and temporal_signals:
        total += 1

    # Periodic depth refresh — auto-fire regardless of message content to
    # counteract mid-context attention degradation ("lost in the middle").
    # Fires on turn 1 (a recall floor for the very short sessions that
    # dominate real usage — many are a single turn) and every 10th turn
    # thereafter. turn_index > 0 guards the turn-0 sentinel so it never fires
    # before the session has a real turn.
    depth_fire = session_state.turn_index > 0 and (
        session_state.turn_index == 1 or session_state.turn_index % 10 == 0
    )
    if depth_fire:
        total += 4  # enough to fire in balanced mode (threshold 4)

    return SignalSet(
        entity=entity_signals,
        temporal=temporal_signals,
        resumption=resumption,
        explicit=explicit,
        total_score=total,
        project_wing=project_wing,
        query_text=text,
        depth_fire=depth_fire,
        identifier=identifier_signals,
        resumption_phrase=phrase,
    )


def _extract_entity_signals(
    text,  # type: str
    session_state,  # type: SessionState
    known_wings,  # type: set
    known_entities,  # type: Optional[set]
):
    # type: (...) -> List[Signal]
    """Extract entity signals from capitalized noun phrases.

    Uses the same regex as searcher.py:1191 (_ENTITY_REGEX).
    Filters stopwords, deduplicates against session_state.queried_entities,
    and scores by wing/entity match.
    """
    signals = []  # type: List[Signal]
    seen = set()  # type: Set[str]

    # Pass 1: capitalized noun phrases via regex
    for m in _ENTITY_REGEX.finditer(text):
        token = m.group(1).strip()
        if len(token) < 3:
            continue
        if token in seen:
            continue
        # Stopword check — compare first word for multi-word phrases too
        first_word = token.split()[0]
        if first_word in _STOPWORDS or first_word in _GENERIC_CAPS:
            continue
        if token in session_state.queried_entities:
            continue
        seen.add(token)

        signal = _score_entity(token, known_wings, known_entities)
        if signal.score < 2:
            # Unknown token. Identifier-shaped ones belong to the identifier
            # pass (exact-match retrieval). A lone capitalized word at the
            # START of a sentence is how English sentences start, not a
            # remembered thing ("Goal set…", "Decline…", "Reply…" all fired
            # the palace fleet-wide, 2026-09-03). Mid-sentence single names
            # ("tell me about Alice") keep the weak-but-real score of 1.
            if _looks_like_identifier(token):
                continue
            if " " not in token and _sentence_initial(text, m.start()):
                continue
        if signal.wing:
            seen.add(signal.wing)
        signals.append(signal)

    # Pass 1.5: lowercase wing / alias match. JP types wing names in
    # lowercase ("candela", "mempalace", "familiar"), which the capital-only
    # regex above never sees. Match known wings and a small alias map against
    # the lowercased text at word boundaries, with a min length and a
    # common-word blocklist to keep precision high.
    q_lower = text.lower()
    for surface, wing in _wing_surface_forms(known_wings):
        if len(surface) < _MIN_LOWERCASE_WING_LEN:
            continue
        if surface in _WING_MATCH_BLOCKLIST:
            continue
        if wing in seen:
            continue
        if wing in session_state.queried_entities:
            continue
        if _word_present(surface, q_lower):
            seen.add(wing)
            signals.append(Signal(kind="entity", name=surface, score=3, wing=wing))

    # Pass 2: lowercase substring match against known entities
    # (same pattern as _ner_from_query in searcher.py:1218-1225)
    if known_entities:
        q_lower = text.lower()
        for ent in sorted(known_entities):  # sorted for determinism
            if ent and ent.lower() in q_lower and ent not in seen:
                if ent in session_state.queried_entities:
                    continue
                seen.add(ent)
                signal = _score_entity(ent, known_wings, known_entities)
                signals.append(signal)

    return signals[:_MAX_ENTITY_SIGNALS]


def _score_entity(
    name,  # type: str
    known_wings,  # type: set
    known_entities,  # type: Optional[set]
):
    # type: (...) -> Signal
    """Score an entity candidate against known wings and entities."""
    # Check wing match: try common slug patterns
    wing_slugs = _entity_to_wing_slugs(name)
    for slug in wing_slugs:
        if slug in known_wings:
            return Signal(kind="entity", name=name, score=3, wing=slug)

    # Check known entity match
    if known_entities and name in known_entities:
        return Signal(kind="entity", name=name, score=2)

    # Unknown entity — weak-but-real score 1. The caller decides whether a
    # lone sentence-initial word or an identifier-shaped token should be
    # dropped from the entity list (see _extract_entity_signals).
    return Signal(kind="entity", name=name, score=1)


_SENTENCE_BOUNDARY = ".!?:;\n\r\"'([{\u2014-"


def _sentence_initial(text, pos):
    # type: (str, int) -> bool
    """True when the token at ``pos`` opens the text or follows a sentence break."""
    i = pos - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0:
        return True
    return text[i] in _SENTENCE_BOUNDARY


def _looks_like_identifier(token):
    # type: (str) -> bool
    """True when a token has identifier shape (digits, underscores, humps, hex)."""
    if not token or len(token) < _MIN_IDENT_LEN:
        return False
    if token.upper() in _IDENT_BLOCKLIST:
        return False
    return any(r.fullmatch(token) for r in _IDENT_RES)


def _extract_identifier_signals(text, session_state):
    # type: (str, SessionState) -> List[Signal]
    """Extract identifier-shaped tokens — exact-match retrieval targets."""
    signals = []  # type: List[Signal]
    seen = set()  # type: Set[str]
    for rx in _IDENT_RES:
        for m in rx.finditer(text):
            tok = m.group(0)
            if len(tok) < _MIN_IDENT_LEN or tok in seen:
                continue
            if tok.upper() in _IDENT_BLOCKLIST:
                continue
            if tok in session_state.queried_entities:
                continue
            # A bare ALLCAPS English word ("FIXED", "MERGED") is not an id.
            if tok.isalpha() and tok.isupper() and tok.capitalize() in _GENERIC_CAPS:
                continue
            seen.add(tok)
            signals.append(Signal(kind="identifier", name=tok, score=2))
            if len(signals) >= _MAX_IDENTIFIER_SIGNALS:
                return signals
    return signals


def _entity_to_wing_slugs(name):
    # type: (str) -> List[str]
    """Generate candidate wing slugs from an entity name.

    Tries common patterns: wing_<lower>, wing_<lower_underscored>,
    and the raw lowercase name.
    """
    lower = name.lower()
    underscored = lower.replace(" ", "_").replace("-", "_")
    return [
        "wing_{}".format(underscored),
        "wing_{}".format(lower.replace(" ", "")),
        underscored,
        lower,
    ]


def _wing_surface_forms(known_wings):
    # type: (set) -> list
    """Yield (surface_form, canonical_wing) pairs for lowercase matching.

    For each wing: the lowercased wing name and an underscores-as-spaces
    form; plus any alias whose target wing is present in known_wings.
    """
    forms = []  # type: list
    for wing in known_wings:
        wl = wing.lower()
        forms.append((wl, wing))
        spaced = wl.replace("_", " ")
        if spaced != wl:
            forms.append((spaced, wing))
    for alias, wing in _WING_ALIASES.items():
        if wing in known_wings:
            forms.append((alias, wing))
    return forms


def _word_present(needle, haystack_lower):
    # type: (str, str) -> bool
    """True if needle appears in haystack at word boundaries (both lowercased)."""
    return re.search(r"\b{}\b".format(re.escape(needle)), haystack_lower) is not None


def _extract_temporal_signals(text):
    # type: (str) -> List[Signal]
    """Extract temporal signals from time-reference phrases.

    Uses _TEMPORAL_RE to match phrases like "last time", "yesterday",
    "when did we", etc. Returns up to _MAX_TEMPORAL_SIGNALS matches.
    """
    signals = []  # type: List[Signal]
    seen_phrases = set()  # type: Set[str]

    for m in _TEMPORAL_RE.finditer(text):
        phrase = m.group().strip()
        if phrase.lower() in seen_phrases:
            continue
        seen_phrases.add(phrase.lower())
        signals.append(Signal(kind="temporal", name=phrase, score=2, phrase=phrase))

    return signals[:_MAX_TEMPORAL_SIGNALS]


def resumption_phrase(text):
    # type: (str) -> str
    """The session-resumption phrase in ``text``, or "" if there is none.

    Public because the router needs to subtract the phrase from the search
    query: "where were we on the pgvector cutover" should search the cutover,
    not the question.
    """
    m = _RESUMPTION_RE.search(strip_foreign_blocks(text or ""))
    return m.group(0).strip() if m else ""


def _check_resumption(
    session_state,  # type: SessionState
    project_wing,  # type: str
    known_wings,  # type: set
    has_recent_drawers,  # type: bool
    phrase="",  # type: str
):
    # type: (...) -> bool
    """Check for task-resumption signal.

    Two ways to resume, and the second is the one #364 added:

    * *positionally* — turn 1 in a known wing that has recent drawers, i.e.
      the session is picking a project back up; or
    * *by asking* — the turn contains a resumption phrase ("where were we",
      "catch me up", "which PRs are open"). This is the case that matters
      after a compaction, and until #364 it carried no signal at all, on any
      turn.
    """
    if phrase:
        return True
    return session_state.turn_index == 1 and project_wing in known_wings and has_recent_drawers


def _check_explicit(text):
    # type: (str) -> bool
    """Check for explicit memory-request hints.

    Returns True if the text contains an explicit hint phrase
    AND a question mark — the user is effectively asking for
    memory recall without phrasing it as a tool call.
    """
    return bool(_EXPLICIT_RE.search(text)) and "?" in text
