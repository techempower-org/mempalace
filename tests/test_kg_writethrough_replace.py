"""The write-through replaces a drawer's MENTIONS edges instead of adding a second set.

2026-10-24, after the upstream port (#578): the chunker-version bump re-filed every exchange drawer of the live
transcripts. An incremental re-mine upserts a changed drawer under its old id with no delete-through, and
``add_mention`` always CREATEs, so each re-file added a full second set of edges. A sample of pre-port drawers had 13 of
50 carrying duplicated (drawer, entity) pairs, and one live-session drawer held 16,872 edges over 36 entities. The same
pass spent ~90% of its wall time in one MERGE/CREATE round trip per mention.

``KnowledgeGraphAGE.replace_mentions`` deletes the drawer's old edges, then writes every current edge in one UNWIND
statement; both write-through hooks use it when the KG has it.
"""

from mempalace import kg_writethrough as kgw
from mempalace.knowledge_graph_age import KnowledgeGraphAGE


class _Entity:
    def __init__(self, name, type="unknown", count=1):
        self.name = name
        self.type = type
        self.count = count


class _GraphKG:
    """A KG that keeps an actual edge list, so duplication would be visible, with replace-then-commit semantics."""

    def __init__(self):
        self.edges = {}  # committed: drawer_id -> list of entity names (a list, so duplicates would show)
        self.pending = {}
        self.replace_calls = 0
        self.commits = 0

    def replace_mentions(self, drawer_id, mentions, *, confidence=0.5, commit=True):
        self.replace_calls += 1
        names = []
        for name, _etype, _count in mentions:
            if name not in names:
                names.append(name)
        self.pending[drawer_id] = names
        if commit:
            self.commit()
        return len(names)

    # pragma: no cover - must never run when replace_mentions exists
    def add_mention(self, *a, **k):
        raise AssertionError("add_mention used although replace_mentions exists")

    def commit(self):
        self.commits += 1
        self.edges.update(self.pending)
        self.pending = {}


def _extractor(text):
    return [_Entity(word) for word in text.split()]


def _batch(kg, drawers, **kw):
    kgw.make_age_batch_writethrough(kg, _extractor, **kw)(drawers)


def test_refiling_the_same_drawer_twice_leaves_one_edge_per_entity():
    kg = _GraphKG()
    drawers = [{"drawer_id": "d1", "document": "alpha beta", "metadata": {}}]
    _batch(kg, drawers)
    _batch(kg, drawers)
    assert kg.edges == {"d1": ["alpha", "beta"]}


def test_a_rewritten_drawer_ends_with_only_its_current_entities():
    kg = _GraphKG()
    _batch(kg, [{"drawer_id": "d1", "document": "alpha beta", "metadata": {}}])
    _batch(kg, [{"drawer_id": "d1", "document": "gamma", "metadata": {}}])
    assert kg.edges == {"d1": ["gamma"]}


def test_a_rewrite_with_no_entities_clears_the_old_edges():
    kg = _GraphKG()
    _batch(kg, [{"drawer_id": "d1", "document": "alpha", "metadata": {}}])
    kgw.make_age_batch_writethrough(kg, lambda text: [])(
        [{"drawer_id": "d1", "document": "now about nothing"}]
    )
    assert kg.edges == {"d1": []}


def test_one_write_per_drawer_and_one_commit_per_batch_whatever_the_entity_count():
    kg = _GraphKG()
    drawers = [
        {"drawer_id": f"d{i}", "document": " ".join(f"e{j}" for j in range(40))} for i in range(25)
    ]
    _batch(kg, drawers)
    assert kg.replace_calls == 25
    assert kg.commits == 1
    assert all(len(v) == 40 for v in kg.edges.values())


def test_the_entity_cap_still_applies():
    kg = _GraphKG()
    _batch(kg, [{"drawer_id": "d1", "document": "a b c d e"}], max_entities_per_drawer=2)
    assert kg.edges == {"d1": ["a", "b"]}


