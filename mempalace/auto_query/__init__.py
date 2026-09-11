"""Auto-query integration for MemPalace.

A context classifier that auto-invokes MemPalace queries during Claude Code
or OpenCode sessions.  It detects entity mentions, temporal references, and
task-resumption patterns in the user's turn and maps them to the appropriate
MCP tool call (mempalace_search, mempalace_kg_query, mempalace_diary_read,
mempalace_traverse).

The classifier is conservative by default: it ships disabled, supports a
dry-run mode, and every decision is logged for offline tuning.

Shared dataclasses live here so all sub-modules import from one place.
"""

import dataclasses

__all__ = [
    "Signal",
    "SignalSet",
    "MCPCall",
    "SessionState",
    "Decision",
]


@dataclasses.dataclass
class Signal:
    """A single signal extracted from the user's turn."""

    kind: str  # "entity", "identifier", "temporal", "resumption", "explicit"
    name: str
    score: int
    wing: str = ""
    phrase: str = ""


@dataclasses.dataclass
class SignalSet:
    """Aggregated signals from a single turn."""

    entity: list  # list[Signal]
    temporal: list  # list[Signal]
    resumption: bool
    explicit: bool
    total_score: int
    project_wing: str = ""
    query_text: str = ""
    depth_fire: bool = False  # periodic depth refresh fired this turn
    # Identifier-shaped tokens (ALLCAPS, snake_case, camelCase, hex, dotted,
    # alnum codes) — the strings that appear in logs and findings and nowhere
    # else. Queried with lexical (BM25 union) retrieval, where exact match pays.
    identifier: list = dataclasses.field(default_factory=list)  # list[Signal]
    # The literal resumption phrase this turn matched ("where were we",
    # "catch me up"), empty when the resumption is positional (turn 1) or
    # absent. The router subtracts it from the search query (#364).
    resumption_phrase: str = ""


@dataclasses.dataclass
class MCPCall:
    """An MCP tool invocation to be issued."""

    tool: str
    args: dict


@dataclasses.dataclass
class SessionState:
    """Mutable per-session state carried across turns."""

    turn_index: int
    queried_entities: set  # entities already auto-queried this session
    session_id: str


@dataclasses.dataclass
class Decision:
    """A single auto-query decision, written to the JSONL log."""

    ts: str
    session_id: str
    turn: int
    signals: dict  # serialized SignalSet
    score: int
    threshold: int
    mode: str
    decision: str  # "fire", "skip", "dry-run-skip"
    reason: str
    tool: str = ""
    args: dict = dataclasses.field(default_factory=dict)
    latency_ms: int = 0
    result_drawers: int = 0
    injection_tokens: int = 0
