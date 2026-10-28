"""livekit-voice call records: a ``sensitive`` source filed only into the private collection.

A call carries an outside party's words, so its drawers must land in
``PRIVATE_COLLECTION_NAME`` and nowhere a search, status or the knowledge graph
reads. Only spoken turns are kept, in-progress records are skipped, and
nothing under an excluded root is read, even through a symlink.
"""

import json

import pytest

import mempalace.sources.livekit_calls as livekit_calls
from mempalace.cli import SensitiveKnowledgeGraphWrite, _drawer_collection_name_for
from mempalace.sources import PalaceContext, SourceNotFoundError, SourceRef, get_adapter
from mempalace.sources.livekit_calls import LivekitCallsSourceAdapter, _call_transcript

SUMMARY = "MODEL-WRITTEN SUMMARY that is not what anyone said"


def _record(call_id, status="completed", turns=None, **extra):
    rec = {
        "id": call_id,
        "direction": "outbound",
        "status": status,
        "started": 1_761_600_000.0,
        "task": "Ask the pharmacy whether the refill is ready",
        "summary": SUMMARY,
        "metrics": [{"audio_duration": 3.5}],
        "transcript": turns
        if turns is not None
        else [
            {"role": "assistant", "t": 0.1, "text": "Hi, I'm calling on behalf of JP."},
            {"role": "user", "t": 1.2, "text": "Sure, the zebrafish refill is ready for pickup."},
            {"role": "assistant", "t": 2.0, "text": "Thank you, I'll let him know."},
            {"role": "user", "t": 3.1, "text": "Anything else today?"},
            {"role": "assistant", "t": 3.9, "text": "No, that's all. Goodbye."},
        ],
    }
    rec.update(extra)
    return rec


