"""Diary reads that do not need a configured identity (issue #501).

The hook's diary path never consults ``~/.mempalace/identity.txt`` or
``MEMPALACE_AGENT_NAME``. It passes ``agent_name=<harness>`` — hook.py's
``--harness`` flag, one of ``claude-code`` / ``codex`` / ``gemini-cli`` —
and ``tool_diary_write`` lowercases that into the ``agent`` metadata key.

Measured against the production daemon 2026-09-17 (read-only MCP calls):

    diary_read(agent_name="claude-code", wing="2g")  -> 3 AUTO-SAVE entries
    diary_read(agent_name="team-lead",   wing="2g")  -> 0, "No diary entries yet."
    diary_read(agent_name="2g-c6",       wing="2g")  -> 0, "No diary entries yet."

So the entries visible in ``list --wing 2g`` are keyed on a name the
reader has no way to guess — the defect in #501. Two capabilities remove
that: an agent-less read that keys on drawers, and a way to enumerate the
agent names actually present.
"""

import pytest


def _patch_mcp_server(monkeypatch, config, kg):
    """Patch the mcp_server module globals to use test fixtures."""
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)
    monkeypatch.setattr(mcp_server, "_taxonomy_cache", None)
    monkeypatch.setattr(mcp_server, "_taxonomy_cache_time", 0.0)
    from mempalace.palace_graph import invalidate_graph_cache

    invalidate_graph_cache()


def _get_collection(palace_path):
    import chromadb

    client = chromadb.PersistentClient(path=palace_path)
    return client, client.get_or_create_collection(
        "mempalace_drawers", metadata={"hnsw:space": "cosine"}
    )


@pytest.fixture
def diary_palace(monkeypatch, config, palace_path, kg):
    """A palace with diary entries from three agents across two wings.

    Mirrors the production shape: a harness-named writer (``claude-code``)
    the reader cannot guess, plus named agents, plus an unrelated wing.
    """
    _patch_mcp_server(monkeypatch, config, kg)
    client, _col = _get_collection(palace_path)
    del client
    from mempalace.mcp_server import tool_diary_write

    rows = [
        ("claude-code", "AUTO-SAVE:sess-a|1710.msgs", "hook", "2g"),
        ("claude-code", "AUTO-SAVE:sess-a|1711.msgs", "hook", "2g"),
        ("claude-code", "COMPACTION:sess-a", "hook", "2g"),
        ("team-lead", "wave 3 landing order", "planning", "2g"),
        ("morpheus", "elsewhere entirely", "general", "otherwing"),
    ]
    for agent, entry, topic, wing in rows:
        written = tool_diary_write(agent_name=agent, entry=entry, topic=topic, wing=wing)
        assert written["success"] is True, written
    return rows


class TestIdentitylessDiaryRead:
    def test_read_without_agent_lists_the_wings_diary_drawers(self, diary_palace):
        """#501(a): no agent_name → every agent's diary drawers in the wing."""
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(wing="2g")
        assert "error" not in r, r
        contents = {e["content"] for e in r["entries"]}
        assert "AUTO-SAVE:sess-a|1710.msgs" in contents
        assert "wave 3 landing order" in contents
        assert r["total"] == 4  # three claude-code + one team-lead, not morpheus

    def test_read_without_agent_tags_each_entry_with_its_writer(self, diary_palace):
        """Across agents the writer must be on every row — otherwise the
        listing cannot tell the reader which --agent to use next."""
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(wing="2g")
        assert all("agent" in e for e in r["entries"]), r["entries"]
        assert {e["agent"] for e in r["entries"]} == {"claude-code", "team-lead"}

    def test_read_without_agent_is_scoped_by_wing(self, diary_palace):
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(wing="otherwing")
        assert r["total"] == 1
        assert r["entries"][0]["content"] == "elsewhere entirely"

    def test_read_without_agent_or_wing_spans_every_wing(self, diary_palace):
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read()
        assert r["total"] == 5

    def test_read_without_agent_is_newest_first_and_honours_last_n(self, diary_palace):
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(wing="2g", last_n=2)
        assert len(r["entries"]) == 2
        stamps = [e["timestamp"] for e in r["entries"]]
        assert stamps == sorted(stamps, reverse=True)
        assert r["total"] == 4  # total counts matches, not the page

    def test_read_without_agent_reports_no_agent_scope(self, diary_palace):
        """``agent`` must not be invented when none was asked for."""
        from mempalace.mcp_server import tool_diary_read

        assert tool_diary_read(wing="2g").get("agent") is None

    def test_read_with_agent_is_unchanged(self, diary_palace):
        """Regression: the per-agent contract keeps working exactly as before."""
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(agent_name="claude-code", wing="2g")
        assert r["agent"] == "claude-code"
        assert r["total"] == 3
        assert all(e["content"].startswith(("AUTO-SAVE", "COMPACTION")) for e in r["entries"])

    def test_read_with_unknown_agent_still_reports_empty(self, diary_palace):
        """The #501 symptom itself — kept, because it is correct behaviour
        for an agent that genuinely wrote nothing."""
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(agent_name="2g-c6", wing="2g")
        assert r["entries"] == []
        assert "No diary entries" in r.get("message", "")

    def test_blank_agent_is_treated_as_absent_not_as_an_error(self, diary_palace):
        """A blank ``--agent ''`` / unset env var must not 'agent_name must be
        a non-empty string' — that is the confusing report in #501."""
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(agent_name="   ", wing="2g")
        assert "error" not in r, r
        assert r["total"] == 4


