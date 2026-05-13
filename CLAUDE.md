# CLAUDE.md — memorypalace

## The Mission

Memory is identity. When an AI forgets everything between conversations, it cannot build real understanding — of you, your work, your people, your life.

MemPalace exists to solve this. It is a memory system — not a search engine, not a RAG pipeline, not a vector database wrapper. It treats every word you have shared as sacred, stores it verbatim, and makes it instantly available. Your data never leaves your machine. We never summarize. We never paraphrase. We return your exact words.

100% recall is the design requirement — the target every search path is measured against. Anything less means forgetting, and forgetting means starting over.

The name comes from the ancient "method of loci" — the memory palace technique used for thousands of years to organize and recall vast amounts of information by placing it in imagined rooms of an imagined building. We were also inspired by the Zettelkasten method (created by German sociologist Niklas Luhmann) — small cross-referenced index cards that point to each other. We apply both ideas to AI memory:

- **Wings** for broad categories (people, projects, topics)
- **Rooms** for time-based groupings (days, sessions)
- **Drawers** for full verbatim content (your exact words)
- **AAAK compression** for the index layer — a compact symbolic format (via `dialect.py`) that lets an LLM scan thousands of entries instantly and know exactly which drawer to open

## Design Principles

These are non-negotiable. Every PR, every feature, every refactor must honor them.

- **Verbatim always** — Never summarize, paraphrase, or lossy-compress user data. The system searches the index and returns the original words. If a user said it, we store exactly what they said. This is the foundational promise.
- **Incremental only** — Append-only ingest after initial build. Never destroy existing data to rebuild. A crash mid-operation must leave the existing palace untouched.
- **Entity-first** — Everything is keyed by real names with disambiguation by DOB, ID, or context. People matter more than topics.
- **Local-first, zero external API by default** — All extraction, chunking, embedding, and LLM-assisted refinement happens on the user's machine by default, using locally-hosted runtimes (Ollama, LM Studio, llama.cpp, vLLM, unsloth studio, etc.). External providers (Anthropic, OpenAI, Google) are supported via BYOK but are never required and never enabled silently. The system never sends user content to a service the user has not explicitly configured. "Local LLM" is not an external API — Ollama and equivalents running on localhost are part of the user's machine. External BYOK is always a deliberate user choice, never a default and never a silent fallback.
- **Performance budgets** — Hooks under 500ms. Startup injection under 100ms. Memory should feel instant.
- **Privacy by architecture** — The system physically cannot send your data because it never leaves your machine. No telemetry, no phone-home, no external service dependencies for core operations.
- **Background everything** — Filing, indexing, timestamps, and pipeline work happen via hooks in the background. Nothing interrupts the user's conversation. Zero tokens spent on bookkeeping in the chat window.

## Contributing

We welcome bug fixes, performance improvements, new language support, better entity disambiguation, documentation, and test coverage.

We do not accept summarization of user content, cloud storage/sync features, telemetry or analytics, features requiring API keys for core memory, or shortcuts that bypass verbatim storage.

---

The sections above are the fork's inherited mission and principles — same words as `upstream/develop:CLAUDE.md` so contributors get the same guidance regardless of which tree they're reading. What follows is fork-specific operational state.

## What This Is

JP's fork of [milla-jovovich/mempalace](https://github.com/milla-jovovich/mempalace) — a local AI memory system using ChromaDB for verbatim storage and semantic search.

- **Fork**: `jphein/mempalace` (origin) / `milla-jovovich/mempalace` (upstream)
- **Version + sync state**: `cat mempalace/__init__.py` for fork version; `git log --oneline upstream/develop ^HEAD | head -5` for unmerged upstream commits. Release/landed-PR history in `FORK_CHANGELOG.md`.
- **Python**: venv at `./venv/`, editable install with dev deps
- **Palace data**: `~/.mempalace/palace` (ChromaDB) + `~/.mempalace/config.json`

## Key Files

- `~/Projects/mempalace.yaml` — **do not delete**. Mining config with wing/room definitions. Regenerate with `mempalace init ~/Projects --yes` if lost.
- `~/.mempalace/config.json` — topic wings and hall keywords, customized for JP's domains (infrastructure, development, tools, creative, projects, system).
- `~/.mempalace/palace/` — ChromaDB vector store. The actual data.
- `~/.mempalace/hook_state/` — stop hook session tracking.

## Development

```bash
source venv/bin/activate
python -m pytest tests/ -q              # ~1096 tests (benchmarks deselected)
mempalace status                         # check palace state
mempalace search "query"                 # test search
python -m mempalace.mcp_server           # run MCP server standalone
```

Ruff for linting (`ruff check`), line length 100, target Python 3.9.

## Fork-ahead state

Authoritative sources — don't duplicate inventory in this file. CLAUDE.md stays slim and architectural; in-flight state lives in the right tracker:

