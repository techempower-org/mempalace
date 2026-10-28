# Backends

What feeds the production palace and what it runs on, tiered by status.
`~/.claude/scripts/backends-report.sh` reads this file for the daily brief
(the "Backends grow continually" rule, 2026-10-27): add one adapter per
idle slot, then add or move its row here in the same PR.

Every status below was measured on familiar on 2026-10-27, read-only
against the live palace (1,261,277 drawers): drawer counts come from
`metadata->>'source_file'` and model names from the running daemon's
environment. A service's name in a drawer path is not ingest: every
Slack, Gmail, Notion, Outline and Home Assistant match on 2026-10-27 was
a memory file or transcript *about* the service. Re-measure before
moving a row. A row marked *not measured*
is inferred from code, not from the palace. The ingest rows sum to
1,258,695; the other 2,582 drawers are tests and one-offs
(`flush-trigger` 1,500, an NFS backup tree 686, `~/.config/claude-code`
118, hand-named sessions).

Tiers:

- **live**: drawers arrive without anyone running a command.
- **supported**: an adapter or normalizer exists; nothing in production
  uses it.
- **missing**: data exists somewhere JP owns, the palace has none of it,
  and there is no adapter.
- **candidate**: proposed, not yet scoped.

## Ingest sources

| Source | Tier | Drawers (2026-10-27) | Path in | Needs |
|---|---|---|---|---|
| Claude Code transcripts | live | 1,026,748 (`.jsonl`) + 60,866 other session files (tool results, scratch) | `~/.claude/projects/` synced 3-way by Syncthing (older drawers carry the disks path `/mnt/raid/claude-config/projects/`); Stop/PreCompact hooks mine them | — |
| Project files | live | 65,022 | `~/Projects/**` via `mempalace mine --mode projects` (older drawers carry the disks path `/mnt/raid/projects/`) | — |
| CLAUDE.md and `docs/*.md` | live | 29,939 | `palace-memory-sync.sh` PostToolUse hook on edits | #528: `docs/specs/` held 0 drawers on 2026-09-18 |
| Auto-memory (`~/.claude/projects/*/memory/`) | live | 8,898 | mined with the project dirs | #421: first-class curated drawers |
| Diary and hook saves | live | 67,222 (no `source_file`) | `tool_diary_write` (silent hook save) | — |
| Codex CLI, Gemini CLI, Aider, OpenCode, Warp sessions | supported | not measured | `mempalace/sources/` adapters, `normalize.py` | a mine target on a host that runs those tools |
| ChatGPT and claude.ai exports, Slack export JSON, Continue | supported | not measured | `normalize.py` parsers | an export file to mine |
| livekit-voice phone calls | missing | 0 (139 call records on disk) | none: records sit in `~/.local/share/livekit-voice/calls/<id>.json` (`transcript` list of `role`/`t`/`text`) | adapter; **next adapter** |
| Slack (live workspace) | missing | 0 | none | export or API adapter; token in the vault |
| Gmail | missing | 0 | none | Takeout mbox mined locally, or an API adapter |
| Notion | missing | 0 | none | export adapter |
| Outline | missing | 0 | none | API export adapter (Outline runs on disks) |
| Home Assistant logs | missing | 0 | none | logbook/recorder export adapter |
| Google Voice SMS (arrives as Gmail) | candidate | 0 | — | rides on the Gmail adapter |
| Calendar | candidate | 0 | — | ICS export adapter |
| Browser history on katana | candidate | 0 | — | JP's call (privacy) before any work |
| YouTube transcripts of the show | candidate | 0 | — | caption download plus a transcript normalizer |

## Model and storage backends

| Backend | Tier | Role | Needs |
|---|---|---|---|
| all-MiniLM-L6-v2 (ONNX, local, 384-dim) | live | drawer and query embeddings; no `MEMPALACE_EMBEDDING_MODEL` override is set | — |
| embeddinggemma, embeddinggemma2, adaptmem_ft, openai-compat (`embedding.py`) | supported | alternative embedders | a switch is a full re-embed; #386 and #468 (no embedder identity recorded) first |
| flashrank `ms-marco-TinyBERT-L-2-v2` | live | palace-daemon rerank (`PALACE_RERANK_ENABLED=true`) | — |
| PostgreSQL + pgvector (HNSW) | live | drawer store, BM25 (`doc_tsv`), trigram, `source_file` index (#567) | — |
| Apache AGE | live (store) | knowledge graph (`MEMPALACE_KG_BACKEND=age`) | — |
| KG write-through and LLM triple extraction | supported, off | `MEMPALACE_KG_WRITETHROUGH=0`, `MEMPALACE_KG_EXTRACTION_QUEUE=0` | #379 (re-enable after the extractor redo) |

Not palace backends, though they are sometimes listed as such: nomic embed
via ollama-embed (no palace code path uses it), NVIDIA Nemotron (the
`palace-nemotron` contest project), and Azure AI Foundry (`xreview`, the
cross-model review).
