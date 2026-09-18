# Failure-shape records

One file per record, `<slug>.md`: Markdown with the fields below as **YAML front
matter**, then the same fields rendered as prose. The design and the scoring rules live
in [`../specs/2026-09-17-failure-shape-index.md`](../specs/2026-09-17-failure-shape-index.md);
this file is the schema card. This README is not a record and the harness skips it.

## Fields

| field | type | meaning |
|---|---|---|
| `slug` | string, `[a-z][a-z0-9-]+` | the identifier. **Must equal the filename stem** (below) |
| `shape` | string | one sentence, the mechanism — never the incident |
| `asked` | string | the question the agent needed answered |
| `instrument` | string | what was consulted to answer it |
| `answered` | string | the narrower question the instrument actually answered. `asked`/`answered` is the pair that makes this a failure shape rather than a bug |
| `control_passed` | bool | whether the positive control on the instrument passed (it did, in every record so far — that is the class) |
| `direction` | `false-confidence` \| `false-alarm` | which way this instance fell |
| `direction_basis` | string (`as-observed`) | why the direction is what it says: a property of the (mechanism, question) pair, recorded, not inferred |
| `provenance` | enum, below | which source the record was written from |
| `instances[]` | list | measured occurrences: `source` (string), `ref` (string — a path, issue or PR), `date` (ISO date, quoted), optional `detail` (string) |
| `not_this` | string, optional | the neighbouring record this one is **not**, when two are easily confused |

## Slug equals filename

`slug:` in the front matter must equal the file's stem: `tally-line.md` declares
`slug: tally-line`. This is not a convention; it is enforced by

    scripts/failure_shape_recall.py --check-set-only

which reads the slug from the front matter (so a renamed file can be detected), compares
it to the stem (which is what detects it), and diffs the on-disk set against the spec's
runnable table both ways. `scripts/check-docs.sh` runs it as one of its steps, so a
record added, renamed or listed in the spec without its file fails the docs gate.

## Provenance and partitions

`provenance` says where the record's text came from:

| value | meaning |
|---|---|
| `A-oracle` | written from an Oracle review section |
| `A-wave` | written from a wave's COMMON-DRAIN lesson or an `instruments-and-liveness` card |
| `2g-sourced` | written from the 2g-c6 session report |
| `authored-here` | produced by the work on this index itself |
| `added-by-review` | added after publication, on a reviewer's ruling |

Provenance is about the **record**. The recall harness's **partitions** are about the
**trigger** — who wrote the arrival phrasing and what they had read:

- **A** — triggers written by the record's author. Measures whether an author can find
  their own words: the regime retrieval already handles.
- **B-blind** — triggers written by a lane that had the slug list but not the records'
  `asked`/`answered`.
- **B-TBD** — triggers to be written by a fresh lane briefed from incident sources only.

The harness scores each partition separately and prints no aggregate. Pooling A with
B reports an author's recall as a reader's. Beside recall it reports two token-overlap
columns (trigger vs slug, trigger vs `asked` + `answered`); they explain a hit, they
never discount one.

## Writing a value

**Do not wrap a value that already carries backticks.** The record generator wrapped
every `instrument` value in a code span; values that already contained one closed the
span early, and in one file the remainder happened to parse as a reference link — CI's
markdownlint named that one file (MD052), the defect was in seventeen, fixed one line
each in #531. If a value contains backticks, write it as it is; if it needs quoting for
YAML, use single quotes and double any embedded single quote.
