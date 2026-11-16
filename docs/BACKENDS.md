# Backends

What feeds the production palace and what it runs on, tiered by status.
A daily report reads this file (the "backends grow continually" rule,
2026-10-27): add one adapter per idle slot, then add or move its row here
in the same PR. This file is public: it names kinds of sources and tools,
never a host, a path on a machine, a person, an account or a secret's name.

Each adapter timer's unit retries a failed run every 10 minutes, at most six
times in four hours: a run fails when another mine holds the palace lock,
which is how the Slack run of 2026-10-28 08:34 was lost. Every write redacts
credential-shaped text first (#596).

Every status below was measured on the palace host on 2026-10-27, read-only
against the live palace (1,261,277 drawers): drawer counts come from
`metadata->>'source_file'` and model names from the running daemon's
environment. A service's name in a drawer path is not ingest: every
Slack, Gmail, Notion, Outline and Home Assistant match on 2026-10-27 was
a memory file or transcript *about* the service. Re-measure before
moving a row. A row marked *not measured*
is inferred from code, not from the palace. A row with its own date was
measured on that date. The rows measured on 2026-10-27, before the Slack
and Outline adapters went live, sum to 1,258,695; the other 2,582 drawers are tests and one-offs
(`flush-trigger` 1,500, an NFS backup tree 686, a tool's config directory
118, hand-named sessions).

Tiers:

- **live**: drawers arrive without anyone running a command.
- **supported**: an adapter or normalizer exists; nothing in production
  uses it.
- **missing**: data exists somewhere the operator owns, the palace has none
  of it, and there is no adapter.
- **candidate**: proposed, not yet scoped.

## Ingest sources

| Source | Tier | Drawers (2026-10-27) | Path in | Needs |
|---|---|---|---|---|
| Claude Code transcripts | live | 1,026,748 (`.jsonl`) + 60,866 other session files (tool results, scratch) | the Claude Code projects directory, synced between the operator's machines (older drawers carry an earlier storage path); Stop/PreCompact hooks mine them | — |
| Project files | live | 65,022 | the operator's project directories via `mempalace mine --mode projects` (older drawers carry an earlier storage path) | — |
| CLAUDE.md and `docs/*.md` | live | 29,939 | `palace-memory-sync.sh` PostToolUse hook on edits | #528: `docs/specs/` held 0 drawers on 2026-09-18 |
| Claude Code auto-memory files | live | 8,898 | mined with the project directories | #421: first-class curated drawers |
| Diary and hook saves | live | 67,222 (no `source_file`) | `tool_diary_write` (silent hook save) | — |
| Codex CLI, Gemini CLI, Aider, OpenCode, Warp sessions | supported | not measured | `mempalace/sources/` adapters, `normalize.py` | a mine target on a host that runs those tools |
| ChatGPT and claude.ai exports, Slack export JSON, Continue | supported | not measured | `normalize.py` parsers | an export file to mine |
| Phone-call transcripts (a voice agent) | live (private) | 411 in `mempalace_private_drawers`, 0 in the drawers collection (121 of 139 records have a transcript) | an hourly timer on the palace host: `mempalace mine <call records> --source livekit-calls`; privacy class `sensitive` | — (session transcripts that quote a call are now filed privately too: #588, #590, #591) |
| Slack (a household workspace) | live (2026-10-27) | 560: two channels (108 drawers over 84 days, 420 over 263 days) and the morning brief (32 over 15 days, the briefs only); 0 private | a daily timer on the palace host: `mempalace mine … --source slack`; channels under `slack_sources` in the palace config; the token in a 0600 environment file filled from the vault | other channels as asked; re-copy the token if it is rotated |
| Gmail | missing | 0 | none | Takeout mbox mined locally, or an API adapter |
| Notion | missing | 0 | none | export adapter |
| Outline (self-hosted) | live (2026-10-28) | 7,471 drawers from 328 published documents in 8 collections (wing `outline_wiki`), 0 sensitive (first run 2026-10-28; the daily timer runs from 2026-10-29) | `palace-outline-mine.timer` daily on the palace host: `mempalace mine … --source outline`; `outline_url` in the palace config; the token in a 0600 environment file filled from the vault | re-copy the token if it is rotated |
| Home Assistant logs | missing | 0 | none | logbook/recorder export adapter |
| Google Voice SMS (arrives as Gmail) | candidate | 0 | — | rides on the Gmail adapter |
| Calendar | candidate | 0 | — | ICS export adapter |
| Browser history on the desktop | candidate | 0 | — | the owner's call (privacy) before any work |
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

## Batch judge backend (2026-11-14/15)

A one-off batch backend, not a service: open-weight judges triaged drawers into
classes (keep, chatter, stale, secret) for the design in
[`designs/judged-drawer-classes.md`](designs/judged-drawer-classes.md). It wrote
nothing to the palace.

| Part | What | Rule |
|---|---|---|
| Compute | a rented AMD GPU host serving the judges with vLLM, reached over an SSH tunnel; after the run the inputs were deleted and the instance destroyed | rented third-party compute: no retention assurance beyond deleting the inputs and destroying the instance, which is why only redacted, allowlisted records were sent |
| Judges | first passes: Qwen2.5-72B-Instruct and Llama-3.1-Tulu-3-70B over every drawer; tie-breaks on the 11,918 drawers where those two disagreed: Tulu-3-70B and Qwen2.5-72B, each with a third prompt that shows both earlier verdicts and their reasons | open weights; no hosted model API |
| What left the palace host | drawers from allowlisted engineering wings only (personal and people wings excluded), each passed through `mempalace.redact` first; about half of judge 2's inputs also through the outbound scrub v3 (#619: the prefix-free shapes and the vault-item needles) | redaction before the record leaves, never after |
| Outputs | proposals in resumable, append-only ledgers (one JSON line per drawer id: class and a short reason) | the ledgers are the only output; the palace is untouched |
| Reasons | each judge's free-text reason passes a scrub before anyone reads it: any 20+ character token of mixed letters and digits, any quoted string over 16 characters, and everything after key, token, password, secret, DSN or `://` on the line; a closed ledger is rewritten scrubbed, verified to keep the same ids in the same order and the same class per id | a model can quote a secret; its words are data |
| Decision | where the two first passes agree, their class; on the disagreement set, only a strict majority of the four verdicts (both first passes and both tie-breaks), so at least three agreeing, and only when no judge said secret; ties and splits are left alone and counted | a flag for chatter and stale (rollback by id list); a secret drawer is re-redacted, then flagged; metadata backed up first |

Not palace backends, though they are sometimes listed as such: nomic embed
via ollama-embed (no palace code path uses it), NVIDIA Nemotron (a separate
contest project), and Azure AI Foundry (used for cross-model code review).
