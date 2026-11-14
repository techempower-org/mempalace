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
        nightly.load_decisions(bad, first_run=False)


def test_a_first_run_creates_the_decisions_store_once_and_a_later_loss_fails(tmp_path):
    store = tmp_path / "d" / "decisions.json"
    assert nightly.load_decisions(store, first_run=True) == {}
    assert store.read_text() == "{}\n"
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o640
    store.write_text('{"x": {"decision": "reject"}}')
    assert nightly.load_decisions(store, first_run=True) == {"x": {"decision": "reject"}}
    store.unlink()
    with pytest.raises(nightly.DecisionsMissing):
        nightly.load_decisions(store, first_run=False)
    store.symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(nightly.DecisionsMissing):
        nightly.load_decisions(store, first_run=True)


@pytest.mark.parametrize("day", ["../decisions", "/etc/passwd", "2026-13-01", "2026-1-1", "x"])
def test_a_day_that_is_not_a_canonical_date_writes_nothing(tmp_path, day):
    out = tmp_path / "dreams"
    with pytest.raises(ValueError):
        nightly.publish({"schema": "dreams/v1", "day": day, "proposals": []}, out)
    assert not out.exists() and not (tmp_path / "decisions.json").exists()


def test_the_final_document_with_its_truncation_note_fits(monkeypatch):
    props = [
        {
            "id": str(i),
            "kind": "no-mechanism",
            "summary": "x" * 50,
            "sources": [],
            "authority": "",
            "detail": {},
        }
        for i in range(60)
    ]
    base = {"run": {}, "proposals": props}
    for limit in range(1500, 4000, 37):
        monkeypatch.setattr(nightly, "MAX_BYTES", limit)
        doc = nightly.within_size(base)
        assert len((json.dumps(doc, indent=2) + "\n").encode()) <= limit


def test_a_document_too_large_without_proposals_is_refused(monkeypatch):
    monkeypatch.setattr(nightly, "MAX_BYTES", 500)
    doc = {
        "run": {},
        "applied": [{"id": str(i), "pr": "u" * 40} for i in range(20)],
        "proposals": [],
    }
    with pytest.raises(nightly.TooLarge):
        nightly.within_size({**doc, "proposals": [{"id": "1"}]})


def test_publish_writes_the_ledger_before_latest(tmp_path, monkeypatch):
    order = []
    real = nightly._atomic_write
    monkeypatch.setattr(
        nightly,
        "_atomic_write",
        lambda path, text, mode=0o640: (order.append(path.name), real(path, text, mode)),
    )
    nightly.publish(
        {"schema": "dreams/v1", "day": "2026-11-12", "proposals": []}, tmp_path / "o", {}
    )
    assert order == [nightly.SEEN, "2026-11-12.json", "latest.json"]


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


@pytest.mark.parametrize("day", ["../x", "", "2026-02-30"])
def test_the_cli_refuses_a_bad_day_before_creating_anything(tmp_path, day):
    root, md = _tree(tmp_path)
    out = tmp_path / "o"
    with pytest.raises(ValueError):
        main(
            [
                "nightly",
                "--memory-root",
                str(root),
                "--claude-md",
                str(md),
                "--out",
                str(out),
                "--day",
                day,
                "--min-available-mb",
                "0",
            ]
        )
    assert not out.exists()


def test_a_first_run_store_lands_in_a_private_folder(tmp_path):
    store = tmp_path / "fresh" / "decisions.json"
    old = os.umask(0o022)
    try:
        nightly.load_decisions(store, first_run=True)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(store.parent).st_mode) == 0o700


def test_run_treats_an_empty_day_as_an_error_not_today(tmp_path):
    root, md = _tree(tmp_path)
    with pytest.raises(ValueError):
        nightly.run(root, [md], _embed, None, {}, day="")


def _linked_pair(tmp_path):
    """One memory reached through a symlinked folder, as the night before the fix proposed it."""
    real = tmp_path / "projects" / "-home-jp-Projects-x" / "memory"
    real.mkdir(parents=True)
    (real / "m.md").write_text(
        "---\nname: m\ndescription: d\nmetadata:\n  type: feedback\n---\nb\n"
    )
    link = tmp_path / "projects" / "-home-jp-srv-x"
    link.mkdir()
    (link / "memory").symlink_to("../-home-jp-Projects-x/memory")
    return {
        "id": "0154343f2d6447e0",
        "kind": "near-duplicate",
        "sources": [str(real / "m.md"), str(link / "memory" / "m.md")],
    }