- **Historical record of every fork-ahead change** — [`FORK_CHANGELOG.md`](FORK_CHANGELOG.md), rendered from canonical `docs/fork-changes.yaml`.
- **Open upstream PRs** — `gh pr list --repo MemPalace/mempalace --author jphein` (status table in README's "Fork change queue").
- **In-flight fork work, todos, coordination promises** — [jphein/mempalace issues](https://github.com/jphein/mempalace/issues). Anything that would feel like a broken promise if forgotten belongs here, not in scratch and not inline in CLAUDE.md.
- **Active session-scoped commitments** — `scratch/promises.md` (in-repo). Pruned aggressively; durable items migrate to issues.

Workflow for landing new fork-ahead changes lives in [Documentation maintenance](#documentation-maintenance) below.

<!-- prior inline row inventory (rows 1–39) + upstream-PR status table removed 2026-05-11; see FORK_CHANGELOG.md + gh pr list -->

## Two-Layer Memory Architecture

Claude Code has two complementary memory layers, used in tandem:

- **Auto-memory** (`~/.claude/projects/*/memory/`) — lightweight preferences, context, feedback. Manual writes only. (Anthropic's "Auto Dream" research-preview shipped late April 2026 in Claude Code `/dream` + the Managed Agents Dreams API; MemPalace deliberately stays un-consolidated and the Dreams API design ratifies the verbatim-vs-derivative axis. See `~/.claude/projects/-home-jp-Projects-memorypalace/memory/project_auto_dream.md`.)
- **MemPalace** (`~/.mempalace/palace/`, ~183K drawers behind the daemon) — verbatim conversations, tool output, code. Write-only archive, searchable via MCP. Completeness is the feature. Wing/room layout follows the canonical 7-room taxonomy (see `palace-taxonomy` skill / `~/Projects/familiar.realm.watch/docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md`).

Both systems coexist. Hook saves are scoped to MemPalace ("For THIS save, use MemPalace MCP tools only") — this is not a permanent ban on auto-memory.

## Hook Save Architecture

Two save modes, controlled by `hook_silent_save` in `~/.mempalace/config.json`:

- **Silent mode** (default, `hook_silent_save: true`): Direct Python API call to `tool_diary_write()`. Plain text, no AI involved, deterministic — save marker advances only after confirmed write. Shows `"✦ N memories woven into the palace"` as terminal notification.
- **Block mode** (legacy, `hook_silent_save: false`): Returns `{"decision": "block"}` asking the AI to call MCP tools. Non-deterministic — AI may ignore, summarize, or fail. Save marker advances before AI acts (data loss risk).

**v3.3.0 change:** Upstream hooks now return `"decision": "allow"` (background save, no AI blocking) instead of `"decision": "block"`. This aligns with our silent mode direction — the AI never needs to act on saves.

## Integration

- **Claude Code plugin**: installed at user scope via marketplace
- **MCP server**: global user scope — available in all projects
- **Stop hook**: fires every 15 messages, saves diary entry + auto-mines transcript
- **PreCompact hook**: emergency save before context compaction, auto-mines transcript, finds transcript by session_id fallback

## Testing

Always run `python -m pytest tests/ -x -q` after changes. Benchmark and stress tests are excluded by default (use `-m benchmark` or `-m stress` to include).

## Documentation maintenance

The fork-ahead narrative was previously hand-maintained in four places
(README's fork-change-queue table, this file's row inventory,
`FORK_CHANGELOG.md`, and `scratch/promises.md`). Drift was inevitable.
As of 2026-04-26 the **canonical source** is `docs/fork-changes.yaml`;
render targets are generated. The inline row inventory in this file was
retired 2026-05-11 — see [Fork-ahead state](#fork-ahead-state) above
for current pointers.

### Workflow for new fork-ahead changes

1. Land the code change with a focused commit on `main`.
2. Add an entry to `docs/fork-changes.yaml` (top of the `entries:`
   list, newest first). Schema is documented at the top of the YAML.
3. Run `scripts/render-docs.py` to regenerate `FORK_CHANGELOG.md`.
4. Run `scripts/check-docs.sh` to verify nothing has drifted (test
   count, commit hashes, render parity, upstream PR states).
5. Commit the YAML + the regenerated `FORK_CHANGELOG.md` together.

### Targets

| Target | Status |
|--------|--------|
| `FORK_CHANGELOG.md` | rendered from YAML (today) |
| README fork-change-queue table | hand-maintained for now |
| `scratch/promises.md` (in-repo) | hand-maintained, kept short — durable items move to `jphein/mempalace` issues |
| jphein/mempalace issues | hand-filed as work surfaces |

The renderer's `--target` flag is wired to take `changelog` or `all`;
`all` is the same as `changelog` until the README/CLAUDE/promises
renderers land.

### Lint

`scripts/check-docs.sh` runs four checks:

1. README test count vs `pytest --collect-only`
2. every fork commit hash referenced in docs resolves via `git cat-file -e`
3. `FORK_CHANGELOG.md` matches the YAML (re-render idempotent)
4. every `#NNNN` reference has an upstream state matching the doc's claim

Run before committing any doc change. Exit code 1 on drift.

