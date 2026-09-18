# Failure-shape retrieval — index by the mistake, keyed on the action

**Status:** design spec, no implementation. **Issue:** #503. **Date:** 2026-09-17.
**Related:** #497 (pre-mutation query — the caller), #451 / #452 (provenance), #477
(curated ordering), #489 (curation gap), #428 (fleet memory umbrella).

---

## 1. What a "failure shape" record is

A **failure shape** is a recurring *mechanism* by which a correct-looking check
produces a wrong answer. The class this spec is built around, named by the 2g-c6
session:

> **An instrument returns a TRUE answer to a NARROWER question than the one asked,
> and its positive control passes.**

Three properties make it worth a record type of its own:

1. **It is invisible to the standard remedy.** The corpus's own rule is *get a
   positive control before trusting a zero*. Here the control passes — it proves
   the reader works, not that the reader was pointed at the question. Every
   instance below had a green control.
2. **It is content-dissimilar from its own trigger.** The card that would have
   prevented "deploy a cert change to a live service" is about *predicate
   selection*. No content-similarity search connects those two strings.
3. **It recurs across unrelated domains.** Git plumbing, shell globbing, process
   tables, RF debugging, markdown diffing. The domain is noise; the shape is signal.

A record therefore has a different shape from a drawer. Minimum fields:

| field | why |
|---|---|
| `shape` | one sentence, the mechanism — *not* the incident |
| `asked` / `answered` | the two questions, side by side. This pair **is** the card |
| `control_passed` | bool. A shape where the control catches it is a different class |
| `triggers[]` | arrival phrasings — what you would SAY or DO just before making it |
| `instances[]` | measured occurrences: date, lane, instrument, one-line evidence |
| `remedy` | the predicate to choose instead, stated as a check |
| `direction` | `false-confidence` \| `false-alarm` — **which way this instrument fails** |

`asked`/`answered` is load-bearing: it is the only field that distinguishes this
class from "a bug happened". If a proposed card cannot fill both, it is not a
failure shape.

`direction` is load-bearing for **retrieval**, not for classification. The corpus
is overwhelmingly `false-confidence` — a green check that measured the wrong thing
— so an index built without the field will answer an agent arriving with *"my check
says the fix broke it"* using cards about distrusting green, which is the opposite
of what they need. Only `harness-indicts-shipping` below is `false-alarm`.

> ⚠️ **Records are cited by `shape` slug, never by ordinal.** §2's rule against
> citing by line number applies to this document too: the corpus grew from 25 to 31
> between two reviews, and every ordinal moved. The slugs below are the identifiers;
> the counts are a derivation, not a naming scheme.

### Seed corpus — 32 on record, 28 individually citable

> 🔴 **The first published count (25/21) was wrong in both directions, and the way it
> was wrong is itself a record in this corpus.** It was taken from the *closing tally
> line* of Oracle PART 25 rather than from that report's section headers. The line
> under-counted; reading it instead of the source then dropped §25.1 and §25.10 and
> admitted two §25.14 near-misses. ⇒ **Derive a corpus count from the sources, never
> from a summary of them** — and state the derivation, as below, so the next reader can
> check it without re-reading everything.

| source | unit counted | on record | citable |
|---|---|---|---|
| Oracle PART 25 (`oracle-pr-review-2.md:4260-4700`) | **sections** holding a qualifying mechanism — §25.1, §25.6, §25.8, §25.9, §25.10, §25.12, §25.13, §25.15 | 8 | 8 |
| This wave — `instruments-and-liveness` §4 (5) + COMMON-DRAIN cards (6) | distinct mechanisms | 11 | 11 |
| 2g-c6's night (#503) | reported errors | 8 | 4 |
| Added since | `bare-path-receipt`, `tally-line`, `sha-prefix-match`, `harness-indicts-shipping`, `self-quoting-retraction` | 5 | 5 |
| **Total** | | **32** | **28** |

⚠️ **Name the unit.** §25.6 and §25.9 each contain *two* sub-errors, so PART 25 is 8
*sections* and ≥10 *sub-instances*. The table counts sections. Excluded by Oracle's own
adjudication of their report: two §25.14 items (a post-merge verification procedure, not
instrument errors that occurred). Four of the 2g-c6 eight are not individually described
in #503, hence 8 on record / 4 citable.

**Oracle PART 25 (eight sections, 2026-09-11):**

