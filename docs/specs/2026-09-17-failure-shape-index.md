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

`asked`/`answered` is load-bearing: it is the only field that distinguishes this
class from "a bug happened". If a proposed card cannot fill both, it is not a
failure shape.

### Seed corpus — 25 measured instances, 21 individually citable

**Oracle PART 25 (six, 2026-09-11):**

| asked | instrument | actually answered |
|---|---|---|
| does the defect exist? | a read of a live uncommitted worktree | did it exist *at read time*? (fixed 2 min later) |
| what did this branch do? | `git diff main..branch` | tree-vs-tree — peer's additions render as this branch's deletions |
| is the file present after merge? | one hardcoded path | is it at *that* path? (a rename falsifies it; blob identical) |
| is it under the char limit? | a byte count | how many bytes? (23,309 B vs 22,936 chars, UTF-8 emoji) |
| did the merge take both sides? | "it merged" | did a command exit 0? (`rev-list --parents` shows the real answer) |
| are the links there? | `r'...([a-z\\-]+)\\.md'` in a quoted heredoc | is there a literal backslash? (matched nothing; reported "no links" about a landed fix) |

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

> ⚠️ Four of the 2g-c6 eight are not individually described in #503. The corpus is
> **25 on record, 21 citable**; any retrieval measurement must state which it used.

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
