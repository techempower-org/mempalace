"""#558: the delete-through attaches independently of write-through.

Measured in production 2026-11-13: with MEMPALACE_KG_WRITETHROUGH=0 (paused for a
re-mine drain) the delete hook was never attached, so every drawer deleted through
the daemon left its AGE Drawer vertex behind.
"""
from mempalace import palace as col_mod


class _RecordingKG:
    def __init__(self):
        self.deleted = []

    def delete_drawers(self, ids):
        self.deleted.append(list(ids))


class _Col:
    def __init__(self):
        self.delete_hook = None

    def set_kg_deletethrough(self, hook):
        self.delete_hook = hook


def _env(monkeypatch, write="0", delete=None):
    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", write)
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "0")
    if delete is None:
        monkeypatch.delenv("MEMPALACE_KG_DELETETHROUGH", raising=False)
    else:
        monkeypatch.setenv("MEMPALACE_KG_DELETETHROUGH", delete)


def _attach(col, dsn):
    col_mod._writethrough_attached.discard(id(col))
    col_mod._no_age_graph.discard(dsn)
    col_mod._maybe_attach_writethrough(col, dsn)


def test_delete_through_attaches_with_write_through_off(monkeypatch):
    _env(monkeypatch)
    kg = _RecordingKG()
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: True)
    monkeypatch.setattr("mempalace.knowledge_graph_age.KnowledgeGraphAGE", lambda dsn: kg)
    col = _Col()
    _attach(col, "postgresql://fake/a")
    assert col.delete_hook is not None
    col.delete_hook(drawer_ids=["d1", "d2"])
    assert kg.deleted == [["d1", "d2"]]


def test_no_graph_no_hook_and_no_graph_is_created(monkeypatch):
    _env(monkeypatch)
    probes, built = [], []
    monkeypatch.setattr(
        "mempalace.kg_writethrough.age_graph_exists", lambda dsn: probes.append(dsn) or False
    )
    monkeypatch.setattr(
        "mempalace.knowledge_graph_age.KnowledgeGraphAGE", lambda dsn: built.append(dsn)
    )
    col = _Col()
    _attach(col, "postgresql://fake/b")
    assert col.delete_hook is None and built == []
    # a second collection on the same DSN does not probe again
    col_mod._maybe_attach_writethrough(_Col(), "postgresql://fake/b")
    assert probes == ["postgresql://fake/b"]


def test_explicit_opt_out_attaches_nothing(monkeypatch):
    _env(monkeypatch, delete="0")
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: True)
    monkeypatch.setattr(
        "mempalace.knowledge_graph_age.KnowledgeGraphAGE", lambda dsn: _RecordingKG()
    )
    col = _Col()
    _attach(col, "postgresql://fake/c")
    assert col.delete_hook is None


def test_age_graph_exists_is_false_when_the_server_is_unreachable():
    from mempalace.kg_writethrough import age_graph_exists

    assert age_graph_exists("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1") is False