| § | asked | instrument | actually answered |
|---|---|---|---|
| 25.1 | is every section still present? | a heading-presence check | does this exact heading *string* appear? (a retitled heading with a byte-identical body read as MISSING — a mis-specified invariant, 3 false reds) |
| 25.6 | is the directory absent? | `ls … \| sed … \|\| echo ABSENT` | did the **last** command in the pipeline fail? (a fallback behind a pipe can never fire) |
| 25.6 | which files are new? | `git status --porcelain` | which **top-level entries** are new? (it collapses untracked dirs: 2 shown, 15 under `-uall`) |
| 25.8 | does the defect exist? | a read of a live uncommitted worktree | did it exist *at read time*? (fixed 2 min later) |
| 25.9 | did the rules survive the split? | a marker-grep over rule lines | how many lines carry a marker glyph? (363 candidates; wrong instrument for survival) |
| 25.10 | does this anchor resolve? | an anchor extractor | does the **truncated prefix** resolve? (the regex split at an escaped backtick, so `A BARE FILENAME IN \"` was the needle) |
| 25.12 | what did this branch do? | `git diff main..branch` | tree-vs-tree — the peer's additions render as this branch's deletions (5 false) |
| 25.13 | is the file present after the merge? | one hardcoded path | is it at *that* path? (a peer's rename falsifies it; blob `f46b601d` identical both sides) |
| 25.15 | are the links there? | `r'…([a-z\\-]+)\\.md'` in a quoted heredoc | is there a literal backslash? (matched nothing; reported "no links" about a fix that had landed) |

> Excluded on Oracle's own re-reading: two §25.14 items (byte-vs-char count and merge-report
> scrutiny). Both are real lessons, but §25.14 is a post-merge **verification procedure** — the
> errors did not occur, so neither fills `asked`/`answered` about an instrument that ran.

**This wave, mempalace (eleven, 2026-09-11 → 09-17):**

| asked | instrument | actually answered |
|---|---|---|
| is this sha on main? | `git cat-file -e` | does this object exist anywhere? |
| is this entry resolved? | `merge-base --is-ancestor HEAD HEAD` | is the tip its own ancestor? (trivially yes) |
| is this field set? | a field grep | does this text appear? |
| what added this file on main? | `git log --diff-filter=A` on a branch | what added it *here*? (squash orphans it) |
| did the data change? | diff of rendered output | did the output change? (loader normalises the differing field) |
| is the phrase present? | `grep -c 'CHANNEL LIST'` | is it present *on one line*? (hard-wrapped → 0 on correct text) |
| how many lines changed? | diff filter `^[+-][^+-]` | …excluding every markdown list line (all start `+- `) → "0 changed" |
| is the branch merged? | `git branch --merged` | is it an *ancestor*? (a squash merge is not) |
| is this file tracked? | `git check-ignore` | would it be ignored? (deleted 5 tracked files under an ignored dir) |
| is the command registered? | a grep for `add_parser(` | is it registered *on one line*? (multi-line call → false 0) |
| is the command wired? | `--help` smoke | does the parser build? (exits before dispatch is read) |

