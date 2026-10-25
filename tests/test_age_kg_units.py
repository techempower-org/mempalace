"""Unit-level coverage for the AGE-KG modules — no postgres required.

The integration suite (``test_knowledge_graph_age.py``) covers the parts
that need a live AGE+postgres; this file targets the pure-function and
mock-friendly slices of ``knowledge_graph_age``, ``kg_writethrough``,
``backfill_age``, and ``palace_graph_age`` so the AGE-KG branch's fork
coverage stays above the CI floor (currently 79%).

These tests deliberately avoid any DSN-dependent code paths — only the
helpers that operate on Python values, regex patterns, environment
variables, or fully-mocked KG handles.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from unittest.mock import MagicMock

import pytest


# =============================================================================
# knowledge_graph_age — _cypher_literal + _inline_cypher_params
# =============================================================================


def test_cypher_literal_rejects_dq_tag_in_string():
    """Defense in depth: a value containing the outer dollar-quote tag
    would let an attacker close the SQL boundary. Reject at literal time."""
    from mempalace.knowledge_graph_age import _AGE_DQ_TAG, _cypher_literal

    with pytest.raises(ValueError, match="dollar-quote tag"):
        _cypher_literal(f"prefix{_AGE_DQ_TAG}suffix")


def test_cypher_literal_non_string_falls_through_json_dumps():
    """Dicts and other non-scalars round-trip via json.dumps and get string-quoted.

    Lists no longer do: a list renders as a Cypher list (see test_kg_cypher_list_literal.py). The old
    "'[1, 2, 3]'" string form is what made ``UNWIND $ids`` in delete_drawers match nothing.
    """
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal({"a": 1}) == "'{\"a\": 1}'"
    assert _cypher_literal([1, 2, 3]) == "[1, 2, 3]"


def test_inline_cypher_params_substitutes_named_placeholders():
    from mempalace.knowledge_graph_age import _inline_cypher_params

    cypher = "MATCH (n {name: $name, age: $age}) RETURN n"
    out = _inline_cypher_params(cypher, {"name": "Max", "age": 30})
    assert "'Max'" in out
    assert "30" in out
    assert "$name" not in out
    assert "$age" not in out


def test_inline_cypher_params_leaves_unknown_placeholders_alone():
    """Unknown $names pass through verbatim so AGE raises a clear parse error
    (instead of silently swallowing the literal text)."""
    from mempalace.knowledge_graph_age import _inline_cypher_params

    out = _inline_cypher_params("MATCH (n {x: $unknown}) RETURN n", {"y": 1})
    assert "$unknown" in out


def test_inline_cypher_params_handles_value_containing_dollar_sign():
    """Closes the recursive-replacement bug from the prior length-sorted
    str.replace impl (Gemini PR #101 review): a value containing
    ``$other_key`` is preserved verbatim, not re-substituted."""
    from mempalace.knowledge_graph_age import _inline_cypher_params

    cypher = "RETURN $a, $b"
    out = _inline_cypher_params(cypher, {"a": "contains $b literally", "b": "xyz"})
    # The 'a' substitution carries '$b' which the single-pass re.sub does NOT
    # re-enter; final string keeps it as text.
    assert "'contains $b literally'" in out
    assert "'xyz'" in out


def test_inline_cypher_params_no_placeholders_is_identity():
    from mempalace.knowledge_graph_age import _inline_cypher_params

    cypher = "MATCH (n) RETURN n"
    assert _inline_cypher_params(cypher, {"unused": 1}) == cypher


# =============================================================================
# KnowledgeGraphAGE — static/class helper methods (no postgres needed)
# =============================================================================


def test_entity_id_lowercases_and_replaces_spaces():
    """``_entity_id`` mirrors the SQLite KG's id derivation so cross-backend
    callers see the same id for the same entity name."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert KnowledgeGraphAGE._entity_id("Max") == "max"
    assert KnowledgeGraphAGE._entity_id("My Project") == "my_project"
    assert KnowledgeGraphAGE._entity_id("It's a Name") == "its_a_name"


def test_unwrap_agtype_handles_none_and_null_string():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert KnowledgeGraphAGE._unwrap_agtype(None) is None
    assert KnowledgeGraphAGE._unwrap_agtype("null") is None


def test_unwrap_agtype_decodes_json_strings():
    """AGE's adapter returns scalars JSON-quoted; we strip to plain Python."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert KnowledgeGraphAGE._unwrap_agtype('"hello"') == "hello"
    assert KnowledgeGraphAGE._unwrap_agtype("42") == 42
    assert KnowledgeGraphAGE._unwrap_agtype("true") is True


def test_unwrap_agtype_passes_invalid_json_through():
    """Bare strings that aren't valid JSON come back unchanged so the caller
    can still see something useful (rather than crashing)."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert KnowledgeGraphAGE._unwrap_agtype("not-quoted-json") == "not-quoted-json"


def test_unwrap_agtype_non_string_passes_through():
    """Already-decoded values (ints, dicts) round-trip unchanged."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert KnowledgeGraphAGE._unwrap_agtype(42) == 42
    assert KnowledgeGraphAGE._unwrap_agtype({"k": "v"}) == {"k": "v"}


def test_extract_return_aliases_finds_named_columns():
    """``_extract_return_aliases`` parses ``AS <name>`` markers from RETURN."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    # Bypass __init__ (which would try to connect) — just instantiate via
    # __new__ for unit-level access to the helper.
    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)

    aliases = kg._extract_return_aliases(
        "MATCH (w:Wing) RETURN w.name AS wing, w.size AS size LIMIT 10"
    )
    assert aliases == ["wing", "size"]


