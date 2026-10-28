"""Sensitive transcript chunks go to the private collection, never the drawers collection (#588).

A marked chunk, and every chunk continuing its tool output, is filed
verbatim into ``PRIVATE_COLLECTION_NAME``. A drawer filed before the marker
was known is moved on the next mine, and a re-mine of the growing session
classifies the same chunks the same way.
"""

import os

import chromadb
import pytest

import mempalace.convo_miner as convo_miner
from mempalace.sensitive import sensitive_chunk_flags

SECRET = "ZEBRAFISHLEDGER"


def test_flags_mark_the_chunk_and_its_continuation_only():
    chunks = [
        "> what is in the folder?\n[Bash] ls ~/notes\n→ a.md b.md",
        "[Bash] cat ~/private/budget.txt\n→ first line of the private file",
        f"more private output {SECRET}\nstill the same output",
        "end of that output\n[Bash] date\n→ Tue",
        "[Bash] uptime\n→ up 3 days",
        "> next question",
    ]
    assert sensitive_chunk_flags(chunks) == [False, True, True, True, False, False]
    # The "> next question" chunk is not held: a tool call ended the block before it.


def test_only_a_column_zero_tool_call_ends_a_continuation():
    chunks = [
        "[Read /home/jp/private/x.md]\n→ private start",
        "> a line quoted inside the private file",
        "  [Bash] an indented line inside the private file",
        "> a new question from the user",
        "[Bash] date\n→ Tue",
    ]
    assert sensitive_chunk_flags(chunks) == [True, True, True, True, False]


def test_config_markers_extend_the_defaults_and_bad_ones_are_skipped():
    assert sensitive_chunk_flags(["[Bash] cat ~/acme-call.txt"], ["acme-call", "(unclosed"]) == [
        True
    ]
    assert sensitive_chunk_flags(["[Bash] cat ~/acme-call.txt"]) == [False]


def test_a_call_id_alone_is_not_a_marker():
    # Reports link calls by id (the rule since 2026-10-27); only a paste is sensitive.
    assert sensitive_chunk_flags(["see call out-20261027-184316-e4fe on the Calls tab"]) == [False]
    assert sensitive_chunk_flags(['{"role": "user", "t": 1.2, "text": "hello"}']) == [True]


def _transcript(extra=""):
    filler = "the private file goes on " * 60
    return (
        "> Please check the notes folder for me.\n"
        "[Bash] ls ~/notes\n→ a.md b.md\nThe notes folder holds two files.\n\n"
        "> Now read the budget file.\n"
        f"[Bash] cat ~/private/budget.txt\n→ {filler}\n{SECRET} appears here\n{filler}\n"
        "[Bash] date\n→ Tue Oct 27\nDone reading.\n\n"
        "> Thanks, what is the weather like today?\n"
        "It is sunny and mild in the forecast for the afternoon.\n" + extra
    )


def _docs(palace, name):
    client = chromadb.PersistentClient(path=palace)
    try:
        return client.get_collection(name).get(include=["documents"])["documents"]
    except Exception:
        return []


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    convos = tmp_path / "convos"
    convos.mkdir()
    path = convos / "session.txt"
    path.write_text(_transcript())
    return convos, path, str(tmp_path / "palace")


def test_mine_files_the_sensitive_chunks_privately(session):
    convos, _path, palace = session
    convo_miner.mine_convos(str(convos), palace, wing="w")
    shared = _docs(palace, "mempalace_drawers")
    private = _docs(palace, "mempalace_private_drawers")
    assert any("sunny and mild" in d for d in shared)
    assert any("notes folder holds two files" in d for d in shared)
    assert not any(SECRET in d or "private file goes on" in d for d in shared)
    assert any(SECRET in d for d in private)


def test_a_shared_copy_is_moved_and_a_remine_keeps_it_private(session, monkeypatch):
    convos, path, palace = session
    # Mined before the marker was known: everything lands in the drawers collection.
    with monkeypatch.context() as m:
        m.setattr(
            convo_miner,
            "sensitive_chunk_flags",
            lambda contents, extra=(): [False] * len(list(contents)),
        )
        convo_miner.mine_convos(str(convos), palace, wing="w")
    assert any(SECRET in d for d in _docs(palace, "mempalace_drawers"))

    path.write_text(_transcript("\n> One more question about lunch.\nSandwiches are fine.\n"))
    os.utime(path, (os.path.getmtime(path) + 5,) * 2)
    convo_miner.mine_convos(str(convos), palace, wing="w")
    shared = _docs(palace, "mempalace_drawers")
    assert not any(SECRET in d for d in shared)
    assert any("Sandwiches are fine" in d for d in shared)
    assert sum(SECRET in d for d in _docs(palace, "mempalace_private_drawers")) == 1


def test_a_private_filing_failure_leaves_the_shared_drawers_untouched(session, monkeypatch):
    convos, path, palace = session
    with monkeypatch.context() as m:
        m.setattr(
            convo_miner,
            "sensitive_chunk_flags",
            lambda contents, extra=(): [False] * len(list(contents)),
        )
        convo_miner.mine_convos(str(convos), palace, wing="w")
    before = sorted(_docs(palace, "mempalace_drawers"))

    def boom(*args, **kwargs):
        raise RuntimeError("private collection unavailable")

    monkeypatch.setattr(convo_miner, "_file_sensitive_chunks", boom)
    os.utime(path, (os.path.getmtime(path) + 5,) * 2)
    with pytest.raises(Exception):
        convo_miner.mine_convos(str(convos), palace, wing="w")
    assert sorted(_docs(palace, "mempalace_drawers")) == before
