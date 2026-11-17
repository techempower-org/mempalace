"""Routing dream proposals to the inbox of the project they name (synthetic fixtures only)."""

from __future__ import annotations

import json
import os
import re
import subprocess

from mempalace.dream import nightly, route
from mempalace.dream.__main__ import main


def _enc(path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _projects(tmp_path):
    """A projects root with two projects (one a git repo ignoring scratch/) and a memory root
    whose folders are named the way the harness names them: the encoded project path."""
    root = tmp_path / "Projects"
    alpha, beta = root / "alpha.site", root / "beta"
    for p in (alpha, beta):
        p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(beta)], check=True)
    (beta / ".gitignore").write_text("scratch/\n")
    mem = tmp_path / "mem"
    for p in (alpha, beta):
        (mem / _enc(p) / "memory").mkdir(parents=True)
    return root, mem, alpha, beta


def _prop(pid, kind, sources, summary="Add the hook that enforces it. More words after."):
    return {"id": pid, "kind": kind, "sources": sources, "summary": summary, "detail": {}}


def test_a_source_names_its_project_by_memory_folder_or_by_path(tmp_path):
    root, mem, alpha, beta = _projects(tmp_path)
    m = str(mem / _enc(alpha) / "memory" / "x.md")
    assert route.project_of(m, [root]) == alpha
    assert route.project_of(str(beta / "CLAUDE.md") + ":12", [root]) == beta
    assert route.project_of(str(tmp_path / ".claude" / "CLAUDE.md:3"), [root]) is None
    assert route.project_of(str(mem / "-home-someone-else" / "memory" / "y.md"), [root]) is None


def test_each_project_gets_one_line_per_proposal_and_the_ledger_records_it(tmp_path):
    root, mem, alpha, beta = _projects(tmp_path)
    props = [
        _prop("a1", "no-mechanism", [str(mem / _enc(alpha) / "memory" / "r.md")]),
        _prop(
            "ab",
            "near-duplicate",
            [str(mem / _enc(alpha) / "memory" / "d.md"), str(beta / "CLAUDE.md")],
        ),
        _prop("g1", "no-mechanism", [str(tmp_path / "CLAUDE.md:8")]),
    ]
    ledger: dict = {}
    stats = route.route(props, [root], ledger, day="2026-11-17", lanes={"beta": "beta-lane"})
    a = (alpha / route.INBOX).read_text().splitlines()
    b = (beta / route.INBOX).read_text().splitlines()
    lines_a = [ln for ln in a if ln.startswith("- [ ] ")]
    lines_b = [ln for ln in b if ln.startswith("- [ ] ")]
    assert len(lines_a) == 2 and len(lines_b) == 1
    assert lines_b[0].startswith("- [ ] beta-lane · ab · ")
    assert any(
        ln.startswith("- [ ] alpha.site · a1 · Add the hook that enforces it.") for ln in lines_a
    )
    assert "More words" not in "\n".join(lines_a)  # one sentence
    rec = ledger[route.KEY]
    assert set(rec) == {"a1", "ab"}
    assert {r["project"] for r in rec["ab"]["to"]} == {"alpha.site", "beta"}
    assert stats["routed"] == 2 and stats["new"] == 2 and stats["unrouted"] == 1
    assert stats["acted"] == 0
    assert os.stat(alpha / route.INBOX).st_mode & 0o077 == 0


def test_a_second_night_adds_nothing_twice_and_marks_decided_or_gone_as_acted(tmp_path):
    root, mem, alpha, _beta = _projects(tmp_path)
    src = lambda n: [str(mem / _enc(alpha) / "memory" / f"{n}.md")]  # noqa: E731
    ledger: dict = {}
    route.route(
        [
            _prop("p1", "no-mechanism", src("1")),
            _prop("p2", "no-mechanism", src("2")),
            _prop("p3", "no-mechanism", src("3")),
        ],
        [root],
        ledger,
        day="2026-11-17",
    )
    # night 2: p1 decided on the page, p2 fixed by the lane (no longer proposed), p3 still open
    stats = route.route(
        [_prop("p3", "no-mechanism", src("3"))],
        [root],
        ledger,
        day="2026-11-18",
        decisions={"p1": {"decision": "reject"}},
    )
    lines = [ln for ln in (alpha / route.INBOX).read_text().splitlines() if ln.startswith("- [")]
    assert len(lines) == 3
    assert stats == {**stats, "routed": 3, "new": 0, "acted": 2, "open": 1}
    assert ledger[route.KEY]["p1"]["acted"] == {"how": "decided: reject", "day": "2026-11-18"}
    assert ledger[route.KEY]["p2"]["acted"] == {"how": "no longer proposed", "day": "2026-11-18"}
    assert ledger[route.KEY]["p3"]["acted"] is None
    assert route.summary(ledger) == "dreams: 3 proposals routed, 2 acted"