def test_the_per_drawer_hook_replaces_and_commits_per_drawer():
    kg = _GraphKG()
    hook = kgw.make_age_writethrough(kg, _extractor)
    hook(drawer_id="d1", document="alpha beta", metadata={})
    hook(drawer_id="d1", document="alpha beta", metadata={})
    assert kg.edges == {"d1": ["alpha", "beta"]}
    assert kg.commits == 2


def test_a_failing_replace_costs_its_drawer_not_the_loop():
    class _Failing(_GraphKG):
        def replace_mentions(self, drawer_id, mentions, **k):
            if drawer_id == "bad":
                raise RuntimeError("AGE said no")
            return super().replace_mentions(drawer_id, mentions, **k)

    kg = _Failing()
    _batch(kg, [{"drawer_id": "bad", "document": "x"}, {"drawer_id": "good", "document": "y"}])
    assert kg.edges == {"good": ["y"]}


# ---------------------------------------------------------------------------
# KnowledgeGraphAGE.replace_mentions: the statements it sends
# ---------------------------------------------------------------------------


def _capturing_kg():
    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    kg.sent = []

    def _run_cypher(cypher, params, *, commit=True, fetch=False):
        kg.sent.append((" ".join(cypher.split()), dict(params), commit))
        return []

    kg._run_cypher = _run_cypher
    return kg


def test_replace_deletes_first_then_writes_all_edges_in_one_unwind():
    kg = _capturing_kg()
    n = kg.replace_mentions(
        "drawer_x", [("Alpha", "org", 2), ("Beta", "person", 1)], confidence=0.5, commit=False
    )
    assert n == 2
    assert len(kg.sent) == 2
    delete, write = kg.sent
    assert delete[0].startswith("MATCH (d:Drawer {id: $did})-[m:MENTIONS]->() DELETE m")
    assert delete[1] == {"did": "drawer_x"} and delete[2] is False
    assert (
        "UNWIND [{name: 'Alpha', etype: 'org', cnt: 2, conf: 0.5}, {name: 'Beta', etype: 'person', cnt: 1, conf: 0.5}]"
        in write[0]
    )
    assert write[0].startswith("MERGE (d:Drawer {id: $did}) WITH d UNWIND")
    assert write[0].count("CREATE (d)-[:MENTIONS") == 1
    assert write[2] is False


def test_repeated_names_collapse_to_one_edge_with_the_highest_count():
    kg = _capturing_kg()
    assert (
        kg.replace_mentions("d", [("Alpha", "org", 1), ("Alpha", "person", 5), ("Beta", None, 1)])
        == 2
    )
    write = kg.sent[1][0]
    assert write.count("name: 'Alpha'") == 1
    assert "{name: 'Alpha', etype: 'org', cnt: 5" in write
    assert "{name: 'Beta', etype: 'unknown', cnt: 1" in write


def test_no_mentions_only_deletes_and_commits_that():
    kg = _capturing_kg()
    assert kg.replace_mentions("d", [], commit=True) == 0
    assert len(kg.sent) == 1
    assert kg.sent[0][0].endswith("DELETE m") and kg.sent[0][2] is True


def test_the_delete_never_commits_on_its_own_when_edges_follow():
    kg = _capturing_kg()
    kg.replace_mentions("d", [("Alpha", "org", 1)], commit=True)
    assert [c for _, _, c in kg.sent] == [False, True]


def test_quotes_in_names_are_escaped_and_bad_names_skipped():
    kg = _capturing_kg()
    kg.replace_mentions("d", [("O'Brien", "person", 1), ("", "x", 1)])
    write = kg.sent[1][0]
    assert "name: 'O\\'Brien'" in write or "name: 'OBrien'" in write
    # The empty name was skipped; "ent.name" in the MERGE is not a list entry.
    assert write.count("{name: '") == 1