def test_extract_return_aliases_returns_empty_when_no_return():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    # CREATE without RETURN.
    assert kg._extract_return_aliases("CREATE (n:X {name: 'x'})") == []


def test_extract_return_aliases_handles_no_alias():
    """Bare RETURN clauses (no ``AS``) produce no aliases."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    aliases = kg._extract_return_aliases("MATCH (n) RETURN n")
    assert aliases == []


# =============================================================================
# KnowledgeGraphAGE — connection-mocked tests for __init__/close/clear
# =============================================================================


class _FakeCursor:
    """Tiny psycopg2-cursor-shaped stand-in. Records every execute()."""

    def __init__(self, fetchone_returns=None, fetchall_returns=None):
        self.executes: list[tuple[str, tuple]] = []
        self._fetchone = fetchone_returns or [None]
        self._fetchall = fetchall_returns or [[]]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executes.append((sql, params))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []


class _FakeConn:
    """Minimum-viable psycopg2 connection mock."""

    def __init__(self, *, fetchone_seq=None, fetchall_seq=None):
        self.closed = False
        self.autocommit = False
        self.commits = 0
        self._cursor = _FakeCursor(fetchone_returns=fetchone_seq, fetchall_returns=fetchall_seq)

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _build_kg_with_fake_conn(monkeypatch, *, fetchone_seq=None, fetchall_seq=None):
    """Construct a real ``KnowledgeGraphAGE`` whose connection is a fake.

    Patches ``_load_psycopg2`` so __init__ doesn't reach for a real driver.
    Returns the (kg, conn) pair so tests can inspect what SQL ran.
    """
    from mempalace import knowledge_graph_age as kg_mod

    conn = _FakeConn(fetchone_seq=fetchone_seq, fetchall_seq=fetchall_seq)

    class _FakeModule:
        @staticmethod
        def connect(_dsn):
            return conn

    monkeypatch.setattr(kg_mod, "_load_psycopg2", lambda: _FakeModule)
    kg = kg_mod.KnowledgeGraphAGE("postgresql://fake/db")
    return kg, conn


def test_kg_age_init_bootstraps_graph(monkeypatch):
    """__init__ runs LOAD age, sets search_path, and creates the graph if absent."""
    # First fetchone returns None → graph not present → create_graph runs.
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[None])
    statements = " ".join(sql for sql, _ in conn._cursor.executes)
    assert "CREATE EXTENSION" in statements
    assert "LOAD 'age'" in statements
    assert "search_path" in statements
    assert "create_graph" in statements
    # One for the graph setup, one for the Drawer-index check (#574: every path
    # of that check commits, so setup never sits idle in transaction).
    assert conn.commits == 2
    assert conn.autocommit is False


def test_kg_age_init_skips_create_when_graph_exists(monkeypatch):
    """If ag_graph already lists the graph, create_graph is NOT re-run."""
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[(1,)])
    statements = " ".join(sql for sql, _ in conn._cursor.executes)
    assert "create_graph" not in statements


def test_kg_age_close_releases_connection(monkeypatch):
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[None])
    assert not conn.closed
    kg.close()
    assert conn.closed


def test_kg_age_close_is_idempotent(monkeypatch):
    """Double-close shouldn't crash — the second call is a no-op."""
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[None])
    kg.close()
    # _conn.closed = True now, so the guard in close() should short-circuit.
    kg.close()  # must not raise


