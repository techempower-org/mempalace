"""The nightly dream document (dreams/v1) and how it is published (synthetic fixtures only)."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat

import pytest

from mempalace.dream import nightly
from mempalace.dream.__main__ import main
from mempalace.dream.inventory import Proposal

RULES = """# Rules
- **Never push to main.** Use a branch.
- **Concise.** No fluff.
"""


def _tree(tmp_path):
    root = tmp_path / "projects"
    folder = root / "p" / "memory"
    folder.mkdir(parents=True)
    for name in ("a", "b"):
        (folder / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: d\nmetadata:\n  type: feedback\n---\nsame\n"
        )
    (folder / "MEMORY.md").write_text("- x\n" * 250)
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_text(RULES)
    return root, md


def _embed(texts):
    return [[1.0, 0.0] for _ in texts]


def test_ids_are_stable_across_line_shifts_and_differ_by_finding():
    a = Proposal("no-mechanism", "s", ["/x/CLAUDE.md:5"], detail={"lead": "Concise."})
    moved = Proposal("no-mechanism", "other words", ["/x/CLAUDE.md:9"], detail={"lead": "Concise."})
    other = Proposal("no-mechanism", "s", ["/x/CLAUDE.md:5"], detail={"lead": "Kind."})
    pair = Proposal("near-duplicate", "s", ["/m/b.md", "/m/a.md"])
    swapped = Proposal("near-duplicate", "s", ["/m/a.md", "/m/b.md"])
    assert nightly.proposal_id(a) == nightly.proposal_id(moved)
    assert nightly.proposal_id(a) != nightly.proposal_id(other)
    assert nightly.proposal_id(pair) == nightly.proposal_id(swapped)
    assert len(nightly.proposal_id(a)) == 16


def test_the_document_has_the_agreed_shape_and_drops_decided_ids(tmp_path):
    root, md = _tree(tmp_path)
    judge = lambda r, m: {"verdict": "contradict", "why": "w"}  # noqa: E731
    doc, seen = nightly.run(
        root, [md], _embed, judge, {}, day="2026-11-12", global_dirs=[md.parent]
    )
    assert doc["schema"] == "dreams/v1" and doc["day"] == "2026-11-12"
    assert doc["applied"] == []
    kinds = [p["kind"] for p in doc["proposals"]]
    assert kinds[0] == "contradiction" and kinds[-1] == "no-mechanism"
    for p in doc["proposals"]:
        assert set(p) == {"id", "kind", "summary", "sources", "authority", "detail", "first_seen"}
        assert p["first_seen"] == "2026-11-12"
        assert all(isinstance(v, (str, int, float, bool)) for v in p["detail"].values())
    rejected = doc["proposals"][0]["id"]
    again, _ = nightly.run(
        root,
        [md],
        _embed,
        judge,
        {rejected: {"decision": "reject"}},
        day="2026-11-13",
        seen=seen,
        global_dirs=[md.parent],
    )
    assert rejected not in {p["id"] for p in again["proposals"]}
    assert again["run"]["decided_and_dropped"] == 1
    assert {p["first_seen"] for p in again["proposals"]} == {"2026-11-12"}


def test_proposals_are_cut_to_the_page_limits():
    p = {
        "id": "x",
        "kind": "no-mechanism",
        "summary": "s" * 5000,
        "sources": [f"/s{i}" for i in range(30)],
        "authority": "a",
        "detail": {**{f"k{i}": i for i in range(20)}, "nested": {"a": 1}, "none": None},
    }
    out = nightly.fit(p)
    assert len(out["summary"]) == nightly.MAX_SUMMARY
    assert len(out["sources"]) == nightly.MAX_SOURCES
    assert len(out["detail"]) == nightly.MAX_DETAIL_KEYS
    assert "nested" not in out["detail"] and "none" not in out["detail"]


def test_an_oversized_document_drops_the_lowest_priority_kinds_first(monkeypatch):
    monkeypatch.setattr(nightly, "MAX_BYTES", 6000)
    props = [
        {
            "id": str(i),
            "kind": k,
            "summary": "x" * 200,
            "sources": [],
            "authority": "",
            "detail": {},
        }
        for i, k in enumerate(["contradiction"] * 3 + ["no-mechanism"] * 40)
    ]
    doc = nightly.within_size({"run": {}, "proposals": props})
    assert len(json.dumps(doc, indent=2)) + 1 <= 6000
    assert [p["kind"] for p in doc["proposals"]][:3] == ["contradiction"] * 3
    assert doc["run"]["truncated"] > 0


def test_publish_is_private_atomic_and_prunes_only_old_day_files(tmp_path):
    out = tmp_path / "dreams"
    out.mkdir()
    old = out / "2026-10-01.json"
    old.write_text("{}")
    keep = out / "2026-11-01.json"
    keep.write_text("{}")
    decisions = out / "decisions.json"
    decisions.write_text("{}")
    doc = {"schema": "dreams/v1", "day": "2026-11-12", "proposals": []}
    path = nightly.publish(doc, out, {"abc": "2026-11-12"})
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o700
    for f in (path, out / "latest.json", out / nightly.SEEN):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o640
    assert json.loads((out / "latest.json").read_text()) == doc
    assert not old.exists() and keep.exists() and decisions.exists()
    assert not [p for p in out.iterdir() if p.name.startswith(".") and p.name != nightly.SEEN]


def test_the_memory_guard_reads_mem_available(tmp_path):
    info = tmp_path / "meminfo"
    info.write_text("MemTotal: 32000000 kB\nMemAvailable: 4000000 kB\n")
    assert "3906 MB" in nightly.memory_guard(6144, str(info))()
    info.write_text("MemAvailable: 13000000 kB\n")
    assert nightly.memory_guard(6144, str(info))() is None


def test_a_guard_stops_the_model_pass_before_the_next_call(tmp_path):
    root, md = _tree(tmp_path)
    calls = []

    def judge(r, m):
        calls.append(r)
        return {"verdict": "consistent", "why": ""}

    doc, _ = nightly.run(
        root, [md], _embed, judge, {}, guard=lambda: "low memory", global_dirs=[md.parent]
    )
    assert calls == []
    assert doc["run"]["steps"]["contradictions"]["error"] == "stopped: low memory"


def test_unreadable_decisions_fail_rather_than_re_propose(tmp_path):
    bad = tmp_path / "decisions.json"
    bad.write_text("[]")
    with pytest.raises(ValueError):
        nightly.load_decisions(bad)
    assert nightly.load_decisions(tmp_path / "missing.json") == {}


def test_the_cli_does_not_start_under_the_memory_floor(tmp_path, monkeypatch, capsys):
    root, md = _tree(tmp_path)
    monkeypatch.setattr(
        nightly, "memory_guard", lambda mb: lambda: "MemAvailable 100 MB < 12288 MB"
    )
    rc = main(
        [
            "nightly",
            "--memory-root",
            str(root),
            "--claude-md",
            str(md),
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert rc == 4
    assert not (tmp_path / "o").exists()
    assert "not starting" in capsys.readouterr().out


def test_the_cli_publishes_with_the_ledgers(tmp_path, monkeypatch):
    root, md = _tree(tmp_path)
    out = tmp_path / "o"
    out.mkdir()
    (out / nightly.APPLIED).write_text(json.dumps([{"id": "x", "pr": "u", "at": "t"}]))
    monkeypatch.setattr(nightly.inventory, "daemon_embedder", lambda: _embed)
    rc = main(
        [
            "nightly",
            "--memory-root",
            str(root),
            "--claude-md",
            str(md),
            "--out",
            str(out),
            "--day",
            "2026-11-12",
            "--no-model",
            "--min-available-mb",
            "0",
        ]
    )
    assert rc == 0
    doc = json.loads((out / "2026-11-12.json").read_text())
    assert doc["applied"] == [{"id": "x", "pr": "u", "at": "t"}]
    assert doc["run"]["steps"]["contradictions"] == {"skipped": True}
    seen = json.loads((out / nightly.SEEN).read_text())
    assert set(seen) == {p["id"] for p in doc["proposals"]}
    assert dt.date.fromisoformat(doc["day"])


def test_every_proposal_in_the_document_is_inside_the_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "MAX_SUMMARY", 40)
    root, md = _tree(tmp_path)
    doc, _ = nightly.run(root, [md], _embed, None, {}, day="2026-11-12")
    assert doc["proposals"]
    assert all(len(p["summary"]) <= 40 for p in doc["proposals"])
