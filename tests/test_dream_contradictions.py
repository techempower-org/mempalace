"""Dream day 2 contradictions: pairing by similarity and a judge that can fail (synthetic only)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mempalace.dream import contradictions as c
from mempalace.dream.inventory import Memory

RULES_MD = """# Rules
- **Never push to main.** Use a branch and a PR.
- **Always answer in English.** Even when asked in another language.
- **GPU**: RTX 2080 Ti.
"""


def _mem(name, kind="feedback", body="b"):
    return Memory(Path(f"/m/{name}.md"), name, f"about {name}", kind, body)


def _embed_by_first_word(table):
    def embed(texts):
        return [table[re.search(r"[A-Za-z]+", t).group(0).lower()] for t in texts]

    return embed


def test_only_normative_rules_are_read(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(RULES_MD)
    rules = c.read_rules([md])
    assert [r.lead for r in rules] == ["Never push to main.", "Always answer in English."]
    assert rules[0].source == f"{md}:2"


def _setup(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(RULES_MD)
    rules = c.read_rules([md])
    mems = [_mem("push"), _mem("lang"), _mem("far"), _mem("proj", kind="project")]
    table = {
        "push": [1.0, 0.0, 0.0],
        "lang": [0.0, 1.0, 0.0],
        "far": [0.0, 0.0, 1.0],
        "proj": [1.0, 0.0, 0.0],
        "never": [0.8, 0.6, 0.0],
        "always": [0.0, 1.0, 0.0],
    }
    return mems, rules, _embed_by_first_word(table)


def test_pairs_are_nearest_rules_above_the_floor_most_similar_first(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    pairs = c.candidate_pairs(mems, rules, embed, floor=0.5, per_memory=1, global_dirs=[tmp_path])
    assert [(m.name, r.lead) for _, m, r in pairs] == [
        ("lang", "Always answer in English."),
        ("push", "Never push to main."),
    ]
    assert pairs[0][0] >= pairs[1][0]
    assert (
        c.candidate_pairs(
            mems, rules, embed, floor=0.5, per_memory=1, cap=1, global_dirs=[tmp_path]
        )
        == pairs[:1]
    )


def test_only_contradict_becomes_a_proposal_with_the_model_as_authority(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    verdicts = iter([{"verdict": "contradict", "why": "x"}, {"verdict": "consistent", "why": "y"}])
    props, stats = c.contradictions(
        mems,
        rules,
        embed,
        lambda r, m: next(verdicts),
        floor=0.5,
        per_memory=1,
        global_dirs=[tmp_path],
    )
    assert [p.kind for p in props] == ["contradiction"]
    assert props[0].sources == ["/m/lang.md", rules[1].source]
    assert "unreviewed" in props[0].authority
    assert stats == {
        "pairs": 2,
        "judged": 2,
        "verdicts": {"contradict": 1, "consistent": 1},
        "error": None,
    }


def test_a_failed_judgement_stops_the_pass_without_a_retry(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    calls = []

    def judge(rule, memory):
        calls.append(rule)
        raise TimeoutError("busy")

    props, stats = c.contradictions(
        mems, rules, embed, judge, floor=0.5, per_memory=1, global_dirs=[tmp_path]
    )
    assert len(calls) == 1
    assert props == []
    assert stats["pairs"] == 2 and stats["judged"] == 0
    assert stats["error"].startswith("TimeoutError")


@pytest.mark.parametrize(
    "text, verdict",
    [
        ('{"verdict": "contradict", "why": "a"}', "contradict"),
        ('Sure.\n```json\n{"verdict":"Consistent","why":"b"}\n```', "consistent"),
    ],
)
def test_verdicts_are_parsed_from_the_answer(text, verdict):
    assert c.parse_verdict(text)["verdict"] == verdict


@pytest.mark.parametrize("text", ["no json here", '{"verdict": "maybe"}'])
def test_an_unusable_answer_is_an_error(text):
    with pytest.raises(ValueError):
        c.parse_verdict(text)


def test_the_local_judge_turns_thinking_off_and_sends_the_key_as_a_header(monkeypatch):
    import io
    import json

    sent = {}

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data)
        sent["auth"] = req.get_header("Authorization")
        answer = {"choices": [{"message": {"content": '{"verdict": "unrelated", "why": "z"}'}}]}
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(c.urllib.request, "urlopen", fake_urlopen)
    judge = c.local_judge(url="http://model", model="m", api_key="k")
    assert judge("rule", "memory") == {"verdict": "unrelated", "why": "z"}
    assert sent["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["body"]["temperature"] == 0
    assert sent["auth"] == "Bearer k"


def test_a_project_rule_governs_only_its_own_projects_memories(tmp_path):
    home_claude = tmp_path / "home" / ".claude"
    repo = tmp_path / "Projects" / "repo.one"
    other = tmp_path / "Projects" / "other"
    for d in (home_claude, repo, other):
        d.mkdir(parents=True)
    key = c._project_key(repo.resolve())
    mem = Memory(tmp_path / "projects" / key / "memory" / "m.md", "m", "", "feedback", "")
    g = c.Rule("g:1", "g", "", home_claude / "CLAUDE.md")
    own = c.Rule("r:1", "r", "", repo / "CLAUDE.md")
    foreign = c.Rule("o:1", "o", "", other / "CLAUDE.md")
    assert c.applies(mem, g, [home_claude])
    assert c.applies(mem, own, [home_claude])
    assert not c.applies(mem, foreign, [home_claude])


def test_pairs_skip_rules_from_other_projects_and_take_the_next_nearest(tmp_path):
    home_claude = tmp_path / ".claude"
    other = tmp_path / "other"
    for d in (home_claude, other):
        d.mkdir()
    (other / "CLAUDE.md").write_text("- **Never push to main.** Use a branch.\n")
    (home_claude / "CLAUDE.md").write_text("- **Always answer in English.** Even so.\n")
    rules = c.read_rules([other / "CLAUDE.md", home_claude / "CLAUDE.md"])
    mem = Memory(tmp_path / "p" / "-x" / "memory" / "push.md", "push", "", "feedback", "")
    embed = _embed_by_first_word({"push": [1.0, 0.0], "never": [1.0, 0.0], "always": [0.8, 0.6]})
    pairs = c.candidate_pairs(
        [mem], rules, embed, floor=0.5, per_memory=1, global_dirs=[home_claude]
    )
    assert [r.lead for _, _, r in pairs] == ["Always answer in English."]
