"""Outline adapter: published documents per collection, verbatim, read-only; reruns leave nothing stale.

A fake Outline client stands in for the API. Also covers core's unchanged-skip:
a rerun of the same content writes nothing.
"""

import pytest

from mempalace.sources import AuthRequiredError, SourceNotFoundError, SourceRef, get_adapter
from mempalace.sources.outline import OutlineSourceAdapter


def _fake(state, calls=None):
    def call(method, body):
        if calls is not None:
            calls.append(method)
        if method == "auth.info":
            return {"data": {"team": {"id": "T1", "url": "https://wiki.example"}}}
        if method == "collections.list":
            return {"data": state["collections"]} if body.get("offset", 0) == 0 else {"data": []}
        if method == "documents.list":
            docs = state["docs"].get(body["collectionId"], [])
            return {"data": docs[body["offset"] : body["offset"] + body["limit"]]}
        raise AssertionError(f"unexpected Outline method {method}")

    return call


def _doc(i, text, title=None):
    return {
        "id": f"d{i}",
        "title": title or f"Doc {i}",
        "text": text,
        "url": f"/doc/d{i}",
        "updatedAt": "2026-10-27T00:00:00Z",
        "updatedBy": {"name": "JP"},
    }


STATE = {
    "collections": [{"id": "c1", "name": "Projects"}, {"id": "c2", "name": "Guides"}],
    "docs": {
        "c1": [_doc(i, f"Body of project doc {i}.") for i in range(150)],
        "c2": [_doc(900, "How to back up.")],
    },
}


def _ingest(adapter):
    return list(adapter.ingest(source=SourceRef(local_path=None), palace=None))


def test_registered():
    assert get_adapter("outline").name == "outline"


def test_every_collection_paged_verbatim_read_only(monkeypatch):
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ())
    calls = []
    drawers = _ingest(OutlineSourceAdapter(call=_fake(STATE, calls), sources=None))
    files = {d.source_file for d in drawers}
    assert len(files) == 151 and "outline://T1/d149" in files
    text = "\n".join(d.content for d in drawers)
    assert "Body of project doc 149." in text and "URL: https://wiki.example/doc/d900" in text
    assert {d.metadata["wing"] for d in drawers} == {"outline_wiki"}
    assert set(calls) <= {"auth.info", "collections.list", "documents.list"}


def test_configured_collections_and_wings(monkeypatch):
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ())
    adapter = OutlineSourceAdapter(
        call=_fake(STATE), sources=[{"collection": "Guides", "wing": "wiki"}]
    )
    drawers = _ingest(adapter)
    assert {d.source_file for d in drawers} == {"outline://T1/d900"}
    assert {d.metadata["wing"] for d in drawers} == {"wiki"}
    with pytest.raises(SourceNotFoundError):
        _ingest(OutlineSourceAdapter(call=_fake(STATE), sources=[{"collection": "Nope"}]))
    with pytest.raises(SourceNotFoundError):
        _ingest(
            OutlineSourceAdapter(
                call=_fake(STATE), sources=[{"collection": "Guides"}, {"collection": "Guides"}]
            )
        )


def test_a_marked_document_is_sensitive_as_a_whole(monkeypatch):
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ("acme-call",))
    long = "\n\n".join(f"paragraph {i} " + "words " * 60 for i in range(6))
    state = {
        "collections": [{"id": "c1", "name": "P"}],
        "docs": {"c1": [_doc(1, long + "\n\nthe acme-call notes")]},
    }
    drawers = _ingest(OutlineSourceAdapter(call=_fake(state), sources=None))
    assert len(drawers) > 1 and all(d.metadata["privacy_class"] == "sensitive" for d in drawers)


def test_no_token_is_an_auth_error(monkeypatch):
    monkeypatch.delenv("MEMPALACE_OUTLINE_TOKEN", raising=False)
    with pytest.raises(AuthRequiredError):
        _ingest(OutlineSourceAdapter(sources=None, base_url="https://wiki.example"))


def test_reruns_remove_deleted_documents_and_skip_unchanged_ones(tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import get_collection
    from mempalace.sources import register, unregister

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ())
    state = {
        "collections": [{"id": "c1", "name": "P"}],
        "docs": {"c1": [_doc(1, "KEEP me"), _doc(2, "DELETE me")]},
    }

    class _Fake(OutlineSourceAdapter):
        name = "test-outline-rerun"

        def __init__(self):
            super().__init__(call=_fake(state), sources=None)

    palace = str(tmp_path / "palace")
    register(_Fake.name, _Fake)
    try:
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        col = get_collection(palace)
        assert any("DELETE me" in d for d in col.get(include=["documents"])["documents"])

        upserts = []
        real = type(col).upsert

        def counting(self, **kw):
            upserts.append(len(kw["ids"]))
            return real(self, **kw)

        monkeypatch.setattr(type(col), "upsert", counting)
        state["docs"]["c1"] = [_doc(1, "KEEP me")]
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        docs = get_collection(palace).get(include=["documents"])["documents"]
        assert any("KEEP me" in d for d in docs) and not any("DELETE me" in d for d in docs)
        assert upserts == [], "an unchanged document is not re-embedded"
    finally:
        unregister(_Fake.name)


