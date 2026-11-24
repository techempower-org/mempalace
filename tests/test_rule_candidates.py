"""Rule candidates flagged at mining time (#584)."""

import subprocess
import sys
from datetime import date

import pytest

from mempalace import rule_candidates as rc


@pytest.fixture
def rc_env(tmp_path, monkeypatch):
    out = tmp_path / "rc"
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMPALACE_RULE_CANDIDATES_DIR", str(out))
    monkeypatch.setenv("MEMPALACE_RULE_MEMORY_GLOB", str(mem / "*.md"))
    monkeypatch.delenv("MEMPALACE_RULE_CANDIDATES", raising=False)
    return out, mem


def _chunk(text):
    return {"content": text, "chunk_index": 0}


DAY = date(2026, 10, 27)


def test_candidate_lines_only_user_rule_lines():
    content = "\n".join(
        [
            "> we should use the good dragon voices for narration",
            "I will always use Piper.",  # assistant line: not the user's words
            "> fix the typo",
            "> <task-notification> always restart </task-notification>",
            "> [Request interrupted by user] never mind",
            "> everything is fine",  # "every" is word-bounded
            "> From now on, merge your own PRs",
        ]
    )
    assert rc.candidate_lines(content) == [
        "> we should use the good dragon voices for narration",
        "> From now on, merge your own PRs",
    ]


def test_flag_chunks_writes_verbatim_with_drawer_id(rc_env):
    out, _mem = rc_env
    items = [
        (_chunk("> we should use the good dragon voices\nok"), "planning", "drawer_a"),
        (_chunk("> what time is it\nnoon"), "planning", "drawer_b"),
    ]
    n = rc.flag_chunks(items, "money", "/x/session.jsonl", today=DAY)
    assert n == 1
    text = (out / "rule-candidates-2026-10-27.md").read_text()
    assert text.startswith("# Rule candidates 2026-10-27\n")
    assert "`drawer_a` · money/planning · session.jsonl" in text
    assert "  > we should use the good dragon voices\n" in text
    assert "drawer_b" not in text


def test_flag_chunks_dedupes_across_passes_and_days(rc_env):
    out, _mem = rc_env
    items = [(_chunk("> Never commit secrets"), "r", "drawer_a")]
    assert rc.flag_chunks(items, "w", "s.jsonl", today=DAY) == 1
    assert rc.flag_chunks(items, "w", "s.jsonl", today=DAY) == 0
    assert rc.flag_chunks(items, "w", "s.jsonl", today=date(2026, 10, 28)) == 0
    assert not (out / "rule-candidates-2026-10-28.md").exists()
    assert (out / "rule-candidates-2026-10-27.md").read_text().count("drawer_a") == 1


def test_flag_chunks_drops_lines_covered_by_a_memory_slug(rc_env):
    out, mem = rc_env
    (mem / "a.md").write_text("---\nname: feedback_signature_on_file\ntype: feedback\n---\nbody\n")
    items = [
        (_chunk("> never place my signature unless the file is named"), "r", "drawer_sig"),
        (_chunk("> always use dragon voices for videos"), "r", "drawer_voice"),
    ]
    assert rc.flag_chunks(items, "w", "s.jsonl", today=DAY) == 1
    text = (out / "rule-candidates-2026-10-27.md").read_text()
    assert "drawer_voice" in text
    assert "drawer_sig" not in text


def test_matching_slug_is_fuzzy_and_needs_two_words():
    assert rc.matching_slug("always use dragon voices", ["feedback_dragon_voice"]) == (
        "feedback_dragon_voice"
    )
    assert rc.matching_slug("always use dragon voices", ["feedback_dragon_piper"]) is None
    # A one-word slug would match too much.
    assert rc.matching_slug("always use dragon voices", ["dragon"]) is None


def test_flag_chunks_skips_subagent_transcripts_and_can_be_disabled(rc_env, monkeypatch):
    out, _mem = rc_env
    items = [(_chunk("> always run the tests"), "r", "drawer_a")]
    assert rc.flag_chunks(items, "w", "/p/abc/subagents/agent-1.jsonl", today=DAY) == 0
    monkeypatch.setenv("MEMPALACE_RULE_CANDIDATES", "0")
    assert rc.flag_chunks(items, "w", "/p/abc/main.jsonl", today=DAY) == 0
    assert not out.exists()


def test_flag_chunks_never_raises(rc_env, monkeypatch):
    out, _mem = rc_env
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("a file where the directory should be")
    items = [(_chunk("> always run the tests"), "r", "drawer_a")]
    assert rc.flag_chunks(items, "w", "s.jsonl", today=DAY) == 0


def test_list_files_since(rc_env):
    out, _mem = rc_env
    for day in (date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)):
        rc.flag_chunks([(_chunk(f"> always {day}"), "r", f"d{day}")], "w", "s", today=day)
    got = [p.name for p in rc.list_files(date(2026, 10, 27))]
    assert got == ["rule-candidates-2026-10-27.md", "rule-candidates-2026-10-28.md"]


def _cli(*argv, env=None):
    return subprocess.run(
        [sys.executable, "-m", "mempalace.cli", *argv],
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_rules_since_lists_candidates(rc_env):
    import os

    out, _mem = rc_env
    rc.flag_chunks([(_chunk("> from now on use dragon"), "r", "drawer_x")], "w", "s", today=DAY)
    env = dict(os.environ)
    ok = _cli("rules", "--since", "2026-10-27", env=env)
    assert ok.returncode == 0, ok.stderr
    assert "`drawer_x`" in ok.stdout
    assert "> from now on use dragon" in ok.stdout
    later = _cli("rules", "--since", "2026-10-28", env=env)
    assert later.returncode == 0
    assert "drawer_x" not in later.stdout
    bad = _cli("rules", "--since", "yesterday", env=env)
    assert bad.returncode == 2


def test_cli_rules_without_since_still_needs_identity():
    result = _cli("rules", "--host", "mac")
    assert result.returncode == 2
    assert "--harness" in result.stderr and "--project" in result.stderr


def test_mining_a_conversation_flags_its_rule_lines(rc_env, tmp_path):
    import chromadb

    from mempalace.convo_miner import mine_convos

    out, _mem = rc_env
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    (convo_dir / "chat.txt").write_text(
        "> What voice did the video use?\nPiper, the local one.\n\n"
        "> We should use the good dragon voices for every recorded video.\n"
        "Noted.\n\n> How long is it?\nNinety seconds.\n"
    )
    palace_path = str(tmp_path / "palace")
    mine_convos(str(convo_dir), palace_path, wing="money")

    files = rc.list_files()
    assert len(files) == 1
    text = files[0].read_text()
    assert "  > We should use the good dragon voices for every recorded video.\n" in text
    assert "What voice" not in text

    col = chromadb.PersistentClient(path=palace_path).get_collection("mempalace_drawers")
    ids = col.get()["ids"]
    listed = [i for i in ids if f"`{i}`" in text]
    assert len(listed) == 1
    doc = col.get(ids=listed)["documents"][0]
    assert "We should use the good dragon voices" in doc

    # A re-mine of the unchanged file lists nothing new.
    mine_convos(str(convo_dir), palace_path, wing="money")
    assert files[0].read_text() == text