**2g-c6 (eight in one night, four named in #503):** a count that measured code
formatting · a regex that measured spacing · `pgrep` matching its own wrapper · a
frequency table that cannot represent a zero.

**Added since first publication (five, 2026-09-17):**

| slug | asked | instrument | actually answered | direction |
|---|---|---|---|---|
| `bare-path-receipt` | why did the palace search fail? | the #497 hook's receipt under a bare `PATH` | did the command exit non-zero? — it emits `✗ no answer in 2s (5ms)`, **two numbers in one receipt that contradict each other** | false-confidence |
| `tally-line` | how many failure shapes are in Oracle PART 25? | that report's closing tally line | what number did the author *state* in the summary? | false-confidence |
| `sha-prefix-match` | does a doc claim PR #459 is OPEN? | `check-docs.sh`'s PR-state step | does the token "open" appear on a line containing the string `459`? — `/459` matched the **commit sha** `459efab`, and "open" came from "**open**-and-refuse sequence" | false-confidence |
| `harness-indicts-shipping` | does the matcher fix break the check? | the two-way control written to prove it | *(nothing — the harness was broken)*: `\|` used as a field delimiter in strings containing `\|`, so both arms reported BLIND | **false-alarm** |
| `self-quoting-retraction` | does the fix remove the warning? | `check-docs.sh` after the fix, on the tree that **documents** the fix | does any line pair the number with a state word? — the changelog entry describing the defect re-supplied both, so the warning returned on a correct fix | false-confidence |

Four of these were produced **by this document or by fixing it**, and that is the finding:

- `tally-line` is the spec's own definition reproduced against the report that seeded
  it, by its author, in the same week. ⭐ **The failure survives being written down by
  the person who named it.** Oracle asked for it to be recorded, and reads it as
  corroboration of the thesis rather than a mark against the spec. It is also the
  upstream cause of the 25/21 miscount corrected above.
- `sha-prefix-match` was found by this spec's own lens within an hour of publication,
  in `scripts/check-docs.sh`, with the surrounding six checks green.
- `harness-indicts-shipping` came out of *fixing* `sha-prefix-match`, and it is the
  first `false-alarm` on record. A broken control reported that the fix broke the
  check; taken at face value it would have discarded a correct change.
  ⭐ **The reusable catch is not "check your harness" but: when an instrument indicts
  the SHIPPING behaviour, that is the implausible arm — check the instrument before
  the subject.** The old pattern is *known* to warn on #459; an arm reporting that it
  does not contradicts a measurement already in hand, and a contradiction with a
  measurement you already hold is the cheapest signal that the instrument moved rather
  than the world.
- `self-quoting-retraction` closed the loop: the changelog entry *documenting*
  `sha-prefix-match` re-supplied the number and the state word the checker matches on,
  so a correct fix still warned. 2g already cards this shape — *a retraction quotes its
  target, so `grep -c "<the false claim>"` can never reach zero* — and it is filed here
  as a recurrence, not a discovery. Remedy: keep the **real** state adjacent to the
  reference, so a checker reading the line learns the truth rather than half of it.

> ⚠️ Four of the 2g-c6 eight are not individually described in #503, so the corpus is
> **32 on record, 28 citable**; any retrieval measurement must state which set it used.
> One further candidate is **unadjudicated**: Oracle's §27.4 `grep -c 'failure-shape'`
> → 0 against rendered text reading "failure SHAPES" (different case, space not hyphen).
> It is arguably the `keyed-on terms` regime of §2 rather than this class; it is left
> out of the count until someone rules, and is recorded here so the omission is visible.

---

## 2. Why index by arrival phrasing — 2g's measurement

`~/Projects/2g/docs/LAWS-INDEX.md` exists because a 468 KB corpus of correct cards
was unreachable in practice. Its author measured why, n=11, phrasing each situation
the way an agent actually meets it:

```
ARRIVAL PHRASINGS   10 of 11 return ZERO, and every one names a card that EXISTS
  "false clean" 0 · "silently skips" 0 · "option parsing" 0 · "stale issue" 0
  "already fixed" 0 · "double count" 0 · "wrong denominator" 0 · "premise moved" 0
  "parsed as an option" 0 · "publish the fix" 0    ("partial enumeration" 1 — lone hit)

KEYED-ON TERMS      work, if you already know the word
  "positive control" 10 · "truncat" 17 · "denominator" 14 · "specimen" 7

TOO COMMON          return hits, locate nothing
  control 113 · instrument 107 · measured 138 · zero 95 · grep 105
```

Three regimes, and the middle one is the trap: **retrieval works exactly when you
already know the card exists.** The index's response was not to rewrite the cards
but to add a lookup keyed on *"what am I about to do / what would I say"* — the
`if you would say…` column. It cites **search anchors and commit shas, never line
numbers**, after line numbers broke on the very commit that made the index findable.

Two properties transfer directly to the palace:

- **Key on the arrival, not the subject.** The palace indexes what a drawer *says*.
  An agent arrives with what it is *about to do*. Those are different strings, and
  §1's property 2 says they are not content-similar.
- **Cite something stable under motion.** A palace analogue of the anchor/sha rule:
  cite a shape by a stable `shape_id`, never by drawer id or rank, both of which
  move on every re-mine (measured: a card moved rank 13 → 1 on an unmodified tree
  within 30 minutes, #477).

---

## 3. Retrieval keys on the action sentence

**#497 is the caller.** Its pre-mutation hook already does the hard half: it matches
mutation shapes (`systemctl restart|reload|stop`, `deploy.sh`, `scp`/`rsync` to a
host, writes under `/etc`, `*.service`, `git push --force`, `rm -rf` outside the
repo, cert/key writes), **builds a short action sentence**, and runs
`mempalace search "<sentence>" --limit 3 --format compact` with a 2 s timeout,
advisory-only, exit 0 always.

So the query key already exists and is already being produced at the right moment.
What is missing is anything on the answering side that a *verb phrase* can match.

```
  #497 hook  ──"about to deploy a cert change to a live service"──▶  search
                                                                       │
  today:   content similarity over drawers  ──▶ transcripts that mention certs
  wanted:  trigger match over shape records ──▶ "a check that passed on the
                                                 narrower question" + its remedy
```

The auto-query hook fires on **topic changes**; #497's own observation is that the
expensive misses happen at **actions**, where no topic changed. A failure-shape
record is the first record type in the palace whose key is a trigger rather than a
subject, which is why it is the right thing to index first: it is the only class
where the caller's string and the card's string have no lexical overlap by design.

---

## 4. What the palace already has, and what it lacks

**Has — reusable today:**

- **`source_kind` (#452)** — `transcript` · `memory` · `diary` · `file` · `unknown`,
  plus `source_stale`, `source_indexed_at`, `provenance_note`. A shape card is a
  curated file, so it inherits the highest-trust classification for free, and
  `all_transcript()` / `no_curated_source()` already exist as result-set predicates.
- **Curated ordering (#477)** — curated hits already rank above the transcripts that
  quote them. A shape corpus is curated by construction, so it lands on the right
  side of an ordering rule that is already shipped and measured.
- **`why`** — explains why a drawer surfaced (location, tags, graph, tunnels).
  The natural place to render *which trigger matched*, with no new surface.
- **`tunnels`** — cross-wing links. A shape recurring in `2g` and `memorypalace` is
  literally a tunnel; the mechanism for "this shape is not domain-specific" exists.
- **`rate`** — records feedback that nudges ranking. The obvious signal channel for
  "this shape was the right one" without building a new feedback path.
- **KG / `cypher` / `walk`** — an edge type from shape → instance → wing is
  expressible in AGE today.

**Lacks:**

1. **No trigger field.** Nothing in a drawer is indexed as "the thing you would be
   doing when you need this". Content search cannot synthesise it.
2. **No shape/instance distinction.** A drawer that *describes* a failure mode and
   one that merely *mentions* one are the same object to search. #489 is the same
   gap from the other side: a conclusion that exists only inside a diff chunk.
3. **No labelled retrieval test.** There is no harness that asks "for query Q, is
   card C in the top N?" over a fixed corpus — so no change to ranking can currently
   be shown to help or hurt. This is the real blocker, and it is cheap to remove.
4. **Ranking instability under re-mine** (#477's rank 13 → 1) means any measurement
   must interleave baseline and candidate per query, never run them as two halves.

---

## 5. Minimal first slice — one PR, and it measures before it builds

**Do not ship an index first.** The honest first slice is 2g's own method applied
to the palace: *measure the retrieval gap on a labelled corpus.*

**One PR delivers:**

1. `docs/failure-shapes/` — the 21 citable instances as YAML, one file per shape,
   fields per §1. Curated, mineable, `source_kind: file`.
2. `scripts/failure_shape_recall.py` — for each shape, issue its `triggers[]`
   against the live search at `--limit 3`, record whether the shape's own drawer is
   returned. Emits recall@3, recall@10, and the per-trigger table.
   **Interleaves baseline and candidate per query** (#477 instability).
3. The measurement, committed as a dated result — the `n=11`-equivalent for this
   corpus, at whatever n it actually reaches.

No new CLI verb, no ranking change, no daemon route. Roughly the size of #493.

### What would falsify the idea

| outcome | reading |
|---|---|
| baseline recall@3 is **high** (say ≥0.6) on arrival phrasings | the index is unnecessary — plain search already connects actions to shapes; close #503 |
| baseline low, and **trigger-indexed retrieval does not beat it** | the bottleneck is the embedding, not the index; a trigger field is the wrong fix |
| baseline low, trigger retrieval **beats it but only on triggers authored with the card** | the index is overfit — it retrieves phrasings someone already thought of, which is the keyed-on-terms regime, not arrival |
| baseline low, trigger retrieval beats it on **held-out phrasings written by a different lane** | the idea holds; proceed to a retrieval surface |

That third row is the one to guard hardest, and it is the reason the triggers for
the held-out test **must be written by a lane that did not author the cards** —
otherwise the measurement reproduces 2g's "keyed-on phrasings work" regime and
reports it as success.

> **Pre-register the expected answer before running the harness** (Oracle PART 23):
> write down the predicted recall@3 for baseline, then diff. A harness whose result
> is read without a prediction is an instrument nobody has proven can see — which is
> the very shape this corpus exists to index.
