# `mempalace.cli.cmd_query`

Source: [`mempalace/cli/cmd_query.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_query.py)

## Functions

### `cmd_search`

```python
def cmd_search(args)
```

### `cmd_list`

```python
def cmd_list(args)
```

Fast direct-to-daemon drawer browser (issue #191).

Pure metadata listing — wraps ``GET /list?wing=&room=&limit=&offset=``
on the palace daemon. Output formats: ``table`` (default), ``compact``,
``full``, ``json``. Daemon unreachable → stderr error + exit 1.

### `cmd_move`

```python
def cmd_move(args)
```

Fast direct-to-daemon single-drawer relocation (issue #191).

Wraps ``PATCH /memory/&#123;drawer_id}`` with a body carrying only the
supplied ``wing`` / ``room`` keys. At least one is required — an empty
PATCH is an ambiguous no-op the daemon would 400, so we refuse it
client-side with a clear message. No ``--content`` flag exists by
design: verbatim-always means the human CLI never edits drawer text.
Daemon unreachable / 404 / 401 / 403 → exit 1 (sibling parity with
cmd_list / cmd_graph / cmd_cypher / cmd_stats); inner-error envelope
(daemon reachable but the move failed) → exit 2.

### `cmd_bulk_move`

```python
def cmd_bulk_move(args)
```

Bulk drawer relocation by source wing/room (issue #191).

The multi-drawer complement to ``move``. Selects drawers via
``GET /list`` (offset-paginated) and PATCHes each match to the target
wing/room. Dry-run by default; ``--apply`` mutates (TTY prompt unless
``--yes``; refuses unattended without ``--yes``). Verbatim-always:
metadata only, no ``--content`` flag. Daemon unreachable / 404 / 401 /
403 during listing → exit 1; selection/target missing or any PATCH
failure → exit 2.

### `cmd_graph`

```python
def cmd_graph(args)
```

Fast direct-to-daemon KG + palace structural snapshot (issue #191).

Pure read — wraps ``GET /graph?limit=`` on the palace daemon, which
returns pre-aggregated wing/room/tunnel counts plus a KG slice
(top-N entities, sample RELATION/MENTIONS triples, global kg_stats).
Output formats: ``table`` (default summary), ``full`` (every wing,
every sampled triple, no truncation), ``json`` (pass-through shape).
Daemon unreachable → stderr error + exit 1; inner-error payload → exit 2.

### `cmd_cypher`

```python
def cmd_cypher(args)
```

Run a read-only Cypher query against the AGE knowledge graph (issue #191).

Wraps the daemon's ``POST /cypher``, which executes inside a
``READ ONLY`` postgres transaction (write verbs fail with HTTP 403,
SQLSTATE 25006). Output formats: ``table`` (aligned columns),
``json`` (pass-through), ``csv`` (pipe-friendly). The optional
``--limit`` is advisory — the daemon's own statement_timeout is the
real ceiling.

Daemon unreachable → stderr error + exit 1; 403 read-only write
attempt → friendly hint + exit 2; inner-error payload → exit 2.

### `cmd_wakeup`

```python
def cmd_wakeup(args)
```

Show L0 (identity) + L1 (essential story) — the wake-up context.

Daemon-routes when ``_daemon_strict()`` is on and the user didn't
pass ``--palace`` (#285). The daemon-native ``mempalace_wakeup``
tool (palace-daemon #96) returns ``&#123;text, tokens, wing}``; we
print in the same shape as the local-path fallback below.

### `cmd_split`

```python
def cmd_split(args)
```

Split concatenated transcript mega-files into per-session files.

### `cmd_export`

```python
def cmd_export(args)
```

### `cmd_migrate`

```python
def cmd_migrate(args)
```

Migrate palace from a different ChromaDB version.

### `cmd_migrate_to_postgres`

```python
def cmd_migrate_to_postgres(args)
```

Migrate a ChromaDB palace to Postgres (pgvector + AGE).

Different from `cmd_migrate` (which handles intra-ChromaDB version
upgrades). This one moves the entire substrate. See
`mempalace/migrate_to_postgres.py` for the 7-phase pipeline.

### `cmd_rooms`

```python
def cmd_rooms(args)
```

Manage the canonical room set (mempalace_canonical_rooms table).

hybrid-search-taxonomy follow-up. The FK constraint on mempalace_drawers.room
means this CLI is the supported way to add/rename/remove canonical
rooms without breaking the DB. ON UPDATE CASCADE on the FK makes
renames safe (all drawers auto-update); removes fail if any drawer
still in the target room.

Daemon-routes when ``_daemon_strict()`` is on (#285). palace-daemon
PR #96 added the four ``mempalace_rooms_&#123;list,add,rename,remove}``
tools that wrap the same SQL the local path runs. When daemon-strict
is off, falls back to direct postgres via ``MEMPALACE_POSTGRES_DSN``.

### `cmd_purge`

```python
def cmd_purge(args)
```

Delete drawers by wing, room, and/or source-file.

Uses ``collection.delete(where=...)`` — the backend's filter-delete path
doesn't go through ``updatePoint`` / ``repairConnectionsForUpdate``,
which is the upsert-only race from #521 that an earlier draft of this
command tried to side-step with a nuke-and-rebuild. The simpler path
works without losing drawers if the process is interrupted, without
re-embedding the survivors under a default model, and without
bypassing the backend abstraction.

``--room`` without ``--wing`` purges that room across ALL wings.

Where it runs (#418): daemon-strict with no ``--palace`` routes to the
daemon's palace; otherwise the backend is resolved *before* any
palace-directory precheck, and the precheck is skipped entirely for a
service-backed store, which has no local database file by design. The
resolved target is printed on every outcome, including the zero-match
one, which exits 1 rather than 0.

### `cmd_prune`

```python
def cmd_prune(args)
```

Delete drawers older than ``--stale-days N`` (dry-run by default).

Age is the span between a drawer's ``filed_at`` timestamp and now. Unlike
``purge``'s metadata-equality filter, the staleness predicate is a string
timestamp that chromadb ``where=`` can't range-compare reliably, so we
fetch candidate metadata and decide age in Python (``mempalace.recency``),
then delete by explicit id list.

Safety: this is the only command that destroys data on a *time* predicate
rather than an explicit selection, so it is **dry-run by default**. Nothing
is deleted unless ``--confirm`` is passed. A drawer with no parseable
``filed_at`` is treated as ageless and is **never** pruned — we never
delete a drawer we can't date.

### `cmd_rename_wing`

```python
def cmd_rename_wing(args)
```

### `cmd_pending`

```python
def cmd_pending(args)
```

Dispatch the ``pending`` verb group.

A verb group named without an action is a usage error, the same class
as argparse's own "invalid choice" — so it exits 2, not 64. 64 means
exactly one thing: the daemon answered and rejected a well-formed
request. This guard never reaches the daemon.

It goes through ``_fail_client`` rather than printing and exiting
inline, because a hand-rolled ``print(..., file=sys.stderr)`` gives a
``--json`` caller prose and no document at all — the wave's recurring
shape, a report that disagrees with the promised contract.

### `cmd_replay`

```python
def cmd_replay(args)
```

Deprecated alias for ``pending drain`` — warns, then defers.

Kept because scripts and a cron entry call it. The name is the bug
(#498): "replay" reads as replaying history, while it posts mine jobs.
It inherits the ``--yes`` guard rather than routing around it, so the
alias is not a back door to the old behaviour.

### `cmd_pending_drain`

```python
def cmd_pending_drain(args)
```

Re-post every queued mine request to the daemon — after saying so.

``~/.mempalace/pending/*.jsonl`` accumulates mine requests whenever the
Stop / PreCompact hooks fire while the daemon or its backend is
unreachable (the 2026-05-21 power-resilience design). Draining it
re-posts each one.

**Defaults to a plan.** The verb this replaced, ``replay``, took no
arguments, printed no description, and posted everything immediately: a
2026-09-17 session reading it as "replay history" queued twelve mines
against production (#498). Nothing is posted without ``--yes``.

The plan comes from :func:`pending_queue.peek`, which reproduces the
drain's own filtering rather than counting lines, so the number printed
is the number that would be posted. A preview that over-counted would be
the same defect this command was filed for.

### `cmd_migrate_wings`

```python
def cmd_migrate_wings(args)
```

Normalize legacy wing names (strip leading/trailing separators).

### `cmd_doctor`

```python
def cmd_doctor(args)
```

One-screen health check of the memory workflow (#425).

Answers, in order: is the MCP bridge on PATH? is the daemon reachable and
how big is the palace? is this project's wing present? are the save hooks
firing (fresh hook_state)? is anything queued for replay? Every line is a
✓ / ✗ / ! so a broken memory workflow is loud instead of silently wrong —
the failure mode that let the MCP bridge stay dead fleet-wide for days.

### `cmd_status`

```python
def cmd_status(args)
```

### `cmd_reconcile_docs`

```python
def cmd_reconcile_docs(args)
```

Queue a mine for every curated doc the palace has not seen (#529).

The curated-docs hook is event-driven and therefore best-effort. Measured
on the live hook before this verb was written, it misses a doc in at
least five distinct ways: a worktree path its Bash matcher excludes; a
worktree path its shell-glob matcher DOES match and which then dies on
the palace host, which has no `.claude/worktrees` at all; a doc arriving
by merge or pull, which produces no tool event; a glob token taken for a
filename; and a path the command merely mentioned. Every one is invisible
to a listener and obvious to a comparison against what is on disk.

So this does not listen. It enumerates `CLAUDE.md` + `docs/**/*.md`,
asks the palace what it recorded, and queues a background single-file
projects-mode mine for anything newer or absent — the same payload
`palace-doc-sync.sh` sends, through the same poster, so there is one
wire format rather than two that drift.

Undecidable files are reported and NOT queued. "No recorded mtime" is an
absence of an answer, not a stale answer, and re-mining a whole wing on
the strength of a missing field is how a reconciler becomes the thing
that needs reconciling.

### `cmd_mined`

```python
def cmd_mined(args)
```

List mined source files grouped by wing.

Companion to ``status`` (which groups by wing × room) — answers "which
files have I mined into this wing?" so an operator can pick targets
for ``mempalace purge --source-file &lt;path>``.

Skips drawers without a ``source_file`` metadata key (typically
diary entries, kg drawers, manually-added entries).

Daemon-routes when ``_daemon_strict()`` is on and ``--palace`` was
not given (#285). palace-daemon's ``mempalace_mined`` tool (PR #96)
returns the same ``&#123;sources_by_wing, wing_filter, total_wings,
total_sources}`` shape the JSON path emits locally.

### `cmd_stats`

```python
def cmd_stats(args)
```

Fast direct-to-daemon palace analytics CLI (#191).

Wraps ``GET /stats`` on the palace daemon, which returns a unified
envelope of three blocks: ``kg`` (entities, triples, relationship
types), ``graph`` (rooms, tunnels, edges), ``status`` (drawer counts,
wings, rooms, protocol/AAAK text). Replaces the older multi-RPC
fan-out — one network hit instead of four. ``--section`` narrows the
table output to a single block; json mode always passes through the
whole envelope so jq pipelines see the daemon contract unchanged.
``--tags`` still triggers an extra ``mempalace_list_tags`` MCP call
because /stats doesn't include the tag breakdown (tag counts can be
100K+ entries and don't belong in the fast-path summary). Daemon
unreachable → exit 1 (matches sibling cmd_list/cmd_graph/cmd_cypher);
inner-error envelope → exit 2.

### `cmd_tags`

```python
def cmd_tags(args)
```

Fast direct-to-daemon tag inventory (slice of #191).

Calls the daemon's ``mempalace_list_tags`` MCP tool — already a
first-class read path on the daemon, just not previously exposed
as a CLI verb. Supports ``--wing`` / ``--room`` scoping and a
``--min-count`` floor; output formats match the sibling commands
(``--format=table`` default, ``--json``/``--format=json`` pass-through).

Daemon unreachable → exit 1; inner-error envelope → exit 2.

### `cmd_hallways`

```python
def cmd_hallways(args)
```

Deprecated alias for ``mempalace hallway list`` (#407).

The plural verb predates the daemon and carried its own fetch path:
always local whatever ``PALACE_DAEMON_URL`` said, ``args.wing``
passed raw to ``list_hallways`` instead of through the sanitizing
MCP tool, and a co-occurrence sort with no tiebreak, so equal-count
rows changed places between runs. #438 closed the ``--json`` hole in
isolation; delegating the whole body closes the remaining rows of
#407's table with it and leaves one hallway-listing path instead of
two divergent ones.

Kept working for scripts that call it — removal is a later release.
The notice goes to stderr so it cannot corrupt a ``--json`` pipe,
and is suppressed entirely on the ``--json`` surface, which carries
a ``deprecated`` marker inside the payload instead (#438's choice,
kept: nothing then has to parse stderr to notice).

### `cmd_overlap`

```python
def cmd_overlap(args)
```

Cross-wing entity overlap via a single read-only Cypher hop (slice of #191).

Answers "what entities appear in both wing A and wing B?". Uses the
daemon's ``POST /cypher`` (read-only by SQLSTATE 25006); the two
wing names are sanitized + inlined as Cypher literals because the
endpoint accepts only ``&#123;cypher, graph}`` — no parameters.

Daemon unreachable / 401/404/403 → exit 1; 403 read-only write
attempt cannot fire here (only MATCH/RETURN); inner-error envelope
or sanitization failure → exit 2.

### `cmd_why`

```python
def cmd_why(args)
```

Explain a drawer — surface the signals that make it findable (slice of #191).

Composes three read-only daemon calls into one report:
  * ``mempalace_get_drawer`` for wing/room/tags + content
  * read-only Cypher for the drawer's :MENTIONS-Entity edges (top N)
  * ``mempalace_search`` on the drawer's own first paragraph for the
    nearest semantic neighbors (drawer itself filtered out)

Daemon unreachable → exit 1; missing drawer or inner-error envelope
→ exit 2. No ``searcher.py`` writes; pure orchestration over existing
read paths.

### `cmd_tunnels`

```python
def cmd_tunnels(args)
```

List cross-wing tunnels (slice of #191).

Wraps the daemon's ``mempalace_list_tunnels`` MCP tool. Default is
explicit-only (the agent-wired tunnels at ``~/.mempalace/tunnels.json``);
pass ``--passive`` to also include passive tunnels (rooms appearing
in 2+ wings, inferred from the palace graph — see issue #75).

Daemon unreachable → exit 1; inner-error envelope → exit 2.

### `cmd_drawer`

```python
def cmd_drawer(args)
```

Single-drawer CRUD by ID — get / add / delete / update (#355).

### `cmd_duplicate`

```python
def cmd_duplicate(args)
```

Check whether content already exists in the palace (#363).

Wraps ``mempalace_check_duplicate``. A completed check exits 0
whatever the verdict — ``is_duplicate`` in the payload is the answer,
and overloading the exit code by default would collide with the
daemon-unreachable 1 that every sibling command uses.

``--fail-on-duplicate`` opts into a scriptable guard: non-zero then
means "do not file this", which is also what daemon-unreachable and a
disabled vector index mean, so ``duplicate check --file x
--fail-on-duplicate && file-it`` fails safe in every branch. A
disabled vector index cannot answer at all; the tool says so
explicitly rather than claiming "not a duplicate", and we always exit
2 on it so a guard never reads silence as novelty.