def test_a_metadata_only_change_is_still_written(tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import get_collection
    from mempalace.sources import register, unregister

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ())
    state = {"collections": [{"id": "c1", "name": "P"}], "docs": {"c1": [_doc(1, "same text")]}}
    sources = [
        {"collection": "P", "wing": "first"}
    ]  # the adapter instance is cached: mutate in place

    class _Fake(OutlineSourceAdapter):
        name = "test-outline-wing"

        def __init__(self):
            super().__init__(call=_fake(state), sources=sources)

    palace = str(tmp_path / "palace")
    register(_Fake.name, _Fake)
    try:
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        sources[0]["wing"] = "second"
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        metas = get_collection(palace).get(include=["metadatas"])["metadatas"]
        assert {m["wing"] for m in metas} == {"second"}
    finally:
        unregister(_Fake.name)


def test_an_error_envelope_aborts_without_deleting_anything(tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import get_collection
    from mempalace.sources import register, unregister

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ())
    state = {"collections": [{"id": "c1", "name": "P"}], "docs": {"c1": [_doc(1, "STILL here")]}}
    broken = {"on": False}

    def call(method, body):
        if broken["on"] and method == "documents.list":
            return {"ok": False, "error": "internal", "data": []}
        return _fake(state)(method, body)

    class _Fake(OutlineSourceAdapter):
        name = "test-outline-error"

        def __init__(self):
            super().__init__(call=call, sources=None)

    palace = str(tmp_path / "palace")
    register(_Fake.name, _Fake)
    try:
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        broken["on"] = True
        with pytest.raises(Exception):
            mine_source_adapter(
                source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace
            )
        assert any(
            "STILL here" in d
            for d in get_collection(palace).get(include=["documents"])["documents"]
        )
    finally:
        unregister(_Fake.name)


def test_a_redirect_is_refused_and_the_token_goes_nowhere():
    import http.server
    import threading
    import urllib.error
    import urllib.request

    from mempalace.sources.outline import _NoRedirect

    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST  # urllib turns a 302 on POST into a GET and keeps Authorization

        def log_message(self, *a):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
            self.end_headers()

        def log_message(self, *a):
            pass

    source = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, source):
        threading.Thread(target=srv.handle_request, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    req = urllib.request.Request(
        f"http://127.0.0.1:{source.server_port}/api/auth.info",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer SECRET"},
    )
    with pytest.raises(urllib.error.HTTPError):
        opener.open(req, timeout=5)
    assert seen == []
    target.server_close()
    source.server_close()


def test_the_unchanged_skip_sees_removed_keys_and_types():
    from mempalace.sources.context import _stored_matches

    class Col:
        def __init__(self, meta):
            self.meta = meta

        def get(self, ids, include):
            return {"ids": ids, "documents": ["x"], "metadatas": [self.meta]}

    assert _stored_matches(Col({"a": 1}), "i", "x", {"a": 1})
    assert not _stored_matches(Col({"a": 1, "gone": "restricted"}), "i", "x", {"a": 1})
    assert not _stored_matches(Col({"flag": 1}), "i", "x", {"flag": True})
    assert not _stored_matches(Col({"a": 1}), "i", "y", {"a": 1})


def test_a_document_that_gains_a_marker_leaves_the_shared_collection(tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import PRIVATE_COLLECTION_NAME, get_collection
    from mempalace.sources import register, unregister

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("mempalace.sources.outline.configured_markers", lambda: ("acme-call",))
    state = {"collections": [{"id": "c1", "name": "P"}], "docs": {"c1": [_doc(1, "NOTES plain")]}}

    class _Fake(OutlineSourceAdapter):
        name = "test-outline-relabel"

        def __init__(self):
            super().__init__(call=_fake(state), sources=None)

    palace = str(tmp_path / "palace")
    register(_Fake.name, _Fake)
    try:
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        state["docs"]["c1"] = [_doc(1, "NOTES now from the acme-call")]
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        shared = get_collection(palace).get(include=["documents"])["documents"]
        private = get_collection(palace, collection_name=PRIVATE_COLLECTION_NAME).get(
            include=["documents"]
        )["documents"]
        assert not any("NOTES" in d for d in shared)
        assert any("acme-call" in d for d in private)
    finally:
        unregister(_Fake.name)
