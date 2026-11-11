"""Dream day 1 inventory: read-only checks that need no model (synthetic fixtures only)."""

from __future__ import annotations

import json
from pathlib import Path

from mempalace.dream import inventory
from mempalace.dream.__main__ import main


def _memory(folder: Path, name: str, kind: str, body: str, nested: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if nested:
        head = f"name: {name}\ndescription: about {name}\nmetadata:\n  type: {kind}\n"
    else:
        head = f'name: "{name}"\ndescription: about {name}\ntype: {kind}\n'
    path = folder / f"{name}.md"
    path.write_text(f"---\n{head}---\n\n{body}\n", encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_frontmatter_type_is_read_nested_or_top_level(tmp_path):
    a = _memory(tmp_path / "p" / "memory", "a", "feedback", "x")
    b = _memory(tmp_path / "p" / "memory", "b", "project", "y", nested=False)
    assert inventory.parse_memory(a).type == "feedback"
    mb = inventory.parse_memory(b)
    assert (mb.type, mb.name) == ("project", "b")
    plain = tmp_path / "plain.md"
    plain.write_text("no frontmatter\n")
    assert inventory.parse_memory(plain) is None


def test_an_index_over_either_cap_is_proposed_and_one_under_both_is_not(tmp_path):
    long_dir = tmp_path / "long" / "memory"
    long_dir.mkdir(parents=True)
    (long_dir / "MEMORY.md").write_text("- x\n" * 201)
    wide_dir = tmp_path / "wide" / "memory"
    wide_dir.mkdir(parents=True)
    (wide_dir / "MEMORY.md").write_text(("- " + "y" * 300 + "\n") * 90)
    ok_dir = tmp_path / "ok" / "memory"
    ok_dir.mkdir(parents=True)
    (ok_dir / "MEMORY.md").write_text("- z\n" * 200)
    found = {Path(p.sources[0]).parent.parent.name for p in inventory.oversized_indexes(tmp_path)}
    assert found == {"long", "wide"}


RULES = """# Rules

## Always-on
- **Merge your own work.** A PR merges when CI is green. Mechanism: the merge gate.
- **Concise and direct.** No fluff, no trailing summaries.
- **Back up first.** Before a destructive step,
  copy the config. none yet (2026-11-11).
- **Run the date command.** Before any claim about time; time-anchor.sh prints it.
- plain bullet without a bold lead names nothing
- **GPU**: RTX 2080 Ti, 11 GB.

## Other
- **Be kind.** Always.
  Continued line that names nothing either.
"""


def test_rules_without_a_mechanism_or_a_dated_none_yet_are_proposed(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(RULES)
    props = inventory.unmechanised_rules([md])
    assert [p.detail["lead"] for p in props] == ["Concise and direct.", "Be kind."]
    assert props[0].sources == [f"{md}:5"]
    assert all(p.authority == inventory.AUTHORITY for p in props)


def test_a_continuation_line_belongs_to_its_rule(tmp_path):
    blocks = list(inventory.rule_blocks(RULES))
    backup = next(b for b in blocks if b[1] == "Back up first.")
    assert "none yet" in backup[2]


def _fake_embed(vectors):
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return [vectors[t.split("\n", 1)[0]] for t in texts]

    return embed, calls


def test_near_duplicate_feedback_pairs_are_proposed_most_similar_first(tmp_path):
    folder = tmp_path / "p" / "memory"
    for name in ("a", "b", "c", "d"):
        _memory(folder, name, "feedback", f"body {name}")
    _memory(folder, "e", "project", "body e")  # not feedback: never compared
    vectors = {
        "a": [1.0, 0.0, 0.0],
        "b": [0.99, 0.05, 0.0],
        "c": [0.95, 0.3, 0.0],
        "d": [0.0, 0.0, 1.0],
        "e": [1.0, 0.0, 0.0],
    }
    embed, calls = _fake_embed(vectors)
    props = inventory.near_duplicates(inventory.read_memories(tmp_path), embed, threshold=0.9)
    names = [sorted(Path(s).stem for s in p.sources) for p in props]
    assert names[0] == ["a", "b"]
    assert ["a", "d"] not in names and all("e" not in n for n in names)
    sims = [p.detail["similarity"] for p in props]
    assert sims == sorted(sims, reverse=True)
    assert sum(calls) == 4


def test_embeddings_are_requested_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "EMBED_BATCH", 2)
    folder = tmp_path / "p" / "memory"
    for i in range(5):
        _memory(folder, f"m{i}", "feedback", "x")
    embed, calls = _fake_embed({f"m{i}": [1.0, float(i)] for i in range(5)})
    inventory.near_duplicates(inventory.read_memories(tmp_path), embed)
    assert calls == [2, 2, 1]


def test_the_run_writes_a_report_and_never_touches_its_inputs(tmp_path):
    root = tmp_path / "projects"
    folder = root / "p" / "memory"
    _memory(folder, "a", "feedback", "same")
    _memory(folder, "b", "feedback", "same")
    (folder / "MEMORY.md").write_text("- a\n" * 300)
    md = tmp_path / "CLAUDE.md"
    md.write_text(RULES)
    before = _snapshot(tmp_path)
    embed, _ = _fake_embed({"a": [1.0, 0.0], "b": [1.0, 0.01]})
    props = inventory.run(root, [md], embed)
    out = tmp_path / "out"
    js, rendered = inventory.write_report(props, out, "2026-11-11")
    after = {p: b for p, b in _snapshot(tmp_path).items() if out not in p.parents}
    assert after == before
    data = json.loads(js.read_text())
    assert {d["kind"] for d in data} == {"oversized-index", "no-mechanism", "near-duplicate"}
    assert all(d["sources"] and d["authority"] for d in data)
    text = rendered.read_text()
    assert text.startswith("# Dream inventory 2026-11-11")
    assert "## near-duplicate (1)" in text


def test_the_cli_runs_without_the_daemon_when_told_to(tmp_path, capsys):
    root = tmp_path / "projects"
    (root / "p" / "memory").mkdir(parents=True)
    md = tmp_path / "CLAUDE.md"
    md.write_text(RULES)
    out = tmp_path / "out"
    rc = main(
        [
            "inventory",
            "--memory-root",
            str(root),
            "--claude-md",
            str(md),
            "--out",
            str(out),
            "--day",
            "2026-11-11",
            "--no-embed",
        ]
    )
    assert rc == 0
    assert (out / "2026-11-11.md").exists()
    assert "2 proposals" in capsys.readouterr().out


def test_a_fact_with_a_bold_lead_is_not_a_rule():
    assert not inventory.is_rule("- **GPU**: RTX 2080 Ti, 11 GB.")
    assert inventory.is_rule("- **Concise and direct.** No fluff, no trailing summaries.")
    assert inventory.is_rule("- **Never push --all.** It rewrites every ref.")


def test_discovery_skips_worktrees_and_goes_one_level(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "CLAUDE.md").write_text("x")
    (tmp_path / "repo-wt").mkdir()
    (tmp_path / "repo-wt" / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n")
    (tmp_path / "repo-wt" / "CLAUDE.md").write_text("x")
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "CLAUDE.md").write_text("x")
    (tmp_path / "plain" / "deep").mkdir()
    (tmp_path / "plain" / "deep" / "CLAUDE.md").write_text("x")
    found = [p.parent.name for p in inventory.discover_claude_md(tmp_path)]
    assert found == ["plain", "repo"]