@pytest.fixture
def calls(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret-call.json").write_text(json.dumps(_record("secret")))
    monkeypatch.setattr(livekit_calls, "_EXCLUDED_ROOTS", (str(private),))
    d = tmp_path / "calls"
    d.mkdir()
    (d / "out-1.json").write_text(json.dumps(_record("out-1")))
    (d / "live.json").write_text(json.dumps(_record("live", status="in-progress")))
    (d / "silent.json").write_text(json.dumps(_record("silent", turns=[])))
    (d / "broken.json").write_text("{not json")
    (d / "linked.json").symlink_to(private / "secret-call.json")
    return d


def _ingest(path):
    adapter = LivekitCallsSourceAdapter()
    return list(adapter.ingest(source=SourceRef(local_path=str(path)), palace=None))


def test_registered_and_sensitive():
    adapter = get_adapter("livekit-calls")
    assert adapter.default_privacy_class == "sensitive"
    assert _drawer_collection_name_for(adapter) == "mempalace_private_drawers"


def test_only_final_spoken_calls_outside_excluded_roots(calls):
    drawers = _ingest(calls)
    assert {d.metadata["call_id"] for d in drawers} == {"out-1"}
    assert all(d.source_file == str(calls / "out-1.json") for d in drawers)
    assert all(d.metadata["wing"] == "private_calls" for d in drawers)
    assert all(d.metadata["privacy_class"] == "sensitive" for d in drawers)


def test_keeps_spoken_words_and_drops_model_text():
    text = _call_transcript(_record("x"))
    assert "> Sure, the zebrafish refill is ready for pickup." in text
    assert "Hi, I'm calling on behalf of JP." in text
    # The task is the request the call was placed for, not something said on it.
    assert "Ask the pharmacy" not in text
    assert SUMMARY not in text
    unspoken = _call_transcript(
        _record(
            "z",
            turns=[
                {"role": "user", "t": 0, "text": "Hello?"},
                {"role": "system", "t": 0, "text": "SYSTEM PROMPT TEXT"},
                {"t": 0, "text": "ROLELESS TEXT"},
                {"role": "assistant", "t": 1, "text": "Hi there."},
            ],
        )
    )
    assert "SYSTEM PROMPT TEXT" not in unspoken and "ROLELESS TEXT" not in unspoken
    assert "> Hello?" in unspoken and "Hi there." in unspoken
    assert "audio_duration" not in text
    assert _call_transcript(_record("y", turns=[{"role": "user", "t": 0, "text": "  "}])) is None


def test_refuses_an_excluded_root(calls, tmp_path):
    with pytest.raises(SourceNotFoundError):
        _ingest(tmp_path / "private")


def test_mine_files_into_the_private_collection_only(calls, tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import PRIVATE_COLLECTION_NAME, get_collection

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    palace = str(tmp_path / "palace")
    written = mine_source_adapter(
        source_name="livekit-calls", source_path=str(calls), palace_path=palace
    )
    private = get_collection(palace, collection_name=PRIVATE_COLLECTION_NAME, create=False)
    assert written == private.count() > 0
    got = private.get(include=["documents", "metadatas"])
    assert any("zebrafish" in doc for doc in got["documents"])
    assert {m["wing"] for m in got["metadatas"]} == {"private_calls"}

    # The drawers collection every search path opens holds none of it.
    drawers = get_collection(palace)
    assert drawers.get(where={"wing": "private_calls"})["ids"] == []
    assert not any("zebrafish" in doc for doc in drawers.get(include=["documents"])["documents"])


def test_sensitive_adapter_cannot_write_the_shared_knowledge_graph(tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.sources import register, unregister

    class _TripleWriter(LivekitCallsSourceAdapter):
        name = "test-sensitive-triples"

        def ingest(self, *, source, palace: PalaceContext):
            palace.knowledge_graph.add_triple("JP", "takes", "medication")
            yield from ()

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    register(_TripleWriter.name, _TripleWriter)
    try:
        with pytest.raises(SensitiveKnowledgeGraphWrite):
            mine_source_adapter(
                source_name=_TripleWriter.name,
                source_path=str(tmp_path),
                palace_path=str(tmp_path / "palace"),
            )
    finally:
        unregister(_TripleWriter.name)


def test_task_is_metadata_not_content(calls):
    drawers = _ingest(calls)
    assert {d.metadata["call_task"] for d in drawers} == {
        "Ask the pharmacy whether the refill is ready"
    }


def test_malformed_records_are_skipped_without_stopping_the_mine(calls):
    (calls / "zz-bad-status.json").write_text(json.dumps(_record("bad-status", status=[])))
    (calls / "zz-nan-start.json").write_text(json.dumps(_record("nan", started=float("nan"))))
    ids = {d.metadata["call_id"] for d in _ingest(calls)}
    assert ids == {"out-1", "nan"}
    nan = [d for d in _ingest(calls) if d.metadata["call_id"] == "nan"]
    assert nan and all(d.metadata["call_started"] == "" for d in nan)


def test_a_symlink_swapped_in_after_listing_is_not_followed(calls, tmp_path):
    record = calls / "out-1.json"
    record.unlink()
    record.symlink_to(tmp_path / "private" / "secret-call.json")
    with pytest.raises(OSError):
        livekit_calls._read_record(record)


def test_a_sensitive_record_is_refused_outside_the_private_collection(calls, tmp_path, monkeypatch):
    from mempalace.palace import get_collection
    from mempalace.sources import SensitiveRecordOutsidePrivateCollection

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    shared = get_collection(str(tmp_path / "palace"))
    ctx = PalaceContext(
        drawer_collection=shared, knowledge_graph=None, palace_path=str(tmp_path / "palace")
    )
    record = _ingest(calls)[0]
    with pytest.raises(SensitiveRecordOutsidePrivateCollection):
        ctx.upsert_drawer(record)
    assert shared.count() == 0


def test_the_drawers_collection_cannot_be_configured_as_the_private_one(tmp_path, monkeypatch):
    from mempalace.palace import get_collection

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MEMPALACE_COLLECTION_NAME", "mempalace_private_drawers")
    with pytest.raises(ValueError, match="reserved for sensitive sources"):
        get_collection(str(tmp_path / "palace"))


def test_the_legacy_hub_path_files_privately_too(calls, tmp_path, monkeypatch):
    import argparse

    from mempalace import cli
    from mempalace.palace import PRIVATE_COLLECTION_NAME, get_collection

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    palace = str(tmp_path / "palace")
    cli._mine_via_adapter(
        argparse.Namespace(
            source="livekit-calls",
            dir=str(calls),
            wing=None,
            palace=palace,
            dry_run=False,
            limit=0,
            agent="test",
        )
    )
    private = get_collection(palace, collection_name=PRIVATE_COLLECTION_NAME, create=False)
    assert private.count() > 0
    assert get_collection(palace).get(where={"wing": "private_calls"})["ids"] == []


def test_search_finds_shared_drawers_but_never_the_call(calls, tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import get_collection
    from mempalace.searcher import search_memories

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    palace = str(tmp_path / "palace")
    mine_source_adapter(source_name="livekit-calls", source_path=str(calls), palace_path=palace)
    # Positive control: search does see the drawers collection.
    get_collection(palace).upsert(
        ids=["shared-1"],
        documents=["Notes on the zebrafish aquarium filter and its refill schedule."],
        metadatas=[{"wing": "notes", "room": "general", "source_file": "/notes/fish.md"}],
    )
    hits = search_memories("zebrafish refill ready for pickup", palace, n_results=10)["results"]
    assert any("aquarium" in json.dumps(h) for h in hits)
    assert not any("ready for pickup" in json.dumps(h) for h in hits)


def test_a_dry_run_counts_the_calls_and_writes_nothing(calls, tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    palace = tmp_path / "palace"
    n = mine_source_adapter(
        source_name="livekit-calls", source_path=str(calls), palace_path=str(palace), dry_run=True
    )
    assert n == len(_ingest(calls)) > 0
    assert not palace.exists()
