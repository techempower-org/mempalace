# docs/fork-changes/ — one file per fork-ahead change

Each `*.yaml` here is **one** changelog entry. `FORK_CHANGELOG.md`, the
README fork-change table and `website/public/llms-full.txt` are all
rendered from this directory by `scripts/render-docs.py`. Never edit
those by hand.

Add a change by adding **one new file**. Do not edit a shared file.

    docs/fork-changes/2026-09-11-my-change.yaml

## Ordering is `seq` descending — please do not "fix" this to a date sort

Presentation order is:

1. `seq` **descending** (higher = newer, so it sorts first)
2. then `date` **descending**
3. then `id` **ascending**

`id` ascending is the one non-recency term, which is what makes the
tie-break readable rather than arbitrary.

**This looks like it should just be a date sort. It cannot be.** The
changelog is in hand-curated narrative order and is *not*
date-descending; sorting 137 entries by `(date, id)` was measured to move
**112 of them**, which turns any layout change into a full rewrite of
`FORK_CHANGELOG.md`. `seq` is what preserves that order (#473).

### Why an explicit number does not recreate the conflict it replaced

The old single-file manifest made every pull request insert at the same
line, so a 10-PR wave conflicted on it every time — measured on
2026-09-10/11, with *zero* source conflicts.

A `seq` **collision is harmless**: two lanes both choosing `max + 1`
write *separate files*, and the tie breaks deterministically. The
conflict was never the number — it was a shared insertion *line* in a
shared *file*. A numeric collision costs nothing; a textual one blocks a
merge. So ordering can stay explicit without being a coordination point.

Get the next number with:

    scripts/fork_changes.py --next-seq

## `commit: HEAD` is correct on a branch — do not write your branch sha

The squash-merge commit **does not exist** while your PR is open, and a
branch sha becomes unreachable from `main` the moment it merges. Thirteen
entries shipped pointing at commits that exist in nobody's clone that
way (#472).

Write `commit: HEAD`. After the merge,
`scripts/maintain-fork-changes.py` resolves it from **the commit that
added this file** — which is the squash commit by construction, since
the file arrives with the pull request:

    git log --follow --diff-filter=A --format=%H -- docs/fork-changes/<file>

That needs nothing from you: a lane cannot know its own PR number while
writing the entry, which is the same chicken-and-egg as the sha. It is
also deterministic, offline, and immune to a squash subject reworded at
merge time — which defeated 4 of the 27 cases in the #472 sweep.

`fork_pr: <your PR number>` is therefore **documentation and a
cross-check, not load-bearing**. Add it if you know it; when present the
resolver compares it against the file-add answer and refuses to resolve
if the two disagree, since two independent mechanisms disagreeing is the
worst case in which to guess.

⚠️ The resolver only ever does this for an entry whose `commit` is
literally `HEAD`. Every entry file that predates the one-file-per-entry
split was created by the split's own commit, so asking "what added this
file" about an already-resolved entry returns the migration commit — it
would rewrite 137 correct historical shas to one wrong value, and that
value is an ancestor of `main`, so it would pass the ancestry check
forever and look right. The merge step records the final sha; the lane
does not.

`scripts/check-docs.sh` step 2b then requires every entry's commit to be
an **ancestor** of `HEAD` — not merely to resolve, which a dangling
object does.

A handful of pre-#472 entries cannot be resolved at all: their change is
on `main`, but the squash subject was rewritten at merge so the commit
cannot be named. Those are listed in `docs/fork-changes-legacy-shas.txt`
as `<id> <known-bad sha>` pairs, and step 2b skips **only** that exact
pair. Editing such an entry's `commit:` to any other value makes it fail
the check again — the exemption covers one recorded sha, not the entry
forever.

## Fields

| field | required | notes |
|---|---|---|
| `seq` | yes | `--next-seq`; see ordering above |
| `id` | yes | kebab-case, unique, used as the changelog anchor |
| `date` | yes | `YYYY-MM-DD`, also the filename prefix |
| `bucket` | yes | `Added` / `Changed` / `Fixed` / `Performance` |
| `commit` | yes | `HEAD` on a branch; a 7-char sha after merge |
| `area` | yes | `Reliability` / `Search` / `Performance` / `CLI` / `Docs` / `Testing` |
| `summary` | yes | one line, < 100 chars |
| `body` | no | markdown paragraphs, block scalar (`body: \|`) |
| `tests` | no | count + class names |
| `fork_pr` | no | **this** repo's PR number — lets the merge resolve `commit` |
| `pr` | no | the **UPSTREAM** PR number (`MemPalace/mempalace`) — not this repo's |
| `pr_state` | no | `OPEN` / `MERGED` / `CLOSED` at last check |
| `files` | no | repo-relative paths the change touched |
| `supersedes` | no | list of entry `id`s this replaces |

`fork_pr` and `pr` are **different repositories** and must not be
confused: `pr` renders as "*Upstream:*" and `check-docs.sh` queries it
against `MemPalace/mempalace`.

Non-entry manifest keys (currently `merged_upstream`) live in
`docs/fork-changes-meta.yaml`, beside this directory: they change when an
*upstream* PR merges, not once per fork PR.

## After adding your file

    scripts/render-docs.py && scripts/render-llms-full.py && scripts/render-api-docs.py
    scripts/check-docs.sh

The schema and loader live in `scripts/fork_changes.py`.
