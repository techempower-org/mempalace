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


def test_a_judge_that_refuses_the_connection_is_off_not_an_error(tmp_path):
    import urllib.error

    mems, rules, embed = _setup(tmp_path)

    def judge(rule, memory):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    props, stats = c.contradictions(
        mems, rules, embed, judge, floor=0.5, per_memory=1, global_dirs=[tmp_path]
    )
    assert props == []
    assert stats["error"] is None
    assert stats["skipped"] is True and stats["judge"] == "off" and stats["waiting"] == 2
    assert "Connection refused" in stats["refused"]


def test_a_refusal_after_the_first_judgement_is_still_an_error(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    calls = []

    def judge(rule, memory):
        calls.append(rule)
        if len(calls) > 1:
            raise ConnectionRefusedError(111, "Connection refused")
        return {"verdict": "consistent", "why": "ok"}

    _, stats = c.contradictions(
        mems, rules, embed, judge, floor=0.5, per_memory=1, global_dirs=[tmp_path]
    )
    assert stats["judged"] == 1 and stats["error"].startswith("ConnectionRefusedError")
    assert "judge" not in stats


def _refused(rule, memory):
    import urllib.error

    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def test_a_judged_pair_is_kept_and_not_judged_again(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger, calls = {}, []

    def judge(rule, memory):
        calls.append(rule)
        return {"verdict": "contradict" if "English" in rule else "consistent", "why": "w"}

    judge.authority = "big-model (cloud), unreviewed"
    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    first, s1 = c.contradictions(mems, rules, embed, judge, **kw)
    assert len(calls) == 2 and s1["judged"] == 2 and s1["cached"] == 0
    assert {v["by"] for v in ledger.values()} == {"big-model (cloud), unreviewed"}
    assert all(v["at"] and v["verdict"] in c.VERDICTS for v in ledger.values())
    again, s2 = c.contradictions(mems, rules, embed, judge, **kw)
    assert len(calls) == 2  # nothing sent twice
    assert s2["judged"] == 0 and s2["cached"] == 2 and s2["verdicts"] == s1["verdicts"]
    assert [p.summary for p in again] == [p.summary for p in first]


def test_with_the_judge_off_earlier_verdicts_still_count_and_only_new_pairs_wait(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger = {}

    def big(rule, memory):
        return {"verdict": "contradict", "why": "w"}

    big.authority = "big-model (AMD cloud), unreviewed"
    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    c.contradictions(mems[:1], rules, embed, big, **kw)  # only "push" judged
    assert len(ledger) == 1
    used = set()
    props, stats = c.contradictions(mems, rules, embed, _refused, used=used, **kw)
    assert stats["judge"] == "off" and stats["error"] is None
    assert stats["cached"] == 1 and stats["waiting"] == 1 and stats["judged"] == 0
    assert [p.authority for p in props] == ["big-model (AMD cloud), unreviewed"]
    assert len(used) == 2 and set(ledger) <= used


def test_a_changed_memory_is_judged_again(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger, calls = {}, []

    def judge(rule, memory):
        calls.append(memory)
        return {"verdict": "consistent", "why": "w"}

    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    c.contradictions(mems, rules, embed, judge, **kw)
    edited = [
        _mem(m.name, kind="project" if m.name == "proj" else "feedback", body="edited")
        for m in mems
    ]
    c.contradictions(edited, rules, embed, judge, **kw)
    assert len(calls) == 4 and len(ledger) == 4


def test_after_a_failure_later_pairs_still_take_their_ledger_verdicts(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger = {}

    def big(rule, memory):
        return {"verdict": "contradict", "why": "w"}

    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    c.contradictions(mems, rules, embed, big, **kw)
    first_key = next(iter(ledger))  # the closest pair's verdict is forgotten
    del ledger[first_key]

    def busy(rule, memory):
        raise TimeoutError("busy")

    props, stats = c.contradictions(mems, rules, embed, busy, **kw)
    assert stats["error"].startswith("TimeoutError") and "judge" not in stats
    assert stats["cached"] == 1 and stats["waiting"] == 1 and len(props) == 1


def test_a_ledger_entry_with_an_unknown_verdict_is_judged_again(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger, calls = {}, []

    def judge(rule, memory):
        calls.append(rule)
        return {"verdict": "consistent", "why": "w"}

    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    c.contradictions(mems, rules, embed, judge, **kw)
    for v in ledger.values():
        v["verdict"] = "maybe"
    c.contradictions(mems, rules, embed, judge, **kw)
    assert len(calls) == 4


def test_the_local_judge_names_where_it_ran(monkeypatch):
    monkeypatch.delenv("DREAM_LLM_WHERE", raising=False)
    assert (
        c.local_judge(url="http://127.0.0.1:8091", model="m").authority == "m (local), unreviewed"
    )
    assert "(gpu.example)" in c.local_judge(url="http://gpu.example:8000", model="m").authority
    monkeypatch.setenv("DREAM_LLM_WHERE", "AMD Developer Cloud MI300X")
    j = c.local_judge(url="http://127.0.0.1:18000", model="Qwen2.5-72B-Instruct")
    assert j.authority == "Qwen2.5-72B-Instruct (AMD Developer Cloud MI300X), unreviewed"


@pytest.mark.parametrize(
    "entry",
    [[], "contradict", {"verdict": "contradict"}, {"verdict": "contradict", "by": " "}, None],
)
def test_a_malformed_ledger_entry_is_judged_again_never_credited(tmp_path, entry):
    mems, rules, embed = _setup(tmp_path)
    ledger, calls = {}, []

    def judge(rule, memory):
        calls.append(rule)
        return {"verdict": "consistent", "why": "w"}

    judge.authority = "now (local), unreviewed"
    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    c.contradictions(mems, rules, embed, judge, **kw)
    for k in ledger:
        ledger[k] = entry
    _, stats = c.contradictions(mems, rules, embed, judge, **kw)
    assert len(calls) == 4 and stats["cached"] == 0 and stats["judged"] == 2
    assert all(v["by"] == "now (local), unreviewed" for v in ledger.values())


def test_a_guard_stop_with_a_ledger_still_meets_every_pair(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger, used = {}, set()

    def judge(rule, memory):
        return {"verdict": "contradict", "why": "w"}

    judge.authority = "big (cloud), unreviewed"
    kw = dict(floor=0.5, per_memory=1, global_dirs=[tmp_path], ledger=ledger)
    c.contradictions(mems, rules, embed, judge, **kw)
    props, stats = c.contradictions(
        mems, rules, embed, judge, guard=lambda: "low memory", used=used, **kw
    )
    # all pairs were in the ledger: the guard is never consulted, nothing waits
    assert stats["cached"] == 2 and stats["error"] is None and len(used) == 2 and len(props) == 2
    first = next(iter(ledger))
    del ledger[first]
    props, stats = c.contradictions(
        mems, rules, embed, judge, guard=lambda: "low memory", used=used, **kw
    )
    assert stats["error"] == "stopped: low memory" and stats["waiting"] == 1
    assert stats["cached"] == 1 and len(props) == 1


def test_a_refusal_after_a_judgement_with_a_ledger_is_an_error_and_keeps_the_verdict(tmp_path):
    mems, rules, embed = _setup(tmp_path)
    ledger, calls = {}, []

    def judge(rule, memory):
        calls.append(rule)
        if len(calls) > 1:
            raise ConnectionRefusedError(111, "Connection refused")
        return {"verdict": "consistent", "why": "ok"}

    saves = []
    _, stats = c.contradictions(
        mems,
        rules,
        embed,
        judge,
        floor=0.5,
        per_memory=1,
        global_dirs=[tmp_path],
        ledger=ledger,
        on_verdict=lambda: saves.append(dict(ledger)),
    )
    assert stats["judged"] == 1 and stats["error"].startswith("ConnectionRefusedError")
    assert "judge" not in stats and stats["waiting"] == 1
    assert len(ledger) == 1 and len(saves) == 1 and saves[0] == ledger
