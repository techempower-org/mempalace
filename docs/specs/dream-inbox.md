# Dream Inbox Contract

- **Status:** Live
- **Date:** 2026-11-17
- **Related:**
  - `mempalace/dream/route.py`: the writer (`python -m mempalace.dream nightly --route`)
  - `mempalace/dream/nightly.py`: the nightly run and its review document (`dreams/v1`)
  - The fork changelog entry `dream-route-proposals` (#641)

The nightly dream proposes changes to memories and rules. Its review document is read in one
place, by one person. A proposal about one project is easier to act on in that project, so
`--route` also puts it in the project's own inbox. This page says what that inbox is, who
writes it, who changes it, and how a proposal counts as acted on.

## The file

`<project>/scratch/dream-inbox.md`, one per project folder that a proposal names. A proposal
names a project through its sources:

- a memory folder whose name is the project's path, encoded the way the harness encodes it;
- or a path inside the project folder under `--projects-root`.

A proposal whose sources name no project (a global rules file) is not routed and stays on the
review page.

The file is written only when git says it can never be tracked: the folder is outside every git
work tree, or git ignores `scratch/dream-inbox.md`. It is created with mode 0600 and kept at
0600.

## The line

```text
- [ ] <lane> · <proposal id> · <one-sentence ask> (dream <YYYY-MM-DD>, <kind>)
```

- `<lane>` is the project's lane from the optional `--lanes` map, else the folder name. Only
  letters, digits and `._+-` are used.
- `<proposal id>` is the stable id the review page shows, the same from night to night.
- `<one-sentence ask>` is the proposal summary's first sentence, at most 240 characters.
- `<kind>` is `no-mechanism`, `near-duplicate`, `oversized-index` or `contradiction`.

## Who writes what

| Who | May | Never |
|---|---|---|
| The dream | append new lines; close a torn last line before appending | rewrite, reorder or delete a line; write one proposal twice to one project |
| The project's lane, or whoever works in it | tick a line: `- [ ]` to `- [x]` | edit the text or the id; delete lines |
| The project owner | decide a proposal (accept or reject) on the review page | (nothing in this file) |

**When to tick.** Tick a line once the proposal is settled for the project. Either you fixed it
at its source, so the next complete night stops proposing it, or it needs the owner's
judgement and you handed it to the review page (named it in your report or asked for a
decision there). The tick is the lane's own bookkeeping. The dream never reads it.

## Acted on

The routing record is kept in the review folder's `judged.json`, under the reserved key
`_routing`, one entry per routed proposal id: its kind, where it went, and `acted`. A routed
proposal counts as **acted** when:

- it is decided (accepted or rejected) on the review page; or
- a night whose producer for its kind ran complete no longer proposes it. A night without the
  model judge never closes a `contradiction`.

A proposal that comes back with no decision on it is open again. `acted` measures what
happened to the proposal, not whether its box is ticked.

## Readers

- **A session in the project.** The SessionStart hook says
  `dream-inbox: N open lines in scratch/dream-inbox.md` when the nearest inbox from the session
  folder upward has unticked lines, and nothing when it has none.
- **A lead's daily pass.** `python -m mempalace.dream routed --out <review folder>` prints
  `dreams: N proposals routed, M acted`, from `judged.json`.

PERTURBATION: [a link to nowhere](./no-such-file-dream-inbox.md).