def test_kg_age_context_manager_closes(monkeypatch):
    """``with KnowledgeGraphAGE(dsn) as kg:`` releases on exit."""
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[None])
    with kg as ctx:
        assert ctx is kg
    assert conn.closed


def test_kg_age_clear_drops_and_recreates_graph(monkeypatch):
    """clear() should drop_graph + create_graph when the graph exists."""
    # init bootstrap: fetchone returns (1,) so create_graph is skipped on init,
    # then None for the unique-index table-check so it early-returns.
    # clear bootstrap: (1,) again so drop_graph runs, then None for unique-index.
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[(1,), None, (1,), None])
    initial_commits = conn.commits
    kg.clear()
    statements = " ".join(sql for sql, _ in conn._cursor.executes)
    assert "drop_graph" in statements
    assert statements.count("create_graph") >= 1
    assert conn.commits > initial_commits


def test_kg_age_clear_skips_drop_when_graph_absent(monkeypatch):
    """If the graph isn't there, clear() just creates — no drop_graph."""
    # init: fetchone None (no graph yet → init create runs)
    # clear: fetchone None again → no drop_graph
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[None, None])
    kg.clear()
    statements = " ".join(sql for sql, _ in conn._cursor.executes)
    assert "drop_graph" not in statements


def _last_cypher_property_map(conn) -> str:
    """Return the property-map portion of the most recent CREATE statement.

    Slices between the first ``RELATION {`` and the matching ``}]->`` so
    assertions can target the property map specifically (and ignore NULLs
    legitimately appearing inside WHERE clauses elsewhere in the query).
    """
    for sql, _ in reversed(conn._cursor.executes):
        if "RELATION {" in sql:
            start = sql.index("RELATION {") + len("RELATION {")
            end = sql.index("}]->", start)
            return sql[start:end]
    raise AssertionError("no CREATE RELATION statement found in executes")


def test_add_triple_omits_null_keys_from_property_map(monkeypatch):
    """Regression for techempower-org/mempalace#221.

    Cypher property maps reject bare ``NULL`` values
    (``SyntaxError: a name constant is expected``). When valid_from /
    valid_to / source are None, the corresponding keys must be omitted
    from the property map entirely — not emitted as ``key: NULL``.
    """
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[(1,)])
    kg.add_triple(subject="JP", relation_type="uses", object_="mempalace")

    prop_map = _last_cypher_property_map(conn)
    assert "NULL" not in prop_map, f"property map should not contain NULL: {prop_map!r}"
    assert "valid_from" not in prop_map
    assert "valid_to" not in prop_map
    assert "source" not in prop_map
    assert "relation_type: 'uses'" in prop_map
    assert "confidence: 1.0" in prop_map


def test_add_triple_includes_only_set_temporal_keys(monkeypatch):
    """When valid_from is set but valid_to is None, only valid_from appears."""
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[(1,)])
    kg.add_triple(
        subject="JP",
        relation_type="started",
        object_="run",
        valid_from="2026-05-01",
    )
    prop_map = _last_cypher_property_map(conn)
    assert "NULL" not in prop_map
    assert "valid_from: '2026-05-01'" in prop_map
    assert "valid_to" not in prop_map


def test_add_triple_emits_all_keys_when_fully_specified(monkeypatch):
    """All four optional fields set → all four appear in the property map."""
    kg, conn = _build_kg_with_fake_conn(monkeypatch, fetchone_seq=[(1,)])
    kg.add_triple(
        subject="JP",
        relation_type="worked_at",
        object_="techempower",
        source="drawer_xyz",
        valid_from="2020-01-01",
        valid_to="2026-01-01",
        confidence=0.95,
    )
    prop_map = _last_cypher_property_map(conn)
    assert "NULL" not in prop_map
    assert "source: 'drawer_xyz'" in prop_map
    assert "valid_from: '2020-01-01'" in prop_map
    assert "valid_to: '2026-01-01'" in prop_map
    assert "confidence: 0.95" in prop_map


# =============================================================================
# kg_writethrough — extractors and hook factories
# =============================================================================


def test_builtin_regex_extractor_finds_proper_nouns():
    from mempalace.kg_writethrough import _builtin_regex_extractor

    entities = _builtin_regex_extractor(
        "Anthropic and OpenAI both ship Claude and ChatGPT respectively."
    )
    names = {e.name for e in entities}
    # All capitalized words of length 3+, lowercased.
    assert "anthropic" in names
    assert "openai" in names
    assert "claude" in names
    assert "chatgpt" in names


