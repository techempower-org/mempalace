# Judged drawer classes: applying a three-judge triage without deleting anything

Status: **approved as a design, with the conditions below; not applied.** Nothing in it deletes a drawer.

## What exists

On 2026-11-14/15 three LLM judges triaged 318,670 drawers from engineering wings into one of four classes. Every record was redacted on the palace host before it left, and the judges ran on a temporary GPU host that has since been destroyed. The classes:

| class | meaning |
|---|---|
| keep | a decision, fact, fix, command or reference worth finding again |
| chatter | nothing to reuse (acknowledgements, progress noise, bare tool output) |
| stale | a status, plan or time-bound claim that is likely no longer true |
| secret | a live-looking credential that is not a `[redacted …]` placeholder |

The judges:

- **Judge 1:** Qwen2.5-72B-Instruct.
- **Judge 2:** Llama-3.1-Tulu-3-70B. About half of its inputs also passed outbound scrub v3 (#619).
- **Judge 3, tie-break:** Tulu-3-70B with a third prompt. It ran only on the 11,918 drawers where the first two disagreed, saw both earlier verdicts and their reasons, and gave a final class.

## Conditions of approval (lead, 2026-11-15)

1. **Four verdicts, strict majority.** On the disagreement set every drawer has four verdicts: Qwen first pass, Tulu first pass, the Tulu tie-break, and the Qwen tie-break. The run acts only on a strict majority there, at least 3 of 4, and only when no judge said `secret`. Ties (2–2) and three-way splits stay untouched and are counted in the report. Outside the disagreement set the first two judges agreed, and that agreement is the majority.
2. **Reachability first.** The search-exclusion code PR ships before any flag is written. It must include a red test and the include flag: `mempalace search --all-classes` and the daemon API's equivalent. A flagged drawer must never become unreachable.
3. **Backup first.** Before the run writes, it backs up the metadata of every row it will flag, in the same form as the 2026-11-15 redaction backup (row-level COPY, 0600, sha in a manifest).

The tables below predate condition 1. The run recomputes them under it and reports the final counts.

## The rule: act only where two of three agree

A class is applied only when at least two judges gave it: either the first two agreed, or the tie-break sided with one of them. Anything else is left alone.

| class | two of three | first two agreed | via tie-break | drawers still present |
|---|---|---|---|---|
| keep | 313,138 | | | (no action) |
| chatter | 3,092 | 1,838 | 1,254 | 3,092 |
| stale | 2,178 | 1,177 | 1,001 | 2,178 |
| secret | 233 | 76 | 157 | 233 |
| no two agree | 24 | | | (no action) |

The first two judges agreed on 96.3% of drawers overall, but much less on the three action classes. That disagreement is the reason for the tie-break.

## Mechanism: a flag, never a delete

1. **Flag.** Each acted-on drawer gets three metadata keys:
   - `judged_class`: chatter, stale or secret;
   - `judged_at`: ISO time of the run;
   - `judges`: a short list, for example `["qwen2.5-72b", "tulu3-70b", "tiebreak"]`.

   The write is a metadata merge (`metadata || {...}`) in batches. It changes no document text, embedding or id. No drawer carries `judged_class` today; this was counted on 2026-11-15.
2. **Search.** Default search excludes `judged_class` in (chatter, stale). A flag includes them: `all_classes=True` in the API (`mempalace_search` tool, `search_memories`), `--all-classes` on the CLI, and `all_classes` on the daemon's `/search`, `/search/fast`, `/search/hybrid`, `/search/keyword`, `/search/age-fused` and `/context`. A result that left drawers out says so in `judged_excluded` and a warning naming the flag.
   - This is a separate code change in mempalace and palace-daemon, with its own PR and tests.
   - The exclusion shipped before any flag was written (condition 2). It fails open: a drawer is left out only when its stored row is read and proven flagged.
   - `secret` is not a search exclusion; see the next step.
3. **Secret.** Each `secret` drawer is first re-redacted in place under the #619 rules: the prefix-free shapes, plus the vault-item needles. The redacted text replaces the document, and its embedding is recomputed by the backend's own embedder.
   - Then the drawer is flagged `judged_class=secret`, so a later report can list secret-flagged drawers that still match a rule.
   - A drawer whose text the redaction did not change keeps the flag and appears in a count for a person to read.
   - Its id appears in that count; its text and value never do.
4. **Rollback.** Remove the three keys (`metadata - 'judged_class' - 'judged_at' - 'judges'`) from the same id list.
   - The id list per class is kept in the run's ledger, ids only.
   - Re-redaction of a secret drawer is not rolled back: the redacted form is the intended state, and the pre-redaction text is not kept.

## When and how it runs

- **When:** inside the nightly dream window (03:30–04:30 local), so it does not overlap the hourly mines, and in batches of 500 ids.
- **Watching:** the same p95 sampler as the knowledge-graph orphan sweep, every 5 min over the run: 30 fixed `/search` queries.
- **Stop rule:**
  - p95 above 2× the baseline on two samples in a row; or
  - the run past 30 minutes.

  On a stop the run ends at a batch boundary. What it already wrote stays (flags are cheap to keep), and the next night resumes from the ledger.
- **Order:** secret first (re-redact, then flag), then stale, then chatter.
- **Report, one line per class:** flagged, skipped (the drawer is gone or re-mined), and errors. For secret, also the rows the re-redaction changed and the count still matching a rule.

## What this does not do

- It deletes nothing. A wrong verdict costs one rollback, not data.
- It does not act on `keep`, or on the 24 drawers where no two judges agree.
- It does not re-judge drawers mined after the run. A later run does that, with the same two-of-three rule.
