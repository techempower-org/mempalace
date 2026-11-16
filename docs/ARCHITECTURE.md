# Architecture

The fork's architectural thinking, consolidated from production experience on a 335K+ drawer Postgres + pgvector + Apache AGE palace. These ideas inform PR review, roadmap priorities, and how we evaluate new features.

## The four layers

Agent memory architecture has stratified into four orthogonal layers. The cleanest way to read the field — and the cleanest way to read what this fork is doing — is to treat them as independently improvable, because the empirical evidence is that improvements stack rather than substitute.

1. **Storage.** Verbatim drawers; no LLM in the index path. ChromaDB upstream, with this fork running Postgres + pgvector + Apache AGE on `main` as of 2026-05-15. Convergent across five 2026-vintage systems (MemPalace, Longhand, Celiums, mcp-memory-service, engram) — the verbatim-first cluster reached independent critical mass in April.
2. **Encoder.** The embedding model itself, normally invisible because most systems treat it as a vendor decision. @nakata-app's [adaptmem](https://github.com/nakata-app/adaptmem) ([discussion #1249](https://github.com/MemPalace/mempalace/discussions/1249)) shows contrastive fine-tuning on hard negatives lifts retrieval orthogonally: MemPalace raw 0.966 R@5 → +FT-300 0.980 → +hybrid_v4 0.990 R@5 / 0.916 R@1. The encoder layer is the most under-explored across OSS memory systems.
3. **Retrieval.** How vectors are queried, combined, reranked. Three patterns co-exist: deterministic pipelines ([Familiar](https://github.com/jphein/familiar.realm.watch) v0.3.9 hits **78.33%** recall on jp-realm-v0.1), LLM-orchestrated retrieval ([RLM](https://github.com/alexzhang13/rlm) with Qwen 2.5 7B and Llama 3.3 70B both **ceiling at 46.67%** — model size doesn't fix it), parallel hybrid ([Hindsight](https://hindsight.vectorize.io/blog/2026/03/27/parallel-hybrid-search)'s 91% R@10 via four-way RRF). The 32-point gap between Familiar and RLM is entirely invocation: RLM agents zero-call the tool on 22–25 of 30 questions; Familiar's pipeline runs on all 30 because the agent doesn't choose.
4. **Consumption.** What happens after retrieval. This is where the R@k → end-to-end QA gap lives — MemPalace's 96.6% R@5 → 82.6% E2E QA on Issue #39 reproduction; Celiums benchmarking shows 100% retrieval rate but only 62.3% QA with Opus 4.6. Three architectural bets compete: algorithm (Familiar's always-on pipeline), human-in-the-loop (Kostadis's retrieve/render isolation), trained policy (Kent's APO recall games). The right answer probably depends on workload; the field doesn't yet know.

*Calibration on the SME numbers:* 30 questions, beta-level instrumentation, substring-on-filename scoring. The defensible findings are deltas under identical conditions; absolute percentages are decoration. Methodology disclosure and the broader four-layer synthesis live in [`docs/research/three-patterns-for-agent-memory.md`](research/three-patterns-for-agent-memory.md) and the [compass artifact](research/compass_artifact_wf-ad108fcc-3960-4eab-ad5d-234bf365b2f4_text_markdown.md).

## The thesis (operational principles)

The fork has converged on three principles. Treat them as the design test for future work over the verbatim layer.

### 1. Verbatim vs. derivative is the canonical axis

The unit of memory in MemPalace is the verbatim utterance — chats, tool calls, mined files, the literal text the user produced or witnessed. Anything else (summaries, KG triples, agent journals, AAAK-encoded reflections, Auto-Dream consolidated indices) is *derivative* of that verbatim record. Derivative writes are useful but they are a different kind of thing: their right access pattern is event-shaped (session_id, time, agent) or scoped (cwd, project), not unconditioned semantic similarity over the whole corpus.

Most public AI memory systems frame the problem the other way around: ingest raw, transform on write, store the derivative as canonical. Mem0 extracts "memories." Zep and Letta tier and summarize. Cognee builds a knowledge graph. Hindsight retains/recalls/reflects with LLM-extracted facts. In each, the verbatim original is gone — or at best, retrievable only through a layer of inference that already lost nuance. The fork's bet is the inverse: keep verbatim canonical, key derivative layers for their actual access pattern, and treat any derivative store as rebuildable from the verbatim.

Mixing verbatim and derivative in one corpus is a real failure mode — the structural fix in May 2026 was to drop one half of an earlier split entirely. Future derivative layers can still live in sibling collections keyed for their access pattern — but only if and when each one earns its own MCP read surface.

This axis is implicit in upstream's [RFC 001](https://github.com/MemPalace/mempalace/pull/743) but isn't yet named in the spec. Worth making explicit upstream — multi-collection-by-purpose is the architectural move that future backends should plan for.

### 2. Corpus shape eats retrieval algorithm for breakfast

A week of filter tuning, BM25 fallback, and over-fetch parameters could not make `kind=content` return more than 3 tokens per question on the canonical palace. ~640 Stop-hook auto-save checkpoint drawers — 0.4% of the corpus — dominated 80%+ of every vector top-N because they were short, query-term-saturated, and embedded close to recent prompts. Recall@5 was 0.984 the whole time. End-to-end answer quality collapsed.

Then we moved them out of the corpus. One structural change — a separate ChromaDB collection — and `kind=content` jumped to 1,267 tokens per question. The fix that retired the checkpoint split in May 2026 went further: stop writing the derivative half at all. The lesson is durable: when corpus shape is wrong, no amount of post-filter cleverness substitutes for fixing the corpus.

This generalizes to every retrieval system that ingests by default and filters by query. Solve it at write time, by purpose, not at query time, by predicate.

### 3. The right to measure is the local-first benefit

The usual case for local AI memory is data sovereignty. The deeper benefit is *the right to audit your own integration shape*. Cat 9 in the SME framework — "the Handshake" — names a class of failure that recall benchmarks miss: the gap between retrieval working and the model actually being grounded on the retrieved content. We could only measure it because we own every layer of the stack. A vendor product would have shown us 0.984 R@5 on a dashboard and called it a day.

The SME jp-realm-v0.1 numbers are this principle made operational. The 46.67% / 78.33% delta is *invocation discipline*, not retrieval quality — a distinction no offline-only benchmark catches. Sovereignty wins arguments; auditability wins debugging sessions. The TechEmpower bridge essay at [`notebook/essays/2026-04-25-techempower-bridge.md`](https://github.com/jphein/notebook/blob/main/essays/2026-04-25-techempower-bridge.md) develops the underlying claim further.

## What this fork has learned

Four claims that fall out of the thesis when you take it seriously and run it in production for a few months.

**Corpus shape is not a tuning parameter; it's an architectural choice.** The 2026-04-25 → 2026-04-26 collection split closed a 210× pre/post token gap. Three weeks later, the structural shift went further: drop the derivative half entirely. Hooks now write only verbatim transcript chunks; one collection, one search path, no kind-filter, no over-fetch hack.

**Verbatim storage is load-bearing as the canonical layer.** Derivative work (KG, summaries, decay scores, Auto-Dream consolidated indices) is welcome as long as it stays *next to* the verbatim record, not replacing it. Anthropic's Dreams API made the same call at the vendor-API level: input read-only, output a separate store.

**The right to measure is the local-first benefit that matters in production.** Cat 9 / The Handshake on this fork's deployment was findable because we own every layer of the stack — a vendor product would have shown 0.984 R@5 on a dashboard and called it shipped.

**The integration gap (Cat 9 / Handshake) is real, reproducible, and measurable.** Engram-2's "17% E2E QA" claim landed on a real failure surface — checkpoint domination of vector top-N — and the structural fix demonstrably closes it on this corpus. End-to-end LongMemEval results to publish at `notebook/data/cat9-postmigrate-e2e/REPORT.md`.

Underneath all four, the operational work that doesn't make headlines is still mostly the two hard things — **naming** (wing/room/topic taxonomies, the verbatim-vs-derivative split was itself a naming clarification) and **cache invalidation** (HNSW staleness detection, graph-cache write-invalidation, decay/recency weighting, stale auto-loaded docs). Karlton's joke is durable for a reason.

## Design principles

Three operational principles that inform PR review. They predate the thesis but converge on the same conclusions.

### 1. Lazy derivation with graceful fallback

Write the raw text first; derive everything else lazily, from unambiguous signals, with a graceful fallback when derivation fails. The verbatim archive is the one thing that must always succeed. Optional enrichment (LLM topic extraction, AAAK encoding, concept chunking) is welcome as long as it stays opt-in, additive, and never a prerequisite for the write to complete.

The inverse — making classification a *gate* — is where the fork's earliest visible bugs came from: `room=None` crashes, a stopword list at 285 English entries papering over false positives, wing misassignment. The fork's design test for any new write-path feature: *does this require interpreting content at write time?* If yes, derive lazily instead.

### 2. Derived hierarchy from unambiguous signals

Hierarchy works when it's derived from unambiguous signals (cwd, transcript path, project directory) — not when it's hand-classified by content inspection. The earlier mistake was conflating "hierarchy is bad" with "mandatory synchronous classification is bad" — different claims.

**Good uses of hierarchy, which we keep:**
- **Browseable scope** for serendipitous recall across 335K+ drawers.
- **Deletion and retention as a unit.** Purging an abandoned project is one operation, not a risky query-then-delete.
- **Disambiguation without query gymnastics.** The same keyword across years of unrelated work.
- **Auto-surfacing priors.** A wing derived from cwd is a cheap, unambiguous scoping signal.

**Bad uses, which we're unwinding:**
- Required at write time (caused all the crashes).
- Derived from content-inspection heuristics (NER, keyword matching) rather than unambiguous signals.
- Single-label, as if every drawer had one true parent. Cross-cutting concerns belong in tags (P0).
- Deep nesting when shallow would do.

### 3. Algorithmic effort belongs on retrieval, not on write-time classification

Spend the algorithmic budget on retrieval, where quality compounds. Classification quality has a hard ceiling set by the accuracy of the classifier, and a write-time classifier won't be that accurate. Vector + BM25 + optional scope filter already beats the hierarchy on its own.

## Two memory layers

Claude Code has two complementary memory layers, used in tandem:

| Layer | Storage | Size | Consolidation | Purpose |
|---|---|---|---|---|
| **Auto-memory** | `~/.claude/projects/*/memory/*.md` | ~17 files (this project) | **Auto Dream** (research preview; beta header `dreaming-2026-04-21`) | Preferences, feedback, context |
| **MemPalace** | palace-daemon (postgres + pgvector + AGE + KG writethrough) | 335K+ drawers | None — deliberately | Verbatim conversations, tool output, code |

**Auto Dream ratifies the verbatim-vs-derivative axis.** Anthropic shipped Auto Dream in two surfaces in late April: a [research-preview consolidator inside Claude Code](https://claudefa.st/blog/guide/mechanics/auto-dream) and a [Dreams API in Managed Agents](https://platform.claude.com/docs/en/managed-agents/dreams). The Dreams API design ratifies this fork's axis: **the input memory store is never modified; the dream produces a separate output store you can review, attach, or discard.** Non-destructive consolidation of verbatim inputs, with a review gate.

**Why MemPalace deliberately doesn't consolidate the verbatim layer.** Both Dreams surfaces consume the *same* JSONL transcripts that MemPalace mines. The pairing is complementary: Auto Dream curates a small high-signal index; MemPalace stores the corpus verbatim for post-hoc retrieval. Consolidating MemPalace's verbatim layer would forfeit exactly the property that makes it useful — the ability to audit, re-derive, and recover the original utterance. The verbatim layer doesn't need consolidation; it needs durability.

## Safety layers (2026-11-15)

Verbatim storage means anything a session printed is a candidate for the palace, so two layers sit between a
transcript and a search result. Neither summarizes or rewrites the verbatim text beyond replacing a secret.

### Redaction on every write

Every backend collection passes the documents and string metadata of each `add`, `upsert` and `update` through
`mempalace.redact` before the backend sees them (`BaseCollection.__init_subclass__`, #596). A secret becomes
`[redacted <kind> sha256:<first 8 hex>]`; existing placeholders are never matched again.

- **Shapes, never values.** Vendor shapes (provider keys, JWTs, vault blobs, one-time codes and PINs: #604, #615),
  credential assignments (`NAME_SECRET=…`, `"password": "…"`, a URL's password), and, since 2026-11-15, prefix-free
  shapes: long mixed-class tokens, long hex runs (except a 40-hex git sha), and quoted runs from the secret
  alphabet (#619, #621).
- **Vault-item needles.** A bare password has no shape. The palace config can list vault item ids and labels (never
  values) under `secret_vault_items`; each value is fetched in process when the patterns load and replaced on word
  boundaries with `[redacted vault <label>]`.
- **Fail closed.** A config that cannot be read, a pattern that does not compile, or a needle that cannot be
  fetched raises `SecretPatternsUnavailable` and the write is refused: a stale palace is visible and recoverable,
  a re-mined password is not. Pattern matching still cannot recognise every secret; the needles exist for known
  values without a shape. Because a locked vault stops all mining, a check in
  [palace-daemon](https://github.com/techempower-org/palace-daemon) (`palace-redact-needles`) loads the patterns as
  a write does, reads the daemon's journal for refused writes, and reports them on the operator's status page; it
  names needle labels only, never values.

### Judged drawer classes: a flag, never a delete

A batch of open-weight judges classified drawers as keep, chatter, stale or secret (the batch judge backend in
[BACKENDS.md](BACKENDS.md)). The design ([designs/judged-drawer-classes.md](designs/judged-drawer-classes.md), #622)
applies a class where the two models' first passes agree; where they disagree, each gives a tie-break verdict, and a
class applies only when at least three of the four verdicts agree and none of them is secret.
For chatter and stale, an acted-on drawer gets three metadata keys (`judged_class`, `judged_at`, `judges`); no
text, embedding or id changes, and rollback removes the keys. A `secret` drawer is different: it is first
re-redacted in place under the rules above (its text changes and its embedding is recomputed), then flagged.
Removing the keys does not undo that redaction, by design; the pre-redaction text is not kept, and the run backs up
the metadata of every row before it writes.

Default search leaves out drawers flagged chatter or stale and says how many it left out (`judged_excluded`, with a
warning naming the flag); `mempalace search --all-classes`, `all_classes=True` on `search_memories` and the
`mempalace_search` tool include them, so a flagged drawer is never unreachable (#635). Anything it cannot prove is
flagged stays visible. As of 2026-11-15 that reader has shipped, the design is approved, and no drawer carries
`judged_class` yet.

### Proposals, not consolidation

The nightly dream writer (#605, #606, #608, #614, #616) publishes an inventory and possible contradictions between
feedback memories and the rules that govern them, for a person to review. It writes no drawer. The judge's verdicts
are kept in a ledger (`judged.json`), so a night with the judge off still reports them.

### Machine-readable surfaces

Many CLI verbs have machine-readable output (`--json`, or `--format json` on search and the read verbs); coverage is
partial. The MCP server exposes `mempalace_search`. The gap an interfaces registry tracks is a machine-readable
`mempalace status --json` (wings, rooms, drawer counts, last mine): #618, open.
