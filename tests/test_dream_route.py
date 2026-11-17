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
    root = tmp_path.resolve() / "Projects"
    alpha, beta = root / "alpha.site", root / "beta"
    for p in (alpha, beta):
        p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(beta)], check=True)
    (beta / ".gitignore").write_text("scratch/\n")
    mem = tmp_path.resolve() / "mem"
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
    root = tmp_path.resolve() / "Projects"
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
    assert stats["routed"] == 0 and stats["skipped"] == ["gamma: the inbox path is not git-ignored"]


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
    assert stats["routed"] == 0 and stats["skipped"][0].startswith(
        "alpha.site: cannot open the inbox"
    )


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


# --- review round 1 (xreview gpt-6.1-sol, 2026-11-17)


def test_a_source_cannot_climb_out_of_the_root_or_name_a_hidden_folder(tmp_path):
    root, _mem, _alpha, _beta = _projects(tmp_path)
    (root.parent / "private").mkdir()
    assert route.project_of(str(root / ".." / "private" / "CLAUDE.md"), [root]) is None
    assert route.project_of(str(root / ".git" / "x"), [root]) is None
    assert route.project_of("relative/beta/CLAUDE.md", [root]) is None


def test_a_symlinked_project_routes_to_its_real_folder_once(tmp_path):
    root, mem, alpha, _beta = _projects(tmp_path)
    (root / "alias").symlink_to(alpha)
    by_link = str(root / "alias" / "CLAUDE.md")
    by_mem = str(mem / _enc(root / "alias") / "memory" / "x.md")
    (mem / _enc(root / "alias") / "memory").mkdir(parents=True)
    assert route.project_of(by_link, [root]) == alpha
    assert route.project_of(by_mem, [root]) == alpha
    ledger: dict = {}
    route.route(
        [_prop("s2", "no-mechanism", [by_link, str(alpha / "CLAUDE.md")])],
        [root],
        ledger,
        day="2026-11-17",
    )
    lines = [ln for ln in (alpha / route.INBOX).read_text().splitlines() if ln.startswith("- [")]
    assert len(lines) == 1 and len(ledger[route.KEY]["s2"]["to"]) == 1


def test_a_memory_name_two_folders_share_names_neither(tmp_path):
    root, mem, alpha, _beta = _projects(tmp_path)
    (root / "alpha-site").mkdir()  # encodes like alpha.site
    assert route.project_of(str(mem / _enc(alpha) / "memory" / "x.md"), [root]) is None
    assert route.project_of(str(alpha / "CLAUDE.md"), [root]) == alpha  # a path still names it


def test_a_night_without_the_judge_does_not_mark_contradictions_acted(tmp_path):
    root, _mem, alpha, _beta = _projects(tmp_path)
    src = [str(alpha / "CLAUDE.md:1")]
    ledger: dict = {}
    route.route(
        [_prop("c1", "contradiction", src), _prop("n1", "no-mechanism", src)],
        [root],
        ledger,
        day="2026-11-17",
    )
    stats = route.route(
        [],
        [root],
        ledger,
        day="2026-11-18",
        complete_kinds={"no-mechanism", "near-duplicate", "oversized-index"},
    )
    assert ledger[route.KEY]["c1"]["acted"] is None
    assert ledger[route.KEY]["n1"]["acted"]["how"] == route.GONE
    assert stats["acted"] == 1
    # n1 comes back: open again
    route.route([_prop("n1", "no-mechanism", src)], [root], ledger, day="2026-11-19")
    assert ledger[route.KEY]["n1"]["acted"] is None


def test_the_nightly_run_tells_routing_which_kinds_are_complete(tmp_path, monkeypatch):
    seen = {}

    def fake_route(*a, **k):
        seen.update(k)
        return {"routed": 0}

    monkeypatch.setattr(nightly.route, "route", fake_route)
    root, mem, _alpha, _beta = _projects(tmp_path)
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Rules\n- **Concise.** No fluff.\n")
    embed = lambda t: [[1.0, 0.0] for _ in t]  # noqa: E731
    nightly.run(mem, [md], embed, None, {}, day="2026-11-17", verdicts={}, route_roots=[root])
    assert "contradiction" not in seen["complete_kinds"]
    assert "no-mechanism" in seen["complete_kinds"]


def test_an_id_already_in_the_inbox_is_not_appended_again(tmp_path):
    # a run that appended and then stopped before the ledger was saved
    root, _mem, alpha, _beta = _projects(tmp_path)
    src = [str(alpha / "CLAUDE.md:1")]
    route.route([_prop("d1", "no-mechanism", src)], [root], {}, day="2026-11-17")
    ledger: dict = {}  # the ledger save was lost
    route.route([_prop("d1", "no-mechanism", src)], [root], ledger, day="2026-11-17")
    lines = [ln for ln in (alpha / route.INBOX).read_text().splitlines() if ln.startswith("- [")]
    assert len(lines) == 1 and "d1" in ledger[route.KEY]


def test_a_tracked_inbox_or_a_repo_above_is_never_written(tmp_path):
    root = tmp_path.resolve() / "Projects"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # a repo ABOVE the project
    delta = root / "delta"
    delta.mkdir()
    stats = route.route(
        [_prop("e1", "no-mechanism", [str(delta / "CLAUDE.md:2")])], [root], {}, day="2026-11-17"
    )
    assert not (delta / "scratch").exists() and stats["routed"] == 0
    assert stats["skipped"] == ["delta: the inbox path is not git-ignored"]


