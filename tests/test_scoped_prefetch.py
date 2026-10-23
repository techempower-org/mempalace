"""Scoped prefetch: a small mine asks the index, not the whole palace (#548).

``prefetch_mined_set(source_files=...)`` and ``prefetch_content_hashes(wing=...)``
must return exactly what the whole-palace scan returns, restricted to the
scope, and must never fall back to that scan. Both page, so memory
holds one page however large the scope. ``mine_convos`` takes the scoped
path for a target of at most ``SCOPED_PREFETCH_MAX_FILES`` files.
"""

import chromadb
import pytest

import mempalace.convo_miner as convo_miner
from mempalace.palace import (
    CONVO_CHUNKER_VERSION,
    NORMALIZE_VERSION,
    _iter_scoped_metadata,
    prefetch_content_hashes,
    prefetch_mined_set,
)

MTIME = 1_700_000_000.0


def _meta(src, wing="alpha", **extra):
    meta = {
        "wing": wing,
        "room": "general",
        "source_file": src,
        "chunk_index": 0,
        "extract_mode": "exchange",
        "normalize_version": NORMALIZE_VERSION,
        "convo_chunker_version": CONVO_CHUNKER_VERSION,   # an exchange drawer without it is stale (upstream #2591)
        "source_mtime": MTIME,
    }
    meta.update(extra)
    return meta


@pytest.fixture
def col(tmp_path):
    client = chromadb.PersistentClient(path=str(tmp_path / "palace"))
    collection = client.get_or_create_collection("mempalace_drawers")
    rows = {
        # complete: 2 of chunk_total 2
        "a0": _meta("/s/complete", chunk_index=0, chunk_total=2, content_hash="h1"),
        "a1": _meta("/s/complete", chunk_index=1, chunk_total=2, content_hash="h1"),
        # partial: 1 of chunk_total 3, must stay unmined
        "b0": _meta("/s/partial", chunk_total=3, content_hash="h2"),
        # legacy: no mtime, no chunk_total
        "c0": {
            k: v for k, v in _meta("/s/legacy", content_hash="h3,h4").items() if k != "source_mtime"
        },
        # stale schema: ignored by both
        "d0": _meta("/s/old", normalize_version=0, content_hash="h5"),
        # other extract mode: ignored for exchange
        "e0": _meta("/s/general", extract_mode="general", content_hash="h6"),
        # same hash, other wing: never a key for alpha
        "f0": _meta("/s/beta", wing="beta", content_hash="h1,h7"),
    }
    collection.upsert(
        ids=list(rows),
        documents=[f"doc {i}" for i in rows],
        metadatas=list(rows.values()),
    )
    return collection


class _Spy:
    """Wraps a collection; records how the palace was read."""

    def __init__(self, inner):
        self._inner = inner
        self.unfiltered_gets = 0
        self.scoped = []  # (where, limit, offset) per filtered get

    def count(self):
        return self._inner.count()

    def get(self, **kwargs):
        if not kwargs.get("where"):
            self.unfiltered_gets += 1
        else:
            self.scoped.append((kwargs["where"], kwargs.get("limit"), kwargs.get("offset")))
        return self._inner.get(**kwargs)

    def get_all_metadata(self, where=None):  # pragma: no cover - must stay unused
        raise AssertionError("scoped prefetch must page, not load the whole match")


SCOPES = [
    ["/s/complete", "/s/partial", "/s/legacy", "/s/old", "/s/general", "/s/beta"],
    ["/s/complete"],
    ["/s/partial", "/s/nowhere"],
    [],
]


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("mode", ["exchange", "general", None])
def test_scoped_mined_set_is_the_full_scan_restricted(col, scope, mode):
    full = prefetch_mined_set(col, extract_mode=mode)
    scoped = prefetch_mined_set(col, extract_mode=mode, source_files=scope)
    assert scoped == {src: mtime for src, mtime in full.items() if src in scope}


def test_scoped_mined_set_keeps_the_completeness_rule(col):
    mined = prefetch_mined_set(col, extract_mode="exchange", source_files=SCOPES[0])
    assert mined == {"/s/complete": MTIME, "/s/legacy": None, "/s/beta": MTIME}


@pytest.mark.parametrize("wing", ["alpha", "beta", "nowhere"])
@pytest.mark.parametrize("mode", ["exchange", "general", None])
def test_scoped_content_hashes_are_the_full_scan_for_the_wing(col, wing, mode):
    full = prefetch_content_hashes(col, extract_mode=mode)
    scoped = prefetch_content_hashes(col, extract_mode=mode, wing=wing)
    assert scoped == {key: src for key, src in full.items() if key[0] == wing}


