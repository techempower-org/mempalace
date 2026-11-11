"""Dream day 2: feedback memories that contradict a CLAUDE.md rule, judged by a local model.

Every rule and every feedback memory is embedded once (palace-daemon ``/embed``). Each memory is
paired with its nearest rules above a similarity floor, the closest pairs first, up to a cap: a
contradiction is only worth a model call when both sides talk about the same thing. A local
OpenAI-compatible model (qwen36-coder on the B60 by default) reads each pair and answers
``contradict``, ``consistent`` or ``unrelated`` with a one-line why. Only ``contradict`` becomes a
proposal, labelled with the model as its authority: unreviewed.

A failed model call stops the pass where it is: what was judged is still reported, with the
failure, and nothing is retried (a busy shared model is not hammered).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .inventory import EMBED_BATCH, Embedder, Memory, Proposal, is_rule, rule_blocks

PAIR_FLOOR = 0.55
PER_MEMORY = 2
MAX_PAIRS = 60
RULE_CHARS = 1200
MEMORY_CHARS = 1500

Judge = Callable[[str, str], Dict[str, str]]


@dataclass
class Rule:
    source: str
    lead: str
    text: str
    path: Optional[Path] = None


def _project_key(directory: Path) -> str:
    """A directory as Claude Code names its project folder: every non-alphanumeric a "-"."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(directory))


def applies(memory: Memory, rule: Rule, global_dirs: Sequence[Path] = ()) -> bool:
    """Whether a rule governs a memory's project. A rule in a global CLAUDE.md (in one of
    ``global_dirs``, by default ``~/.claude``) governs every project; one in a project's CLAUDE.md
    governs only the memories saved in that project (its memory folder's project key), so a
    lesson saved in one repo is never weighed against another repo's rules."""
    if rule.path is None:
        return True
    parent = rule.path.parent.resolve()
    globals_ = [Path(d).expanduser().resolve() for d in (global_dirs or [Path.home() / ".claude"])]
    if parent in globals_:
        return True
    return memory.path.parent.parent.name == _project_key(parent)


def read_rules(paths: Sequence[Path]) -> List[Rule]:
    out = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, lead, block in rule_blocks(text):
            if is_rule(block):
                out.append(Rule(f"{path}:{line_no}", lead, block, path))
    return out


def _unit(vectors):
    import numpy as np

    mat = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.where(norms == 0, 1.0, norms)


def _embed_all(texts: List[str], embed: Embedder):
    vectors: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(embed(texts[i : i + EMBED_BATCH]))
    return _unit(vectors)


def candidate_pairs(
    memories: Sequence[Memory],
    rules: Sequence[Rule],
    embed: Embedder,
    floor: float = PAIR_FLOOR,
    per_memory: int = PER_MEMORY,
    cap: int = MAX_PAIRS,
    global_dirs: Sequence[Path] = (),
):
    """(similarity, memory, rule) for each feedback memory's nearest rules that govern it (see
    :func:`applies`) at or above ``floor``, most similar first, at most ``cap``."""
    feedback = [m for m in memories if m.type == "feedback"]
    if not feedback or not rules:
        return []
    mem_vecs = _embed_all(
        [f"{m.name}\n{m.description}\n{m.body}"[:MEMORY_CHARS] for m in feedback], embed
    )
    rule_vecs = _embed_all([r.text[:RULE_CHARS] for r in rules], embed)
    sims = mem_vecs @ rule_vecs.T
    pairs = []
    for i, row in enumerate(sims):
        taken = 0
        for j in row.argsort()[::-1]:
            if taken == per_memory or row[j] < floor:
                break
            if applies(feedback[i], rules[int(j)], global_dirs):
                pairs.append((float(row[j]), feedback[i], rules[int(j)]))
                taken += 1
    pairs.sort(key=lambda p: -p[0])
    return pairs[:cap]


PROMPT = """You check an agent's standing instructions for contradictions.

RULE (from a CLAUDE.md):
{rule}

FEEDBACK MEMORY (a lesson an agent saved):
{memory}

Would following one of these make an agent break the other? Answer with JSON only:
{{"verdict": "contradict" | "consistent" | "unrelated", "why": "<one sentence>"}}
"contradict" only when both cannot be followed at once in the same situation; a memory that is
narrower, stricter or an example of the rule is "consistent"."""

_JSON = re.compile(r"\{.*\}", re.S)


def parse_verdict(text: str) -> Dict[str, str]:
    m = _JSON.search(text or "")
    if not m:
        raise ValueError("no JSON in the model's answer")
    data = json.loads(m.group(0))
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("contradict", "consistent", "unrelated"):
        raise ValueError(f"unknown verdict {verdict!r}")
    return {"verdict": verdict, "why": str(data.get("why", "")).strip()[:400]}


def local_judge(
    url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None
) -> Judge:
    """An OpenAI-compatible chat endpoint; ``DREAM_LLM_URL``, ``DREAM_LLM_MODEL`` and
    ``DREAM_LLM_API_KEY`` from the environment by default (the key never in argv)."""
    base = (url or os.environ.get("DREAM_LLM_URL") or "http://127.0.0.1:8091").rstrip("/")
    name = model or os.environ.get("DREAM_LLM_MODEL") or "qwen36-coder"
    key = api_key or os.environ.get("DREAM_LLM_API_KEY", "")

    def judge(rule: str, memory: str) -> Dict[str, str]:
        body = {
            "model": name,
            "temperature": 0,
            "max_tokens": 200,
            # A reasoning model spends the whole budget thinking and answers nothing (2026-11-11:
            # qwen36-coder returned empty content, finish "length"). The verdict needs no chain.
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "user", "content": PROMPT.format(rule=rule, memory=memory)},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            answer = json.load(resp)["choices"][0]["message"]["content"]
        return parse_verdict(answer)

    return judge


def contradictions(
    memories: Sequence[Memory],
    rules: Sequence[Rule],
    embed: Embedder,
    judge: Judge,
    authority: str = "qwen36-coder (local), unreviewed",
    guard: Optional[Callable[[], Optional[str]]] = None,
    **pair_args,
):
    """(proposals, stats). Stops at the first failed judgement and records it in ``stats``.
    ``guard``, called before each judgement, stops the pass the same way when it returns a
    reason (a host under memory pressure, say)."""
    pairs = candidate_pairs(memories, rules, embed, **pair_args)
    stats: Dict[str, object] = {"pairs": len(pairs), "judged": 0, "verdicts": {}, "error": None}
    out = []
    for sim, mem, rule in pairs:
        reason = guard() if guard else None
        if reason:
            stats["error"] = f"stopped: {reason}"
            break
        try:
            v = judge(rule.text[:RULE_CHARS], f"{mem.description}\n{mem.body}"[:MEMORY_CHARS])
        except Exception as exc:  # one hiccup ends the pass; no retry
            stats["error"] = f"{type(exc).__name__}: {exc}"[:300]
            break
        stats["judged"] = int(stats["judged"]) + 1
        counts = stats["verdicts"]
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        if v["verdict"] != "contradict":
            continue
        out.append(
            Proposal(
                kind="contradiction",
                summary=(
                    f'feedback memory {mem.name!r} may contradict rule "{rule.lead}": {v["why"]} '
                    "Reconcile: change one, or narrow it."
                ),
                sources=[str(mem.path), rule.source],
                authority=authority,
                detail={
                    "similarity": round(sim, 4),
                    "why": v["why"],
                    "rule": rule.lead,
                    "memory": mem.name,
                },
            )
        )
    return out, stats