def test_a_tracked_scratch_folder_is_never_written(tmp_path):
    root = tmp_path / "Projects"
    gamma = root / "gamma"
    gamma.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(gamma)], check=True)  # scratch/ NOT ignored
    ledger: dict = {}
    stats = route.route(
        [_prop("c1", "no-mechanism", [str(gamma / "CLAUDE.md:2")])],
        [root],
        ledger,
        day="2026-11-17",
    )
    assert not (gamma / "scratch").exists()
    assert stats["routed"] == 0 and stats["skipped"] == ["gamma: scratch/ is not git-ignored"]


def test_a_symlinked_inbox_is_not_followed(tmp_path):
    root, mem, alpha, _beta = _projects(tmp_path)
    (alpha / "scratch").mkdir()
    target = tmp_path / "elsewhere.md"
    target.write_text("keep\n")
    (alpha / route.INBOX).symlink_to(target)
    stats = route.route(
        [_prop("s1", "no-mechanism", [str(alpha / "CLAUDE.md:1")])], [root], {}, day="2026-11-17"
    )
    assert target.read_text() == "keep\n"
    assert stats["routed"] == 0 and stats["skipped"] == ["alpha.site: inbox is not a regular file"]


def test_the_judge_never_prunes_the_routing_record(tmp_path, monkeypatch):
    root, mem, alpha, _beta = _projects(tmp_path)
    ledger = {route.KEY: {"x": {"to": [], "acted": None}}, "stalepair": {"verdict": "consistent"}}
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Rules\n- **Concise.** No fluff.\n")

    def judge(*_a, **_k):
        raise AssertionError("no pairs expected")

    nightly.run(
        mem, [md], lambda t: [[1.0, 0.0] for _ in t], judge, {}, day="2026-11-17", verdicts=ledger
    )
    assert route.KEY in ledger and "stalepair" not in ledger


def test_the_cli_routes_when_asked_and_reports_the_line(tmp_path, monkeypatch, capsys):
    root, mem, alpha, _beta = _projects(tmp_path)
    folder = mem / _enc(alpha) / "memory"
    for name in ("a", "b"):
        (folder / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: d\nmetadata:\n  type: feedback\n---\nsame\n"
        )
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Rules\n- **Concise.** No fluff.\n")
    out = tmp_path / "o"
    monkeypatch.setattr(
        nightly.inventory, "daemon_embedder", lambda: lambda t: [[1.0, 0.0] for _ in t]
    )
    argv = [
        "nightly",
        "--memory-root",
        str(mem),
        "--claude-md",
        str(md),
        "--projects-root",
        str(root),
        "--out",
        str(out),
        "--day",
        "2026-11-17",
        "--no-model",
        "--min-available-mb",
        "0",
    ]
    assert main(argv) == 0
    assert not (alpha / route.INBOX).exists()  # off unless asked
    assert main([*argv, "--route"]) == 0
    assert (alpha / route.INBOX).is_file()
    doc = json.loads((out / "latest.json").read_text())
    assert doc["run"]["steps"]["routing"]["routed"] >= 1
    judged = json.loads((out / nightly.JUDGED).read_text())
    assert judged[route.KEY]
    capsys.readouterr()
    assert main(["routed", "--out", str(out)]) == 0
    assert re.fullmatch(r"dreams: \d+ proposals routed, \d+ acted\n", capsys.readouterr().out)