class TestDiaryAgents:
    def test_lists_agent_names_with_counts(self, diary_palace):
        """#501(b): enumerate who has written, so a reader can pick one."""
        from mempalace.mcp_server import tool_diary_agents

        r = tool_diary_agents()
        assert "error" not in r, r
        counts = {a["agent"]: a["entries"] for a in r["agents"]}
        assert counts == {"claude-code": 3, "team-lead": 1, "morpheus": 1}

    def test_sorted_by_count_descending(self, diary_palace):
        from mempalace.mcp_server import tool_diary_agents

        rows = tool_diary_agents()["agents"]
        assert [a["entries"] for a in rows] == sorted([a["entries"] for a in rows], reverse=True)
        assert rows[0]["agent"] == "claude-code"

    def test_scoped_to_one_wing(self, diary_palace):
        from mempalace.mcp_server import tool_diary_agents

        r = tool_diary_agents(wing="2g")
        assert {a["agent"] for a in r["agents"]} == {"claude-code", "team-lead"}
        assert r["wing"] == "2g"

    def test_carries_the_latest_timestamp_per_agent(self, diary_palace):
        """A reader picking an agent wants to know which one is still active.

        Asserted against the entries themselves rather than against a
        hand-written constant, so the row cannot drift from the data.
        """
        from mempalace.mcp_server import tool_diary_agents, tool_diary_read

        rows = {a["agent"]: a for a in tool_diary_agents(wing="2g")["agents"]}
        for name in ("claude-code", "team-lead"):
            expected = max(
                e["timestamp"] for e in tool_diary_read(agent_name=name, wing="2g")["entries"]
            )
            assert rows[name]["latest"] == expected

    def test_empty_palace_returns_an_empty_list_not_an_error(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        client, _col = _get_collection(palace_path)
        del client
        from mempalace.mcp_server import tool_diary_agents

        r = tool_diary_agents()
        assert r["agents"] == []
        assert r.get("truncated") is False

    def test_reports_truncation_rather_than_a_capped_count(self, diary_palace, monkeypatch):
        """A count capped by the scan limit is a LOWER BOUND, and the payload
        must say so. Reporting a capped number as the total is the
        report-disagrees-with-reality defect; the daemon also runs at its
        memory ceiling, so an unbounded scan is not the alternative."""
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_DIARY_SCAN_LIMIT", 2)
        r = mcp_server.tool_diary_agents()
        assert r["truncated"] is True
        assert r["scanned"] == 2

    def test_not_truncated_when_under_the_limit(self, diary_palace):
        from mempalace.mcp_server import tool_diary_agents

        r = tool_diary_agents()
        assert r["truncated"] is False
        assert r["scanned"] == 5


class TestIdentityBanner:
    """#501(c): the banner must say what the identity is FOR, and that a
    read does not need one.

    The reporter's session saw "No identity configured", concluded the
    diary was unavailable, and then could not read entries that were
    sitting in the wing. The banner was technically true and practically
    misleading — it named a missing file without naming a consequence.

    Only rendered when identity.txt is absent, so the added text costs
    nothing in the configured case.
    """

    def _banner(self, tmp_path):
        from mempalace.layers import Layer0

        return Layer0(identity_path=str(tmp_path / "nope.txt")).render()

    def test_keeps_the_existing_markers(self, tmp_path):
        text = self._banner(tmp_path)
        assert "No identity configured" in text
        assert "identity.txt" in text

    def test_says_what_the_identity_is_for(self, tmp_path):
        text = self._banner(tmp_path).lower()
        assert "write" in text

    def test_says_reads_do_not_need_one(self, tmp_path):
        """The actionable half — otherwise the banner reads as 'no diary'."""
        text = self._banner(tmp_path).lower()
        assert "read" in text
        assert "diary read" in text

    def test_points_at_the_discovery_command(self, tmp_path):
        text = self._banner(tmp_path)
        assert "diary agents" in text