def test_builtin_regex_extractor_finds_tech_idents():
    """Hyphenated lowercase identifiers like ``palace-daemon`` are tagged TECH_IDENT."""
    from mempalace.kg_writethrough import _builtin_regex_extractor

    entities = _builtin_regex_extractor(
        "Deploy palace-daemon via systemd; uses pg-vector and apache-age."
    )
    types_by_name = {e.name: e.type for e in entities}
    assert types_by_name.get("palace-daemon") == "TECH_IDENT"
    assert types_by_name.get("pg-vector") == "TECH_IDENT"
    assert types_by_name.get("apache-age") == "TECH_IDENT"


def test_builtin_regex_extractor_finds_version_strings():
    from mempalace.kg_writethrough import _builtin_regex_extractor

    entities = _builtin_regex_extractor("Upgrading to Python 3.12.0 and ruff v0.15.9 today.")
    names = {e.name for e in entities}
    assert "3.12.0" in names
    assert "v0.15.9" in names


def test_builtin_regex_extractor_counts_duplicates():
    """Repeated entities accumulate via Counter."""
    from mempalace.kg_writethrough import _builtin_regex_extractor

    entities = _builtin_regex_extractor("Claude said hello. Claude is here. Claude.")
    counts = {e.name: e.count for e in entities}
    assert counts.get("claude", 0) >= 3


def test_builtin_regex_extractor_empty_text_returns_empty():
    from mempalace.kg_writethrough import _builtin_regex_extractor

    assert _builtin_regex_extractor("") == []
    assert _builtin_regex_extractor("123 -- !@#$ no words") == []


def test_make_null_writethrough_returns_callable_that_does_nothing():
    from mempalace.kg_writethrough import make_null_writethrough

    hook = make_null_writethrough()
    # Must accept the canonical kwargs without raising.
    assert hook(drawer_id="d1", document="anything", metadata={}) is None


def _legacy_kg():
    """A KG with only the per-mention surface (no replace_mentions): the hook's fallback path."""
    return MagicMock(spec=["add_mention", "commit"])


def test_make_age_writethrough_replaces_a_drawers_mentions_in_one_call():
    """The factory wires extractor -> one kg.replace_mentions call carrying every entity."""
    from mempalace.kg_writethrough import _builtin_regex_extractor, make_age_writethrough

    kg = MagicMock()
    hook = make_age_writethrough(kg, _builtin_regex_extractor)

    hook(drawer_id="drw1", document="Claude meets OpenAI", metadata={})

    kg.replace_mentions.assert_called_once()
    args, kw = kg.replace_mentions.call_args
    assert args[0] == "drw1"
    assert {name for name, _t, _c in args[1]} >= {"claude", "openai"}
    assert kw["commit"] is True
    kg.add_mention.assert_not_called()


def test_make_age_writethrough_falls_back_to_add_mention_per_entity():
    """A KG without replace_mentions still gets one add_mention per entity."""
    from mempalace.kg_writethrough import _builtin_regex_extractor, make_age_writethrough

    kg = _legacy_kg()
    hook = make_age_writethrough(kg, _builtin_regex_extractor)

    hook(drawer_id="drw1", document="Claude meets OpenAI", metadata={})

    called_with_names = {kw["entity_name"] for _args, kw in kg.add_mention.call_args_list}
    assert "claude" in called_with_names
    assert "openai" in called_with_names


def test_make_age_writethrough_skips_empty_document():
    from mempalace.kg_writethrough import make_age_writethrough

    kg = MagicMock()

    def fake_extractor(text):  # pragma: no cover — never reached on empty doc
        raise AssertionError("extractor should not run on empty document")

    hook = make_age_writethrough(kg, fake_extractor)
    hook(drawer_id="d1", document="", metadata={})
    kg.add_mention.assert_not_called()
    kg.replace_mentions.assert_not_called()


def test_make_age_writethrough_swallows_extractor_errors():
    """A broken extractor must not break ingest — the hook logs and returns."""
    from mempalace.kg_writethrough import make_age_writethrough

    kg = MagicMock()

    def boom(text):
        raise RuntimeError("extractor exploded")

    hook = make_age_writethrough(kg, boom)
    # Must NOT raise.
    hook(drawer_id="d1", document="some text", metadata={})
    kg.add_mention.assert_not_called()
    kg.replace_mentions.assert_not_called()


