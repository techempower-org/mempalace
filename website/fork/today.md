# Where the fork stands

*Last updated 2026-11-15.*

This is TechEMPOWER's fork of MemPalace. We run it every day: over a million drawers of verbatim
conversations, tool output and code, in PostgreSQL with pgvector, served by
[palace-daemon](https://github.com/techempower-org/palace-daemon). Here is what changed lately, in plain words.

## Redaction before storage

A session can print a password or a key without anyone meaning to. Mined as is, it would sit in a drawer that any
agent can search. So every write goes through a redaction step first, and a secret becomes a placeholder like
`[redacted openai sha256:1a2b3c4d]`.

- **Shapes, not values.** We look for the shape of a credential: provider keys, tokens, JWTs, one-time codes,
  `NAME_SECRET=…` assignments, and since November, long random strings that have no telltale prefix.
- **Known passwords too.** A plain lower-case password has no shape. For those, the palace config can name items in
  your password vault (by id and a label, never the value). Their values are loaded when the palace starts writing,
  and any copy of one in new text is replaced with `[redacted vault <label>]`.
- **Fail closed.** If the patterns can't load, for example because the vault is locked, the write is refused. A
  palace that stops filling up is visible and fixable; a password mined again is not. A check reports refused
  writes, so a stopped palace doesn't go unnoticed.

Pattern matching can't recognise every secret. It catches the ones with a shape and the ones you name.

## Cleaning up without deleting

Some drawers are noise: "ok", progress lines, a status that stopped being true months ago. We asked two open-weight
models to sort drawers from our engineering wings into *keep*, *chatter*, *stale* and *secret*. Only redacted
drawers left our server, they went to a rented GPU machine that was destroyed afterwards, and the models wrote
proposals, not changes.

The rule for acting on a proposal is strict:

- if both models agree, that's the class;
- if they disagree, each gets a second look that shows both earlier answers, and a class applies only when at
  least three of the four answers agree and none of them said *secret*.

Acting means a metadata flag, never a delete. Flagged chatter and stale drawers drop out of default search, and
`mempalace search --all-classes` brings them back. That search change shipped first, on purpose; no flag has been
written yet.
A *secret* drawer is re-redacted in place first, which can't be undone, on purpose.
The full design is [in the repo](https://github.com/techempower-org/mempalace/blob/main/docs/designs/judged-drawer-classes.md).

## Keeping up with upstream

We don't merge upstream wholesale; we pick. Upstream has 281 commits since our last sync point. Each one is listed
with what we do about it (pick now, later, or skip) and why, in
[the sync triage](https://github.com/techempower-org/mempalace/blob/main/docs/UPSTREAM-SYNC-2026-11-15.md).
The bug and safety fixes on our path come in first, in small reviewed pull requests.

## Still to do

- A machine-readable `mempalace status --json` (wings, rooms, drawer counts, last mine):
  [#618](https://github.com/techempower-org/mempalace/issues/618).
- The run that writes the judged flags, under the strict rule above.

More detail: [Architecture](https://github.com/techempower-org/mempalace/blob/main/docs/ARCHITECTURE.md#safety-layers-2026-11-15),
[Backends](https://github.com/techempower-org/mempalace/blob/main/docs/BACKENDS.md),
[Fork changelog](https://github.com/techempower-org/mempalace/blob/main/FORK_CHANGELOG.md).