def test_a_loose_inbox_is_tightened_and_a_hard_link_is_refused(tmp_path):
    root, _mem, alpha, beta = _projects(tmp_path)
    (alpha / "scratch").mkdir()
    (alpha / route.INBOX).write_text("")
    os.chmod(alpha / route.INBOX, 0o644)
    route.route(
        [_prop("f1", "no-mechanism", [str(alpha / "CLAUDE.md:1")])], [root], {}, day="2026-11-17"
    )
    assert os.stat(alpha / route.INBOX).st_mode & 0o777 == 0o600
    (beta / "scratch").mkdir()
    other = tmp_path / "other.md"
    other.write_text("x\n")
    os.link(other, beta / route.INBOX)
    stats = route.route(
        [_prop("f2", "no-mechanism", [str(beta / "CLAUDE.md:1")])], [root], {}, day="2026-11-17"
    )
    assert other.read_text() == "x\n"
    assert stats["skipped"] == ["beta: inbox is not a regular single-link file of this user"]


def test_one_unwritable_project_does_not_stop_the_others(tmp_path):
    root, _mem, alpha, beta = _projects(tmp_path)
    (alpha / "scratch").mkdir()
    os.chmod(alpha / "scratch", 0o500)
    try:
        stats = route.route(
            [
                _prop("g1", "no-mechanism", [str(alpha / "CLAUDE.md:1")]),
                _prop("g2", "no-mechanism", [str(beta / "CLAUDE.md:1")]),
            ],
            [root],
            {},
            day="2026-11-17",
        )
    finally:
        os.chmod(alpha / "scratch", 0o700)
    assert stats["routed"] == 1 and stats["skipped"][0].startswith("alpha.site: cannot open")


# --- review round 2


def test_git_that_cannot_answer_is_never_taken_as_outside_a_repo(tmp_path):
    root, _mem, alpha, _beta = _projects(tmp_path)
    (alpha / ".git").write_text("not a gitdir pointer\n")  # git fails, but not "not a repo"
    stats = route.route(
        [_prop("aa01", "no-mechanism", [str(alpha / "CLAUDE.md:1")])], [root], {}, day="2026-11-17"
    )
    assert stats["routed"] == 0 and not (alpha / "scratch").exists()
    assert stats["skipped"] == ["alpha.site: git could not tell whether it is in a work tree"]


def test_a_torn_last_line_is_closed_and_is_not_a_delivery(tmp_path):
    root, _mem, alpha, _beta = _projects(tmp_path)
    (alpha / "scratch").mkdir(mode=0o700)
    (alpha / route.INBOX).write_text(route.HEADER + "- [ ] alpha.site · d1 · Add the")
    os.chmod(alpha / route.INBOX, 0o600)
    ledger: dict = {}
    route.route(
        [_prop("d1", "no-mechanism", [str(alpha / "CLAUDE.md:1")])],
        [root],
        ledger,
        day="2026-11-17",
    )
    text = (alpha / route.INBOX).read_text()
    assert text.endswith("(dream 2026-11-17, no-mechanism)\n")
    complete = [ln for ln in text.splitlines() if route._ENTRY.match(ln)]
    assert len(complete) == 1 and "d1" in ledger[route.KEY]


def test_a_decided_proposal_proposed_again_without_its_decision_reopens(tmp_path):
    root, _mem, alpha, _beta = _projects(tmp_path)
    src = [str(alpha / "CLAUDE.md:1")]
    ledger: dict = {}
    route.route([_prop("b1", "no-mechanism", src)], [root], ledger, day="2026-11-17")
    route.route([], [root], ledger, day="2026-11-18", decisions={"b1": {"decision": "accept"}})
    assert ledger[route.KEY]["b1"]["acted"]["how"] == "decided: accept"
    route.route([_prop("b1", "no-mechanism", src)], [root], ledger, day="2026-11-19")
    assert ledger[route.KEY]["b1"]["acted"] is None


def test_a_lane_name_cannot_break_the_line_or_forge_an_id(tmp_path):
    root, _mem, alpha, _beta = _projects(tmp_path)
    lanes = {"alpha.site": "bad\nlane · ffff · x"}
    route.route(
        [_prop("c0ffee", "no-mechanism", [str(alpha / "CLAUDE.md:1")])],
        [root],
        {},
        day="2026-11-17",
        lanes=lanes,
    )
    entries = [ln for ln in (alpha / route.INBOX).read_text().splitlines() if ln.startswith("- [")]
    assert len(entries) == 1 and route._ENTRY.match(entries[0]).group(1) == "c0ffee"


def test_a_scratch_others_can_write_is_refused(tmp_path):
    root, _mem, alpha, _beta = _projects(tmp_path)
    (alpha / "scratch").mkdir()
    os.chmod(alpha / "scratch", 0o777)
    stats = route.route(
        [_prop("e0", "no-mechanism", [str(alpha / "CLAUDE.md:1")])], [root], {}, day="2026-11-17"
    )
    assert stats["skipped"] == ["alpha.site: scratch/ is not this user's or others can write it"]
    assert not (alpha / route.INBOX).exists()


def test_a_project_that_is_not_the_folder_git_was_asked_about_is_refused(tmp_path):
    _root, _mem, alpha, _beta = _projects(tmp_path)
    written, why = route._append(alpha, [("e1", "- [ ] a · e1 · x. (dream 2026-11-17, k)")], (0, 0))
    assert written == set() and why == "the project folder changed after it was checked"
