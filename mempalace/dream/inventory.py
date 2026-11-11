"""Dream day 1: an inventory that needs no model.

Three checks, each read-only, each emitting proposals for a reviewer:

* ``oversized-index``: a memory folder's ``MEMORY.md`` over its line or byte cap, which the
  harness truncates when it loads the index (200 lines / 25 KB by default).
* ``no-mechanism``: a standing rule in a ``CLAUDE.md`` that names no mechanism (a hook, a gate,
  a test, a timer, a generated page) and carries no dated "none yet" line. A rule that does
  nothing is prose, and prose is what lets a rule die in a transcript.
* ``near-duplicate``: two feedback memories whose embeddings are this close, by the palace's own
  embedder (the daemon's ``/embed``), so a reviewer can merge them.

Nothing here writes to an input. The report goes to ``--out`` as ``YYYY-MM-DD.json`` and
``YYYY-MM-DD.md``. Inputs may be private (memory folders, CLAUDE.md files): keep ``--out``
somewhere private too.

    python -m mempalace.dream inventory --memory-root ~/.claude/projects \\
        --claude-md ~/.claude/CLAUDE.md --out <private dir>
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

INDEX_NAME = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024
NEAR_DUPLICATE = 0.90
EMBED_BATCH = 128
# What an embedding sees of a memory: its description and the start of its body.
EMBED_CHARS = 2000

AUTHORITY = "inventory: measured, no model"

Embedder = Callable[[List[str]], List[List[float]]]


@dataclass
class Memory:
    path: Path
    name: str
    description: str
    type: str
    body: str


@dataclass
class Proposal:
    kind: str
    summary: str
    sources: List[str]
    authority: str = AUTHORITY
    detail: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "sources": self.sources,
            "authority": self.authority,
            "detail": self.detail,
        }


# --- memories ------------------------------------------------------------------------------

_FRONT = re.compile(r"\A---[ \t]*\n(?P<head>.*?)\n---[ \t]*\n?(?P<body>.*)\Z", re.S)
_FIELD = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w-]*):[ \t]*(?P<value>.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_memory(path: Path) -> Optional[Memory]:
    """A memory file's frontmatter and body; None when it has no frontmatter. ``type`` is read at
    the top level or under ``metadata:``, the two shapes the harness has written."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FRONT.match(text)
    if not m:
        return None
    fields: Dict[str, str] = {}
    for line in m.group("head").splitlines():
        f = _FIELD.match(line)
        if f and f.group("value"):
            fields.setdefault(f.group("key"), _unquote(f.group("value")))
    return Memory(
        path=path,
        name=fields.get("name", path.stem),
        description=fields.get("description", ""),
        type=fields.get("type", ""),
        body=m.group("body"),
    )


def memory_folders(root: Path) -> List[Path]:
    """Every ``<project>/memory`` folder one level under ``root`` (no deeper walk)."""
    if not root.is_dir():
        return []
    return sorted(p / "memory" for p in root.iterdir() if (p / "memory").is_dir())


def read_memories(root: Path) -> List[Memory]:
    out = []
    for folder in memory_folders(root):
        for path in sorted(folder.glob("*.md")):
            if path.name == INDEX_NAME:
                continue
            mem = parse_memory(path)
            if mem is not None:
                out.append(mem)
    return out


# --- check 1: oversized indexes ------------------------------------------------------------


def oversized_indexes(
    root: Path, max_lines: int = INDEX_MAX_LINES, max_bytes: int = INDEX_MAX_BYTES
) -> List[Proposal]:
    out = []
    for folder in memory_folders(root):
        index = folder / INDEX_NAME
        try:
            data = index.read_bytes()
        except OSError:
            continue
        lines = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
        if lines <= max_lines and len(data) <= max_bytes:
            continue
        out.append(
            Proposal(
                kind="oversized-index",
                summary=(
                    f"{index} is {lines} lines / {len(data)} bytes, over the "
                    f"{max_lines}-line / {max_bytes}-byte cap: the tail is cut when it loads. "
                    "Merge or retire entries, or move detail into the memories."
                ),
                sources=[str(index)],
                detail={"lines": lines, "bytes": len(data)},
            )
        )
    return out


# --- check 2: rules with no mechanism ------------------------------------------------------

# A standing rule: a top-level bullet whose lead is bold ("- **Merge your own work.** …").
_RULE = re.compile(r"^- \*\*(?P<lead>[^*\n]+?)\*\*")
_HEADING = re.compile(r"^#{1,6} ")
# A named mechanism: the word itself, a dated "none yet", or something that runs: a script or
# hook file, a unit, a timer, a test, a gate, a guard. Case-insensitive; a heuristic, so its
# output is a proposal for a person, never an edit.
_MECHANISM = re.compile(
    r"(?i)\bmechanism\b|\bnone yet\b"
    r"|\b[\w.-]+\.(?:sh|py|mjs|js|ts|go|service|timer)\b"
    r"|\bhooks?\b|\bgate\b|\bguard\b|\bpre-?commit\b|\bsystemd\b|\bcron\b|\btimer\b"
    r"|\btests?\b|\bCI\b|\bskill\b"
)


# A rule says what to do; a bold-lead bullet that only states a fact ("**GPU**: RTX 2080 Ti") is
# not one. The lead or the block's first sentence must carry one of these words.
_NORMATIVE = re.compile(
    r"(?i)\b(?:never|always|must|should|don'?t|do not|avoid|prefer|only|every|each|before|"
    r"after|unless|instead|no|not|keep|use|run|ask|confirm|check|stop|wait)\b"
)
_FIRST_SENTENCE = re.compile(r"^.*?(?:[.!?](?=\s)|$)", re.S)


