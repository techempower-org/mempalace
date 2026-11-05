# Dreaming for the fleet: decision

2026-11-05 · morpheus-palace · for JP (relayed by jp-eb): "decide if we should use karta, or another product, or write our own."

"Dreaming" here means a background job that reads what agents and JP produce and proposes merged or retired rules, contradictions and daily digests, without touching the verbatim record.

## What we have

- **Karta fork** (jphein/karta, MIT, Rust, 11.4K lines, last commit 2026-04-11).
  - Its dream engine runs seven passes (deduction, induction, abduction, consolidation, contradiction, two digests).
  - It writes results into the **same** vector store it reads from, and its write path "evolves" notes in place.
  - BEAM 100K: 53.0% at the end (57.7% best), against a 63% target.
  - Upstream (rohithzr/karta) has 26 commits since April, including a July storage rewrite; our fork has diverged.
- **MemPalace's position** (`docs/ARCHITECTURE.md` §1): the verbatim record is canonical, and derived layers sit beside it, rebuildable from it. Mixing the two hurt retrieval, so the derived half left the corpus in May 2026.
- **Claude Code Auto Dream.** `autoDreamEnabled: true` is set (2.1.318), but the official memory docs and changelog never mention it; its cadence comes from blogs. It rewrites one project's memory files in place. The `~` index `MEMORY.md` is **28.7 KB**, past the documented 25 KB load cap.
- **Volume.** 1,712 memory files in 132 projects (414 feedback memories, about 4.7 MB). CLAUDE.md is 31 KB; HANDOFFs run 24 KB to 1 MB per lane.
- **Local model.** qwen36-coder on familiar's B60 (`:8091`): 131k context, about 50 tokens/s, healthy today.

## What's out there

| System | What it consolidates | Where output lives | Input kept verbatim? | Fits our need (b)? | Cost model | Burden / license |
|---|---|---|---|---|---|---|
| **Karta** (our fork) | notes → deductions, contradictions, digests | same store as input | no (evolves notes in place) | no: QA recall, not rules | local core model + a premium answer model | Rust fork, 7 months stale · MIT |
| **Claude Code Auto Dream** | one project's auto-memory | the memory files themselves | no (edits in place) | partly: one project, no CLAUDE.md, no review gate | the seat's quota | none, but undocumented |
| **Anthropic Dreams API** | a memory store plus ≤100 sessions | a **new** store | **yes** | partly: closest design | Opus or Sonnet on the API, research preview | hosted, access form |
| **Letta sleep-time agents** | conversations → memory blocks; git-backed MemFS since 2026 | the agent's blocks (versioned) | no (blocks edited; git gives a history) | no: per-agent state | any model, cheap models suggested | Python server · Apache-2.0 |
| **Honcho** (Plastic Labs) | messages → conclusions, peer cards, "dreams" | the Honcho store | input messages kept | no: models users, not rules | hosted or self-host; gpt-5.4-mini default (v3.4.1) | service · AGPL-3.0 |
| **Mem0** | facts: ADD/UPDATE/DELETE per write | the Mem0 store | no | no | an LLM call per write | library/service · Apache-2.0 |
| **Zep / Graphiti** | a temporal KG with validity windows | the graph | episodes kept | no: facts, not rules | an LLM call per write | Graphiti Apache-2.0; Zep hosted |
| **LangMem** | a debounced background extractor | the LangGraph store | no | no | an LLM call per thread | library · MIT; queue lost on restart |
| **Cognee** | graph, opt-in contradiction pass | the graph | no | no | an extra LLM pass per run | library · Apache-2.0 |

