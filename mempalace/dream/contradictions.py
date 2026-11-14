"""Dream day 2: feedback memories that contradict a CLAUDE.md rule, judged by a local model.

Every rule and every feedback memory is embedded once (palace-daemon ``/embed``). Each memory is
paired with its nearest rules above a similarity floor, the closest pairs first, up to a cap: a
contradiction is only worth a model call when both sides talk about the same thing. A local
OpenAI-compatible model (qwen36-coder on the B60 by default) reads each pair and answers
``contradict``, ``consistent`` or ``unrelated`` with a one-line why. Only ``contradict`` becomes a
proposal, labelled with the model as its authority: unreviewed.

A failed model call stops the pass where it is: what was judged is still reported, with the
failure, and nothing is retried (a busy shared model is not hammered).

A verdict is kept in a ledger keyed by the exact rule and memory text the judge read
(:func:`pair_key`), so a pair judged once is not judged again, and a night with the judge off
still reports what an earlier judge said (2026-11-14: a one-off cloud run judged the 60 pairs
the local model had left waiting). Either text changing makes a new key, and so a new judgement.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set

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
VERDICTS = ("contradict", "consistent", "unrelated")


def parse_verdict(text: str) -> Dict[str, str]:
    m = _JSON.search(text or "")
    if not m:
        raise ValueError("no JSON in the model's answer")
    data = json.loads(m.group(0))
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    return {"verdict": verdict, "why": str(data.get("why", "")).strip()[:400]}


def local_judge(
    url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None
) -> Judge:
    """An OpenAI-compatible chat endpoint; ``DREAM_LLM_URL``, ``DREAM_LLM_MODEL`` and
    ``DREAM_LLM_API_KEY`` from the environment by default (the key never in argv). The judge's
    ``authority`` names the model and where it ran: ``DREAM_LLM_WHERE`` when set (a tunnelled
    remote looks local by its URL), else "local" for a loopback URL, else the URL's host."""
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

    where = os.environ.get("DREAM_LLM_WHERE") or (
        "local" if _is_local(base) else urllib.parse.urlsplit(base).hostname or base
    )
    judge.authority = f"{name} ({where}), unreviewed"
    return judge


def _is_local(base: str) -> bool:
    host = urllib.parse.urlsplit(base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def pair_key(rule_text: str, memory_text: str) -> str:
    """The ledger key of one judgement: the exact texts the judge reads, nothing else."""
    digest = hashlib.sha256(f"{rule_text}\0{memory_text}".encode("utf-8")).hexdigest()
    return digest[:24]


def judge_refused(exc: BaseException) -> bool:
    """The judge's port refused the connection: the model is stopped, which is a state (JP
    stops :8091/:8092 when he needs the GPU), not a failure of the pass."""
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    return isinstance(exc, ConnectionRefusedError)


def _usable(entry: object) -> bool:
    """A ledger entry that can stand in for a judgement: a known verdict and who gave it."""
    return (
        isinstance(entry, dict)
        and entry.get("verdict") in VERDICTS
        and isinstance(entry.get("by"), str)
        and bool(entry["by"].strip())
    )


def contradictions(
    memories: Sequence[Memory],
    rules: Sequence[Rule],
    embed: Embedder,
    judge: Judge,
    authority: Optional[str] = None,
    guard: Optional[Callable[[], Optional[str]]] = None,
    ledger: Optional[Dict[str, dict]] = None,
    used: Optional[Set[str]] = None,
    on_verdict: Optional[Callable[[], None]] = None,
    **pair_args,
):
    """(proposals, stats). Stops calling the judge at the first failed judgement and records it
    in ``stats``. ``guard``, called before each model call, stops it the same way when it returns
    a reason (a host under memory pressure, say).

    ``ledger`` (updated in place) holds earlier verdicts by :func:`pair_key`: a pair found there
    is not sent to the judge, and its verdict carries the authority that gave it. With a ledger,
    ``stats["cached"]`` counts those, and pairs past a failure or an off judge still take their
    ledger verdicts. ``used`` collects every key this pass met, so the caller can drop the rest.
    An entry that is not a dict with a known verdict and a ``by`` is a miss: it is judged again,
    never credited to the current judge. ``on_verdict`` runs after each new verdict enters the
    ledger, so the caller can save it then and a run killed part way keeps what it judged."""
    authority = authority or getattr(judge, "authority", None) or "qwen36-coder (local), unreviewed"
    pairs = candidate_pairs(memories, rules, embed, **pair_args)
    stats: Dict[str, object] = {"pairs": len(pairs), "judged": 0, "verdicts": {}, "error": None}
    if ledger is not None:
        stats["cached"] = 0
    calling = True  # False once the judge is off or has failed: only the ledger answers then
    waiting = 0
    out = []
    for sim, mem, rule in pairs:
        rule_text = rule.text[:RULE_CHARS]
        memory_text = f"{mem.description}\n{mem.body}"[:MEMORY_CHARS]
        key = pair_key(rule_text, memory_text)
        if used is not None:
            used.add(key)
        by = authority
        hit = ledger.get(key) if ledger is not None else None
        if _usable(hit):
            v = {"verdict": hit["verdict"], "why": str(hit.get("why", ""))}
            by = hit["by"]
            stats["cached"] = int(stats["cached"]) + 1
        elif not calling:
            waiting += 1
            continue
        else:
            reason = guard() if guard else None
            if reason:
                stats["error"] = f"stopped: {reason}"
                calling = False
                waiting += 1
                if ledger is None:
                    break
                continue
            try:
                v = judge(rule_text, memory_text)
            except Exception as exc:  # one hiccup ends the model calls; no retry
                calling = False
                waiting += 1
                if stats["judged"] == 0 and judge_refused(exc):
                    # Off before the first judgement: say so, with what waits for it, and no error.
                    stats.update(
                        skipped=True,
                        judge="off",
                        refused=f"{type(exc).__name__}: {exc}"[:300],
                    )
                else:
                    stats["error"] = f"{type(exc).__name__}: {exc}"[:300]
                if ledger is None:
                    break
                continue
            stats["judged"] = int(stats["judged"]) + 1
            if ledger is not None:
                ledger[key] = {
                    "verdict": v["verdict"],
                    "why": v["why"],
                    "by": authority,
                    "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                if on_verdict is not None:
                    on_verdict()
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
                authority=by,
                detail={
                    "similarity": round(sim, 4),
                    "why": v["why"],
                    "rule": rule.lead,
                    "memory": mem.name,
                },
            )
        )
    if stats.get("judge") == "off":
        stats["waiting"] = waiting if ledger is not None else len(pairs)
    elif ledger is not None and waiting:
        stats["waiting"] = waiting
    return out, stats