def is_rule(block: str) -> bool:
    first = _FIRST_SENTENCE.match(block.split("**", 2)[-1].strip())
    lead = _RULE.match(block)
    text = (lead.group("lead") if lead else "") + " " + (first.group(0) if first else "")
    return _NORMATIVE.search(text) is not None


def discover_claude_md(projects_root: Path) -> List[Path]:
    """``<projects_root>/*/CLAUDE.md``, one level down, skipping git worktrees (their ``.git`` is
    a file): a worktree's CLAUDE.md is a copy of its repo's, and would repeat every proposal."""
    if not projects_root.is_dir():
        return []
    out = []
    for d in sorted(projects_root.iterdir()):
        md = d / "CLAUDE.md"
        if md.is_file() and not (d / ".git").is_file():
            out.append(md)
    return out


def rule_blocks(text: str) -> Iterable[tuple]:
    """(line number, lead, block text) for each top-level bold bullet: the bullet and its
    continuation lines, up to the next top-level bullet, heading or blank-line-then-text."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _RULE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.startswith("- ") or _HEADING.match(line):
                break
            if not line.strip() and (i + 1 >= len(lines) or not lines[i + 1].startswith(" ")):
                break
            i += 1
        yield start + 1, m.group("lead").strip(), "\n".join(lines[start:i])


def unmechanised_rules(paths: Sequence[Path]) -> List[Proposal]:
    out = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, lead, block in rule_blocks(text):
            if not is_rule(block) or _MECHANISM.search(block):
                continue
            out.append(
                Proposal(
                    kind="no-mechanism",
                    summary=(
                        f'"{lead}" names no mechanism and no dated "none yet" line: add the hook, '
                        "gate, test or page that enforces it, or a dated none-yet line."
                    ),
                    sources=[f"{path}:{line_no}"],
                    detail={"lead": lead},
                )
            )
    return out


# --- check 3: near-duplicate feedback memories ---------------------------------------------


def _embed_text(mem: Memory) -> str:
    return f"{mem.name}\n{mem.description}\n{mem.body}"[:EMBED_CHARS]


def near_duplicates(
    memories: Sequence[Memory], embed: Embedder, threshold: float = NEAR_DUPLICATE
) -> List[Proposal]:
    """Pairs of feedback memories whose cosine similarity is at least ``threshold``."""
    import numpy as np

    feedback = [m for m in memories if m.type == "feedback"]
    if len(feedback) < 2:
        return []
    vectors: List[List[float]] = []
    texts = [_embed_text(m) for m in feedback]
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(embed(texts[i : i + EMBED_BATCH]))
    mat = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms == 0, 1.0, norms)
    sims = mat @ mat.T
    rows, cols = np.nonzero(np.triu(sims, k=1) >= threshold)
    pairs = sorted(zip(rows.tolist(), cols.tolist()), key=lambda rc: -float(sims[rc]))
    out = []
    for r, c in pairs:
        a, b = feedback[r], feedback[c]
        out.append(
            Proposal(
                kind="near-duplicate",
                summary=(
                    f"feedback memories {a.name!r} and {b.name!r} are {float(sims[r, c]):.3f} "
                    "similar: merge into one, keeping both whys."
                ),
                sources=[str(a.path), str(b.path)],
                detail={"similarity": round(float(sims[r, c]), 4)},
            )
        )
    return out


def daemon_embedder(url: Optional[str] = None, api_key: Optional[str] = None) -> Embedder:
    """The palace's own embedder through palace-daemon ``POST /embed`` (no model loaded here).
    ``PALACE_DAEMON_URL`` and ``PALACE_API_KEY`` from the environment by default."""
    base = (url or os.environ.get("PALACE_DAEMON_URL") or "http://127.0.0.1:8085").rstrip("/")
    key = api_key or os.environ.get("PALACE_API_KEY", "")

    def embed(texts: List[str]) -> List[List[float]]:
        req = urllib.request.Request(
            base + "/embed",
            data=json.dumps({"texts": texts}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)["embeddings"]

    return embed


# --- report --------------------------------------------------------------------------------


def run(
    memory_root: Path,
    claude_md: Sequence[Path],
    embed: Optional[Embedder] = None,
    threshold: float = NEAR_DUPLICATE,
) -> List[Proposal]:
    proposals = oversized_indexes(memory_root) + unmechanised_rules(claude_md)
    if embed is not None:
        proposals += near_duplicates(read_memories(memory_root), embed, threshold)
    return proposals


def render_markdown(proposals: Sequence[Proposal], day: str) -> str:
    lines = [f"# Dream inventory {day}", ""]
    if not proposals:
        lines.append("No proposals.")
    by_kind: Dict[str, List[Proposal]] = {}
    for p in proposals:
        by_kind.setdefault(p.kind, []).append(p)
    for kind, items in by_kind.items():
        lines += [f"## {kind} ({len(items)})", ""]
        for p in items:
            lines.append(f"- {p.summary}")
            lines.append(f"  - sources: {', '.join(p.sources)}")
            lines.append(f"  - authority: {p.authority}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(proposals: Sequence[Proposal], out_dir: Path, day: Optional[str] = None):
    day = day or _dt.date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / f"{day}.json"
    md = out_dir / f"{day}.md"
    js.write_text(json.dumps([p.as_dict() for p in proposals], indent=2) + "\n", encoding="utf-8")
    md.write_text(render_markdown(proposals, day), encoding="utf-8")
    return js, md