def test_an_accepted_pair_that_is_one_file_is_applied_and_logged(tmp_path):
    prop = _linked_pair(tmp_path)
    dec = {
        prop["id"]: {"decision": "accept", "by": "JP", "at": "2026-11-14T03:34:26-08:00"},
        "aaaaaaaaaaaaaaaa": {"decision": "accept", "by": "JP", "at": "t"},
        "bbbbbbbbbbbbbbbb": {"decision": "reject", "by": "JP", "at": "t"},
        "cccccccccccccccc": {"decision": "accept", "by": "JP", "at": "t"},
    }
    other = {"id": "aaaaaaaaaaaaaaaa", "kind": "no-mechanism", "sources": ["x:1"]}
    old = [{"id": "zz", "result": "earlier"}]
    ledger, waiting = nightly.apply_decisions(
        dec, old, {prop["id"]: prop, other["id"]: other}, now="N"
    )
    assert ledger[0] == old[0] and len(ledger) == 2
    entry = ledger[1]
    assert entry["id"] == prop["id"] and entry["by"] == "JP" and entry["applied_at"] == "N"
    assert "nothing to merge" in entry["result"]
    assert (tmp_path / "projects" / "-home-jp-Projects-x" / "memory" / "m.md").is_file()
    assert {w["id"]: w["reason"] for w in waiting} == {
        "aaaaaaaaaaaaaaaa": "needs a lane: not mechanical",
        "cccccccccccccccc": "not in any published night",
    }
    # Applied once: a second night adds nothing.
    again, _ = nightly.apply_decisions(dec, ledger, {prop["id"]: prop}, now="M")
    assert again == ledger


def test_two_real_files_are_never_applied_mechanically(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("same\n")
    b.write_text("same\n")
    prop = {"id": "dddddddddddddddd", "kind": "near-duplicate", "sources": [str(a), str(b)]}
    ledger, waiting = nightly.apply_decisions(
        {prop["id"]: {"decision": "accept"}}, [], {prop["id"]: prop}
    )
    assert ledger == [] and waiting[0]["reason"] == "needs a lane: not mechanical"


def test_the_cli_applies_a_decision_from_an_earlier_night(tmp_path, monkeypatch):
    root, md = _tree(tmp_path)
    prop = _linked_pair(tmp_path / "mem")
    out = tmp_path / "o"
    out.mkdir()
    (out / "2026-11-13.json").write_text(json.dumps({"schema": "dreams/v1", "proposals": [prop]}))
    (out / "decisions.json").write_text(
        json.dumps({prop["id"]: {"decision": "accept", "by": "JP", "at": "t"}})
    )
    (out / nightly.SEEN).write_text("{}")
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
            "2026-11-14",
            "--no-model",
            "--min-available-mb",
            "0",
        ]
    )
    assert rc == 0
    doc = json.loads((out / "latest.json").read_text())
    assert [a["id"] for a in doc["applied"]] == [prop["id"]]
    assert json.loads((out / nightly.APPLIED).read_text()) == doc["applied"]
    assert "accepted_waiting" not in doc["run"]


def test_a_judge_that_is_off_does_not_fail_the_night(tmp_path, monkeypatch):
    import urllib.error

    # A project rule and a memory saved in that project, so the pass has pairs to wait.
    proj = tmp_path / "repo"
    proj.mkdir()
    md = proj / "CLAUDE.md"
    md.write_text(RULES)
    root = tmp_path / "projects"
    folder = root / nightly.contradictions._project_key(proj.resolve()) / "memory"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  type: feedback\n---\nx\n"
    )
    out = tmp_path / "o"
    monkeypatch.setattr(nightly.inventory, "daemon_embedder", lambda: _embed)

    def judge():
        def j(rule, memory):
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        return j

    monkeypatch.setattr(nightly.contradictions, "local_judge", judge)
    monkeypatch.setattr("mempalace.kg_delete_queue.sweep_from_env", lambda: {"skipped": "test"})
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
            "2026-11-14",
            "--min-available-mb",
            "0",
        ]
    )
    con = json.loads((out / "latest.json").read_text())["run"]["steps"]["contradictions"]
    assert rc == 0 and con.get("judge") == "off" and con["error"] is None
    assert con["waiting"] == con["pairs"] > 0


def test_two_real_folders_linking_one_file_are_not_applied(tmp_path):
    shared = tmp_path / "shared.md"
    shared.write_text("x\n")
    a, b = tmp_path / "p" / "memory", tmp_path / "q" / "memory"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "m.md").symlink_to(shared)
    (b / "m.md").symlink_to(shared)
    prop = {
        "id": "eeeeeeeeeeeeeeee",
        "kind": "near-duplicate",
        "sources": [str(a / "m.md"), str(b / "m.md")],
    }
    ledger, waiting = nightly.apply_decisions(
        {prop["id"]: {"decision": "accept"}}, [], {prop["id"]: prop}
    )
    assert ledger == [] and waiting[0]["reason"] == "needs a lane: not mechanical"


def test_a_second_run_while_one_holds_the_lock_does_not_start(tmp_path, monkeypatch, capsys):
    root, md = _tree(tmp_path)
    out = tmp_path / "o"
    out.mkdir()
    held = nightly.single_writer(out)
    assert held is not None
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
            "2026-11-14",
            "--no-model",
            "--min-available-mb",
            "0",
        ]
    )
    assert rc == 4 and "another dream run" in capsys.readouterr().out
    assert not (out / "latest.json").exists()
    held.close()
    assert nightly.single_writer(out) is not None