def test_make_age_writethrough_swallows_replace_mentions_errors():
    """KG-side failures during enrichment don't break ingest either."""
    from mempalace.kg_writethrough import _builtin_regex_extractor, make_age_writethrough

    kg = MagicMock()
    kg.replace_mentions.side_effect = RuntimeError("kg blew up")

    hook = make_age_writethrough(kg, _builtin_regex_extractor)
    # Must NOT raise.
    hook(drawer_id="d1", document="Claude and OpenAI shipped models.", metadata={})
    kg.replace_mentions.assert_called_once()


def test_make_age_writethrough_swallows_add_mention_errors_on_the_fallback():
    """On the per-mention fallback, each failure is swallowed and the rest are still tried."""
    from mempalace.kg_writethrough import _builtin_regex_extractor, make_age_writethrough

    kg = _legacy_kg()
    kg.add_mention.side_effect = RuntimeError("kg blew up")

    hook = make_age_writethrough(kg, _builtin_regex_extractor)
    hook(drawer_id="d1", document="Claude and OpenAI shipped models.", metadata={})
    assert kg.add_mention.call_count >= 2  # tried for each entity, none re-raised


def test_make_age_writethrough_caps_entities_per_drawer():
    """``max_entities_per_drawer`` bounds what one drawer writes."""
    from mempalace.kg_writethrough import make_age_writethrough

    # Build an extractor returning 50 fake entities.
    class _Ent:
        def __init__(self, n):
            self.name = f"ent_{n}"
            self.type = "TEST"
            self.count = 1

    def extractor(text):
        return [_Ent(i) for i in range(50)]

    kg = MagicMock()
    make_age_writethrough(kg, extractor, max_entities_per_drawer=5)(
        drawer_id="d1", document="anything", metadata={}
    )
    assert len(kg.replace_mentions.call_args[0][1]) == 5

    legacy = _legacy_kg()
    make_age_writethrough(legacy, extractor, max_entities_per_drawer=5)(
        drawer_id="d1", document="anything", metadata={}
    )
    assert legacy.add_mention.call_count == 5


def test_make_writethrough_from_env_returns_none_when_disabled(monkeypatch):
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    assert make_writethrough_from_env(kg=MagicMock()) is None


def test_make_writethrough_from_env_requires_kg_when_enabled(monkeypatch):
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    with pytest.raises(ValueError, match="kg must be provided"):
        make_writethrough_from_env(kg=None)


def test_make_writethrough_from_env_null_extractor_returns_null_hook(monkeypatch):
    """``MEMPALACE_KG_EXTRACTOR=null`` returns the no-op hook."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "null")
    hook = make_writethrough_from_env(kg=MagicMock())
    assert hook is not None
    # The null hook accepts and ignores everything.
    assert hook(drawer_id="d", document="x", metadata={}) is None


def test_make_writethrough_from_env_rejects_unknown_extractor(monkeypatch):
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "spacy-but-not-yet")
    with pytest.raises(ValueError, match="unknown MEMPALACE_KG_EXTRACTOR"):
        make_writethrough_from_env(kg=MagicMock())


def test_make_writethrough_from_env_regex_returns_age_hook(monkeypatch):
    """Default regex path returns a usable hook bound to the supplied KG."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTOR", raising=False)
    kg = MagicMock()
    hook = make_writethrough_from_env(kg=kg)
    assert hook is not None
    hook(drawer_id="d1", document="Claude greets OpenAI.", metadata={})
    assert kg.replace_mentions.called


# =============================================================================
# backfill_age — pure helpers + CLI argparse paths
# =============================================================================


def test_validate_pg_identifier_accepts_valid_names():
    from mempalace.backfill_age import _validate_pg_identifier

    assert _validate_pg_identifier("mempalace_drawers") == "mempalace_drawers"
    assert _validate_pg_identifier("Drawer42") == "Drawer42"
    assert _validate_pg_identifier("_private") == "_private"


@pytest.mark.parametrize(
    "bad",
    [
        "; DROP TABLE foo",
        "table-name",  # hyphen not allowed
        "table name",  # space
        "1table",  # leading digit
        "",  # empty
    ],
)
def test_validate_pg_identifier_rejects_injection_attempts(bad):
    from mempalace.backfill_age import _validate_pg_identifier

    with pytest.raises(ValueError, match="invalid postgres identifier"):
        _validate_pg_identifier(bad)