Sources: [Claude Code memory docs](https://code.claude.com/docs/en/memory), [changelog](https://code.claude.com/docs/en/changelog), [Auto Dream write-up (third party)](https://claudefa.st/blog/guide/mechanics/auto-dream), [Dreams API](https://platform.claude.com/docs/en/managed-agents/dreams), [Letta sleep-time compute](https://letta.com/blog/sleep-time-compute), [Honcho](https://honcho.dev/), [LangMem delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing), [Cognee on conflicting data](https://www.cognee.ai/time-aware-ai-memory-conflicting-data), [Karta upstream](https://github.com/rohithzr/karta), [AuthMem-Bench, arXiv 2608.01679](https://arxiv.org/abs/2608.01679).

## Decision

**Write our own: a small nightly Python "dream" job inside mempalace.**
- It reads feedback memories, MEMORY.md indexes, CLAUDE.md, HANDOFFs and palace daily windows through the daemon.
- It does the bulk pass on the local qwen36-coder, and uses Opus only to review the shortlist.
- It writes a **proposals file** to a branch and opens a PR. Nothing becomes a rule, and no memory changes, until a reviewer merges it.

Don't revive Karta, and don't adopt a product. Leave Auto Dream on for now; the job neither depends on it nor conflicts with it.

## Why, in JP's terms

1. **Your words stay untouched.** Every product except the Dreams API rewrites its input. That includes Karta, whose dreams land in the same store they read. The Dreams API has the right shape, but it runs on Anthropic's memory stores, not our files, and bills Opus per call.
2. **Nobody sells what the fleet needs.** These products remember facts about users. Our problem is the 414 feedback memories that overlap, CLAUDE.md rules that a lane's behaviour contradicts, rules that have no mechanism (your 10-27 rule), and a daily digest per lane. A program that knows our files can do most of that before any model is involved.
3. **Review before a rule.** AuthMem-Bench (Aug 2026) found consolidation turned observations into instructions in 48 of 49 setups tested. Unlabelled, that led to unauthorized actions 50% of the time; persisted source labels brought it to 0%. So every proposal carries its source and authority (your words, a lead's words, an agent's observation), and a PR is the gate.
4. **Cheap and ours to keep.** The bulk runs on the B60, and Opus reads a short list. It's one Python module and a timer, like the palace mine timers we already run; no Rust fork.
5. **Benchmarks don't decide this.** BEAM and LoCoMo measure question-answering recall, and their numbers are disputed. We need reviewable rule hygiene.

## Cost to start

About 3 lane-days for morpheus-palace, all on familiar. qwen36-coder does the bulk pass; Opus reviews about 20–40K tokens a night. No sign-ups, no spend, nothing for JP to click.

## First week

- **Day 1: inventory, no model.** Read every memory folder and CLAUDE.md.
  - Emit near-duplicate feedback memories by embedding, using the palace's own embedder.
  - Emit MEMORY.md files over their 200-line or 25 KB cap.
  - Emit CLAUDE.md rules with no named mechanism and no dated "none yet" line.
- **Day 2: local passes.**
  - Contradictions: feedback memory against CLAUDE.md rule, on pairs pre-filtered by similarity.
  - A per-lane daily digest from HANDOFF diffs and the palace window.
  - Every proposal carries its sources and an authority label.
- **Day 3: gate and schedule.** Opus reviews the shortlist; the job writes `dream/YYYY-MM-DD.md` plus JSON and opens a PR. A systemd user timer at 03:30, with retry like the mine timers.
- **Days 4–5: measure.** Run it nightly and record how many proposals the lead accepts versus rejects. Drop any proposal kind that's mostly rejected. Report to jp-eb with the numbers.

## Not verified

- Auto Dream's real behaviour on this account: whether it has run, its cadence, which files it changed. No official documentation exists.
- Dreams API access for our org, and how its output store looks in practice.
- Letta's 2026 MemFS design and Honcho's internals, which come from secondary sources. The licenses in the table were checked against each repo through the GitHub API.
- qwen36-coder's precision on contradiction finding (untested) and its prefill speed on a full pass of about 1.2M tokens (not measured).
- Karta upstream's July changes: I read commit titles only.