def test_scoped_content_hashes_split_joined_hashes(col):
    hashes = prefetch_content_hashes(col, extract_mode="exchange", wing="alpha")
    assert hashes == {
        ("alpha", "h1"): "/s/complete",
        ("alpha", "h2"): "/s/partial",
        ("alpha", "h3"): "/s/legacy",
        ("alpha", "h4"): "/s/legacy",
    }


def test_scoped_prefetch_never_scans_the_palace(col):
    spy = _Spy(col)
    prefetch_mined_set(spy, extract_mode="exchange", source_files=["/s/complete", "/s/complete"])
    prefetch_content_hashes(spy, extract_mode="exchange", wing="alpha")
    assert spy.unfiltered_gets == 0
    # One paged lookup per distinct file, one per wing; every read is paged.
    starts = [where for where, _, offset in spy.scoped if not offset]
    assert starts == [{"source_file": "/s/complete"}, {"wing": "alpha"}]
    assert all(limit for _, limit, _ in spy.scoped)


def test_scoped_pages_hold_one_page_at_a_time(col):
    pages = list(_iter_scoped_metadata(col, {"wing": "alpha"}, page_size=2))
    assert [len(page) for page in pages] == [2, 2, 2]
    assert sorted(m["source_file"] for page in pages for m in page) == sorted(
        m["source_file"]
        for m in col.get(where={"wing": "alpha"}, include=["metadatas"])["metadatas"]
    )


def test_unscoped_prefetch_still_scans(col):
    spy = _Spy(col)
    prefetch_mined_set(spy, extract_mode="exchange")
    prefetch_content_hashes(spy, extract_mode="exchange")
    assert spy.unfiltered_gets >= 2
    assert spy.scoped == []


def _write_convos(directory, n):
    directory.mkdir()
    for i in range(n):
        (directory / f"chat{i}.txt").write_text(
            f"> Question {i} about the palace?\nAnswer {i} is verbatim storage.\n\n"
            f"> Follow-up {i}?\nIt stays local and searchable.\n"
        )


def _spy_prefetch(monkeypatch):
    calls = {}
    real_mined, real_hashes = convo_miner.prefetch_mined_set, convo_miner.prefetch_content_hashes

    def mined(collection, **kwargs):
        calls["source_files"] = kwargs.get("source_files")
        return real_mined(collection, **kwargs)

    def hashes(collection, **kwargs):
        calls["wing"] = kwargs.get("wing")
        return real_hashes(collection, **kwargs)

    monkeypatch.setattr(convo_miner, "prefetch_mined_set", mined)
    monkeypatch.setattr(convo_miner, "prefetch_content_hashes", hashes)
    return calls


def test_small_mine_scopes_its_prefetch(tmp_path, monkeypatch):
    convos = tmp_path / "convos"
    _write_convos(convos, 2)
    calls = _spy_prefetch(monkeypatch)
    convo_miner.mine_convos(str(convos), str(tmp_path / "palace"), wing="w")
    assert sorted(calls["source_files"]) == sorted(
        str(p.resolve()) for p in convos.glob("chat*.txt")
    )
    assert calls["wing"] == "w"


def test_large_mine_keeps_the_full_scan(tmp_path, monkeypatch):
    convos = tmp_path / "convos"
    _write_convos(convos, 2)
    monkeypatch.setattr(convo_miner, "SCOPED_PREFETCH_MAX_FILES", 1)
    calls = _spy_prefetch(monkeypatch)
    convo_miner.mine_convos(str(convos), str(tmp_path / "palace"), wing="w")
    assert calls == {"source_files": None, "wing": None}


def test_small_remine_skips_what_the_scoped_prefetch_found(tmp_path, capsys):
    convos = tmp_path / "convos"
    _write_convos(convos, 2)
    palace = str(tmp_path / "palace")
    convo_miner.mine_convos(str(convos), palace, wing="w")
    count = chromadb.PersistentClient(path=palace).get_collection("mempalace_drawers").count()
    capsys.readouterr()
    convo_miner.mine_convos(str(convos), palace, wing="w")
    out = capsys.readouterr().out
    assert "Files skipped (already filed): 2" in out
    assert (
        chromadb.PersistentClient(path=palace).get_collection("mempalace_drawers").count() == count
    )