def test_validate_pg_identifier_rejects_non_string():
    from mempalace.backfill_age import _validate_pg_identifier

    with pytest.raises(ValueError):
        _validate_pg_identifier(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _validate_pg_identifier(123)  # type: ignore[arg-type]


def test_get_extractor_returns_callable():
    """The regex extractor falls back to the builtin when SME isn't installed."""
    from mempalace.backfill_age import _get_extractor

    extractor = _get_extractor("regex")
    assert callable(extractor)
    # The fallback should still produce entity-shaped objects from text.
    entities = extractor("Claude meets OpenAI today.")
    assert all(hasattr(e, "name") for e in entities)


def test_get_extractor_unknown_name_raises():
    from mempalace.backfill_age import _get_extractor

    with pytest.raises(ValueError, match="unknown extractor"):
        _get_extractor("totally-not-a-real-extractor")


def test_backfill_main_exits_nonzero_without_dsn():
    """The CLI ``main()`` rejects missing DSN before opening any connection.

    argparse signals required-arg failure via SystemExit(2), not by
    returning a non-zero code. Treat either signal as success here.
    """
    from mempalace.backfill_age import main

    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            rc = main(["--table-name", "mempalace_drawers"])
        except SystemExit as e:
            rc = e.code
    assert rc not in (0, None)
    assert "--dsn" in buf.getvalue() or "dsn" in buf.getvalue().lower()


def test_make_age_writethrough_skips_when_extractor_returns_empty():
    """The early-return on ``not entities`` (line 104) is reached when
    the extractor finds nothing. The KG should NOT be touched."""
    from mempalace.kg_writethrough import make_age_writethrough

    kg = MagicMock()
    hook = make_age_writethrough(kg, lambda text: [])
    hook(drawer_id="d1", document="content with no extractable entities", metadata={})
    kg.add_mention.assert_not_called()


# =============================================================================
# backfill_age — checkpoint helpers with a connection mock
# =============================================================================


class _CheckpointCursor:
    """Cursor stand-in that captures SQL + lets the test seed fetchone/rowcount."""

    def __init__(self, fetchone=None, rowcount=0):
        self.executes: list[tuple[str, tuple | None]] = []
        self._fetchone = fetchone
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executes.append((sql, params))

    def fetchone(self):
        return self._fetchone


class _CheckpointConn:
    """Connection stand-in for backfill_age._checkpoint_* helpers."""

    def __init__(self, *, fetchone=None, rowcount=0):
        self._cursor = _CheckpointCursor(fetchone=fetchone, rowcount=rowcount)
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def test_ensure_checkpoint_table_emits_create_and_commits():
    from mempalace.backfill_age import CHECKPOINT_TABLE, _ensure_checkpoint_table

    conn = _CheckpointConn()
    _ensure_checkpoint_table(conn)
    sql = conn._cursor.executes[0][0]
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert CHECKPOINT_TABLE in sql
    assert conn.commits == 1


def test_checkpoint_done_returns_true_when_row_exists():
    from mempalace.backfill_age import _checkpoint_done

    conn = _CheckpointConn(fetchone=(1,))
    assert _checkpoint_done(conn, phase="mine", key="k1") is True
    sql, params = conn._cursor.executes[0]
    assert "SELECT 1 FROM" in sql
    assert params == ("mine", "k1")


def test_checkpoint_done_returns_false_when_absent():
    from mempalace.backfill_age import _checkpoint_done

    conn = _CheckpointConn(fetchone=None)
    assert _checkpoint_done(conn, phase="mine", key="missing") is False


def test_checkpoint_mark_inserts_with_conflict_clause():
    from mempalace.backfill_age import _checkpoint_mark

    conn = _CheckpointConn()
    _checkpoint_mark(conn, phase="mine", key="k1")
    sql, params = conn._cursor.executes[0]
    assert "INSERT INTO" in sql
    assert "ON CONFLICT" in sql
    assert params == ("mine", "k1")
    assert conn.commits == 1


def test_checkpoint_clear_deletes_and_returns_rowcount():
    from mempalace.backfill_age import _checkpoint_clear

    conn = _CheckpointConn(rowcount=7)
    deleted = _checkpoint_clear(conn)
    assert deleted == 7
    sql, _ = conn._cursor.executes[0]
    assert "DELETE FROM" in sql
    assert conn.commits == 1


def test_backfill_main_rejects_bad_table_name():
    """The CLI rejects identifiers that wouldn't pass ``_validate_pg_identifier``."""
    from mempalace.backfill_age import main

    buf = io.StringIO()
    # With an obviously malicious table name, argparse-or-validation should
    # short-circuit before any postgres call.
    with redirect_stderr(buf):
        try:
            rc = main(
                [
                    "--dsn",
                    "postgresql://unreachable.example:9/none",
                    "--table-name",
                    "drop; DELETE",
                ]
            )
        except (ValueError, SystemExit) as e:
            rc = getattr(e, "code", 1) or 1
    assert rc != 0


# =============================================================================
# palace_graph_age — fully-mocked KG walks (no AGE required)
# =============================================================================


def test_walk_wing_executes_cypher_with_wing_param():
    """``walk_wing`` should call ``kg._run_cypher`` with the wing name
    interpolated as ``$wing``. We mock the KG and verify the call."""
    from mempalace.palace_graph_age import walk_wing

    kg = MagicMock()
    kg._run_cypher.return_value = []

    walk_wing(kg, "wing_test", depth=1, limit=10)
    kg._run_cypher.assert_called()
    cypher_arg = kg._run_cypher.call_args.args[0]
    params_arg = (
        kg._run_cypher.call_args.args[1]
        if len(kg._run_cypher.call_args.args) > 1
        else kg._run_cypher.call_args.kwargs.get("params", {})
    )
    assert "$wing" in cypher_arg
    assert params_arg.get("wing") == "wing_test"
    assert params_arg.get("limit") == 10


def _mock_kg_with_rows(rows):
    """Build a fake KG that returns ``rows`` from _run_cypher and a no-op
    _unwrap_agtype (passes the value through, since AGE's agtype wrapping
    only matters with a real driver)."""
    kg = MagicMock()
    kg._run_cypher.return_value = rows
    kg._unwrap_agtype.side_effect = lambda v: v
    return kg


def test_list_wings_extracts_name_column():
    """``list_wings`` should return a flat list of wing names."""
    from mempalace.palace_graph_age import list_wings

    # Rows are tuple-shaped — palace_graph_age uses r[0] to read the first
    # projected column.
    kg = _mock_kg_with_rows([("wing_a",), ("wing_b",)])
    result = list_wings(kg, limit=10)
    assert sorted(result) == ["wing_a", "wing_b"]


def test_list_rooms_in_wing_extracts_room_column():
    from mempalace.palace_graph_age import list_rooms_in_wing

    kg = _mock_kg_with_rows([("decisions",), ("architecture",)])
    result = list_rooms_in_wing(kg, "wing_x", limit=10)
    assert "decisions" in result
    assert "architecture" in result


def test_list_drawers_in_room_extracts_id_column():
    from mempalace.palace_graph_age import list_drawers_in_room

    kg = _mock_kg_with_rows([("drw_1",), ("drw_2",)])
    result = list_drawers_in_room(kg, "room_x", limit=10)
    assert "drw_1" in result
    assert "drw_2" in result


def test_tunnels_from_wing_pairs_to_wing_and_via_room():
    from mempalace.palace_graph_age import tunnels_from_wing

    kg = _mock_kg_with_rows([("wing_b", "shared_room"), ("wing_c", "other_room")])
    result = tunnels_from_wing(kg, "wing_a")
    assert {"to_wing": "wing_b", "via_room": "shared_room"} in result
    assert {"to_wing": "wing_c", "via_room": "other_room"} in result


# ───────────────────────────────────────────────────────────────────
# kg_writethrough — delete-through hook
# ───────────────────────────────────────────────────────────────────


def test_make_null_deletethrough_returns_callable_that_does_nothing():
    from mempalace.kg_writethrough import make_null_deletethrough

    hook = make_null_deletethrough()
    assert hook(drawer_ids=["a", "b"]) is None
    assert hook(drawer_ids=[]) is None


def test_make_age_deletethrough_calls_delete_drawers_once():
    """The hook forwards the id list to kg.delete_drawers in one call."""
    from mempalace.kg_writethrough import make_age_deletethrough

    kg = MagicMock()
    hook = make_age_deletethrough(kg)
    hook(drawer_ids=["d1", "d2", "d3"])
    kg.delete_drawers.assert_called_once_with(["d1", "d2", "d3"])


def test_make_age_deletethrough_skips_empty_list():
    from mempalace.kg_writethrough import make_age_deletethrough

    kg = MagicMock()
    hook = make_age_deletethrough(kg)
    hook(drawer_ids=[])
    kg.delete_drawers.assert_not_called()


def test_make_age_deletethrough_swallows_kg_errors():
    """KG-side failures during delete must not break the relational delete."""
    from mempalace.kg_writethrough import make_age_deletethrough

    kg = MagicMock()
    kg.delete_drawers.side_effect = RuntimeError("kg blew up")
    hook = make_age_deletethrough(kg)
    # Must NOT raise — caller has already committed the relational DELETE.
    hook(drawer_ids=["d1"])
    kg.delete_drawers.assert_called_once()


def test_make_deletethrough_from_env_returns_none_when_disabled(monkeypatch):
    from mempalace.kg_writethrough import make_deletethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    assert make_deletethrough_from_env(kg=MagicMock()) is None


def test_make_deletethrough_from_env_requires_kg_when_enabled(monkeypatch):
    from mempalace.kg_writethrough import make_deletethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    with pytest.raises(ValueError, match="kg must be provided"):
        make_deletethrough_from_env(kg=None)


def test_make_deletethrough_from_env_enabled_returns_hook(monkeypatch):
    from mempalace.kg_writethrough import make_deletethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    kg = MagicMock()
    hook = make_deletethrough_from_env(kg=kg)
    assert hook is not None
    hook(drawer_ids=["d1"])
    kg.delete_drawers.assert_called_once_with(["d1"])


# =============================================================================
# knowledge_graph_age — thread-safety of the shared (autocommit=False) conn
# =============================================================================
#
# A single KnowledgeGraphAGE instance is reachable from multiple threads when
# the inline KG write-through hook (MEMPALACE_KG_WRITETHROUGH=1) is attached —
# the daemon offloads each write to a threadpool, so two concurrent writes call
# add_mention on the same instance, sharing one psycopg connection. With
# autocommit=False, psycopg's per-connection lock keeps each statement atomic on
# the wire but does NOT isolate the transaction span: thread A's CREATE and
# thread B's CREATE can land in one shared transaction and a single commit
# decides both. The RLock added in __init__ restores one-transaction-per-thread
# semantics. These tests pin that guard so it can't silently regress.


def test_run_cypher_acquires_lock():
    """The write/read chokepoint must hold self._lock across its span."""
    import inspect

    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    src = inspect.getsource(KnowledgeGraphAGE._run_cypher)
    assert "self._lock" in src, (
        "_run_cypher does not acquire self._lock. Concurrent threads sharing "
        "the autocommit=False connection can interleave transactions and "
        "silently drop KG writes."
    )


def test_cypher_scalar_acquires_lock():
    import inspect

    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    src = inspect.getsource(KnowledgeGraphAGE._cypher_scalar)
    assert "self._lock" in src


def test_stats_and_commit_acquire_lock():
    import inspect

    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert "self._lock" in inspect.getsource(KnowledgeGraphAGE.stats)
    assert "self._lock" in inspect.getsource(KnowledgeGraphAGE.commit)
    assert "self._lock" in inspect.getsource(KnowledgeGraphAGE.clear)


def test_run_cypher_serializes_transaction_spans_across_threads():
    """Functional proof: two threads driving _run_cypher on one shared
    connection never interleave their execute→commit spans.

    Uses a fake connection that records (thread, event) ordering and asserts
    every thread's ``execute`` is immediately followed by its own ``commit``
    with no other thread's event in between — which is exactly what the
    transaction-span lock guarantees and what a bare shared autocommit=False
    connection would violate.
    """
    import threading
    import time

    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    events: list[tuple[str, str]] = []
    events_lock = threading.Lock()

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            with events_lock:
                events.append((threading.current_thread().name, "execute"))
            # Yield the GIL mid-span so an unguarded impl would interleave.
            time.sleep(0.001)

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            with events_lock:
                events.append((threading.current_thread().name, "commit"))

    # Build an instance without touching Postgres: __new__ skips __init__'s
    # connect() / _ensure_graph(), then inject the fake conn + real RLock.
    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    kg._conn = _FakeConn()
    kg._lock = threading.RLock()
    kg._age_loaded = True  # skip the LOAD 'age' / SET search_path preamble

    def worker():
        for _ in range(20):
            kg._run_cypher("MATCH (n) RETURN n", {}, fetch=False, commit=True)

    threads = [threading.Thread(target=worker, name=f"w{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Walk the event log: every execute must be followed by a commit from the
    # SAME thread before any other thread acts. The lock guarantees this; an
    # unguarded shared connection would show a foreign event between them.
    for i in range(0, len(events), 2):
        exec_thread, exec_kind = events[i]
        commit_thread, commit_kind = events[i + 1]
        assert exec_kind == "execute" and commit_kind == "commit", (
            f"span boundary broken at index {i}: {events[i]}, {events[i + 1]}"
        )
        assert exec_thread == commit_thread, (
            f"transaction span interleaved: execute by {exec_thread} but "
            f"commit by {commit_thread} — the per-instance lock is not "
            f"serializing spans"
        )
