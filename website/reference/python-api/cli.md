# `mempalace.cli`

Source: [`mempalace/cli.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli.py)

MemPalace — Give your AI a memory. No API key required.

Three ways to ingest:
  Projects:      mempalace mine ~/projects/my_app                  (code, docs, notes)
  Conversations: mempalace mine &lt;convo-dir> --mode convos          (Claude Code, Claude.ai, ChatGPT, Slack exports)
  Documents:     mempalace mine &lt;docs-dir> --mode extract          (PDF, DOCX, PPTX, XLSX, RTF, EPUB — requires mempalace[extract])
  Adapters:      mempalace mine &lt;source> --source &lt;adapter-name>  (registered source adapters)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init &lt;dir>                  Detect rooms from folder structure
    mempalace split &lt;dir>                 Split concatenated mega-files into per-session files
    mempalace mine &lt;dir>                  Mine project files (default)
    mempalace mine &lt;dir> --mode convos    Mine conversation exports
    mempalace mine &lt;dir> --mode extract   Mine binary office documents (PDF/DOCX/etc.)
    mempalace mine &lt;source> --source NAME Mine through a registered source adapter
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace task create ...             Create a complete agent handoff
    mempalace task launch ...             Run a stored task headlessly
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed
    mempalace mined                       List mined source files grouped by wing
    mempalace purge --source-file &lt;path>  Remove drawers mined from a specific file

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs

## Classes

### `class DaemonError(RuntimeError)`

Raised when a daemon HTTP call fails or returns a JSON-RPC error.

### `class DaemonRequestError(DaemonError)`

The daemon answered and REFUSED the request — a 4xx, not an outage.

A subclass so every existing `except DaemonError` keeps working; call
sites that can tell the operator something useful catch this first.

The distinction is the whole of #499: one exception type meant a client
could not tell "the palace is down" from "your argument is wrong", and it
guessed the former. `mempalace list --room diary` reported "palace daemon
unreachable … see mempalace status" while the daemon was up and had
answered with, verbatim:

    &#123;"detail": &#123;"error": "room 'diary' is not in the canonical set",
                "valid_rooms": [...]}}

The message the operator needed had already arrived. `detail` carries it
forward so the client stops substituting a guess for it.

#### `__init__`

```python
def __init__(self, message: str, *, status: int, detail = None)
```

### `class UnknownSourceAdapterError(ValueError)`

Raised when an explicit ``--source`` name is absent from the registry.

### `class UnsupportedSourceAdapterProtocolError(ValueError)`

Raised when an adapter requires runner semantics not implemented yet.

## Functions

### `cmd_init`

```python
def cmd_init(args)
```

### `cmd_mine`

```python
def cmd_mine(args)
```

### `mine_source_adapter`

```python
def mine_source_adapter(*, source_name: str, source_path: str, palace_path: str, dry_run: bool = False) -> int
```

Run an explicitly selected RFC 002 source adapter through ``PalaceContext``.

This deliberately sits alongside, rather than inside, the legacy mode
miners.  Until those miners are migrated to first-party adapters, no-flag
and ``--mode`` calls must retain their established dispatch paths.

### `cmd_sweep`

```python
def cmd_sweep(args)
```

Sweep a transcript file or directory.

The sweeper deduplicates against its own prior writes via
deterministic drawer IDs + a timestamp cursor. It does NOT currently
coordinate with the file-level miners (miner.py / convo_miner.py) —
those produce char-chunked drawers without compatible message
metadata, so running both miners may store overlapping content under
different IDs.

### `cmd_sync`

```python
def cmd_sync(args)
```

Prune drawers whose source files are gitignored, deleted, or moved (#1252).

Where it runs (#418): ``--daemon`` submits to the local job queue;
daemon-strict with no ``--palace`` routes to the palace daemon; otherwise
the backend is resolved *before* the palace-directory precheck, and that
precheck is skipped for a service-backed store, which has no local
database file by design.

### `cmd_daemon`

```python
def cmd_daemon(args)
```

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

### `cmd_update`

```python
def cmd_update(args)
```

Configure, check, or prepare updates without installing automatically.

### `cmd_diary`

```python
def cmd_diary(args)
```

``mempalace diary write|read`` — the agent diary at the CLI (#354).

``write`` wraps ``mempalace_diary_write``; ``read`` wraps
``mempalace_diary_read``; ``agents`` wraps ``mempalace_diary_agents``.
``read``'s ``--topic`` / ``--since`` filters are applied client-side —
the tool has no such parameters.

Only ``write`` needs an agent name (#501). A read does not: the hook
writes ``agent_name=&lt;harness>`` (``claude-code`` / ``codex`` /
``gemini-cli``) and never consults ``identity.txt`` or
``MEMPALACE_AGENT_NAME``, so requiring one on read meant entries
plainly visible in ``list --wing W`` answered "No diary entries" to
every name a reader could guess. ``agents`` exists to discover the
names that do work.

### `cmd_kg`

```python
def cmd_kg(args)
```

``mempalace kg add|invalidate|timeline`` — KG writes + temporal read (#357).

Deviations from issue #357's sketch, driven by the tool schemas:
``kg invalidate`` addresses a fact by ``--subject/--predicate/--object``
(``mempalace_kg_invalidate`` has no triple ids and no reason field), and
``kg timeline``'s ``--limit`` truncates client-side because
``mempalace_kg_timeline`` takes only ``(entity, as_of)``.

A ``--limit`` above ``_KG_TIMELINE_TOOL_CAP`` cannot be honoured: the
graph backend's own ``timeline()`` stops at that many rows and the tool
doesn't expose the parameter, so the CLI reports how many it actually
received rather than implying the requested window was searched. Seen
live: ``kg timeline JP --limit 200`` returns ``count: 100``.

### `cmd_walk`

```python
def cmd_walk(args)
```

``mempalace walk`` — traverse the palace graph (#359).

Two tools behind one verb: ``--follow palace`` (default) calls
``mempalace_walk_palace`` from a ``--wing`` / ``--room`` / ``--entity``
anchor, and ``--follow tunnels`` calls ``mempalace_traverse`` from a
``--room`` anchor with ``--depth`` as its hop budget. Issue #359's
``--from &lt;drawer_id>`` is not offered: neither tool accepts a drawer
anchor (``mempalace why &lt;drawer_id>`` is the per-drawer view).

### `cmd_rate`

```python
def cmd_rate(args)
```

``mempalace rate &lt;drawer_id> --useful|--not-useful`` (#361).

``mempalace_rate_memory`` records a boolean, not a 1–5 score, and has
no field for a free-text reason — so issue #361's ``--score N
--reason "..."`` sketch is expressed as the two boolean flags. The
rating lands in drawer metadata and becomes a bounded ranking signal;
it never touches verbatim content.

### `cmd_logstream`

```python
def cmd_logstream(args)
```

### `cmd_task`

```python
def cmd_task(args)
```

Create and run complete logstream tasks through a small public interface.

### `cmd_artifact`

```python
def cmd_artifact(args)
```

### `cmd_palace_set_embedder`

```python
def cmd_palace_set_embedder(args)
```

Record (or force-override) a palace's embedder identity (RFC 001).

Resolves the ``unknown`` state for a legacy palace, or records a specific
model with ``--model``. It records identity on the palace only; it does not
change the configured model — when the two differ it prints how to align
``MEMPALACE_EMBEDDING_MODEL``. ``--force`` overwrites an existing,
differently-named identity.

### `cmd_repair_status`

```python
def cmd_repair_status(args)
```

Read-only HNSW capacity health check (#1222).

### `cmd_repair`

```python
def cmd_repair(args)
```

Repair palace state.

Default mode is full HNSW rebuild via extract + re-upsert
(``--mode rebuild`` / ``--mode legacy``, synonyms). Also handles
``--mode max-seq-id`` for un-poisoning ``max_seq_id`` rows
corrupted by the legacy 0.6.x → 1.5.x chromadb migration shim
(#1208 / #1288 family). The earlier ``reorganize`` mode was
retired alongside the recovery collection (PR #8 / row 32).

Closes Copilot finding on jphein/mempalace#8: docstring claimed
only "rebuild" while the function continued to dispatch
``max-seq-id`` based on ``args.mode``.

On a successful rebuild the palace SQLite file is VACUUMed and the
FTS5 index is rebuilt, so the next repair's integrity preflight reads
a consistent database (#1747).

### `cmd_hook`

```python
def cmd_hook(args)
```

Run hook logic: reads JSON from stdin, outputs JSON to stdout.

### `cmd_instructions`

```python
def cmd_instructions(args)
```

Output skill instructions to stdout.

### `cmd_rules`

```python
def cmd_rules(args)
```

Output the shared-brain agent rules block for a given agent identity.

### `cmd_mcp`

```python
def cmd_mcp(args)
```

Show how to wire MemPalace into MCP-capable hosts.

### `cmd_serve`

```python
def cmd_serve(args)
```

Run a secure remote HTTP MCP server for a team to share one palace (#1877).

A turnkey wrapper over ``mempalace-mcp --transport http``: it resolves a
bearer token (auto-generating a strong one for non-loopback binds), prints a
ready-to-paste client config, then execs the real server in the foreground so
Docker/systemd own the process lifecycle. The token is passed via the
environment, never argv, so it can't leak through ``ps``.

### `cmd_compress`

```python
def cmd_compress(args)
```

Compress drawers in a wing using AAAK Dialect.

### `cmd_wings`

```python
def cmd_wings(args)
```

List every wing with its drawer count (slice of #191, issue #356).

Wraps ``mempalace_list_wings``. On the daemon path the counts come
from ``GET /status/fast`` first — that endpoint already aggregates
wing counts from the metadata index and answers in well under a
second, whereas ``mempalace_list_wings`` over ``/mcp`` walks the
facet path and does not return in any usable time on a palace of a
few hundred thousand drawers (measured against the production
daemon, 2026-08-20). ``mempalace_list_wings`` stays as the fallback
for daemons that predate ``/status/fast``. Both shapes are reduced
to the tool's ``&#123;"wings": &#123;...}}`` envelope so ``--json`` consumers
see one contract regardless of which path served the request.

The issue also asked for a "last updated" column; ``tool_list_wings``
returns counts only and no timestamp is available anywhere on the
read path, so the table carries a share-of-palace percentage instead.

### `cmd_taxonomy`

```python
def cmd_taxonomy(args)
```

Print the wing → room → drawer-count tree (slice of #191, issue #362).

Wraps ``mempalace_get_taxonomy``. ``--wing`` narrows the output to
one wing; the underlying tool takes no arguments, so the filter is
applied client-side after the tree comes back (documented here
because it does not reduce the work the daemon does).

### `cmd_aaak`

```python
def cmd_aaak(args)
```

Print the AAAK dialect specification (slice of #191, issue #362).

Wraps ``mempalace_get_aaak_spec``. Routed rather than read straight
out of the local package on purpose: when a daemon is configured,
the spec that matters is the one the daemon is actually filing
against, which can differ from the locally installed version.

### `cmd_hallway_list`

```python
def cmd_hallway_list(args, _deprecated_alias: bool = False)
```

List within-wing entity hallways (slice of #191, issue #358).

Wraps ``mempalace_list_hallways``. The pre-existing ``mempalace
hallways`` verb is a deprecated alias that delegates here (#407), so
both spellings share one fetch path, one sort and one limit rule;
``_deprecated_alias`` only adds the ``deprecated`` marker to the
JSON payload when the caller arrived by the old name.

### `cmd_hallway_delete`

```python
def cmd_hallway_delete(args)
```

Delete one hallway record by id (slice of #191, issue #358).

Destructive, so it refuses to act without ``--confirm``. On an
interactive terminal the flag can be supplied by answering the
prompt; with no TTY there is nobody to ask, so the command exits 2
rather than deleting on an implied yes.

### `cmd_hallway`

```python
def cmd_hallway(args)
```

Dispatch the two-level ``hallway`` verb.

### `cmd_checkpoint`

```python
def cmd_checkpoint(args)
```

Batch-file a session in one call (slice of #191, issue #360).

Wraps ``mempalace_checkpoint``: semantic-dedups each item, files the
non-duplicates as drawers, then writes one optional diary entry.

This writes to the palace, so ``--dry-run`` prints the exact payload
that would be sent and exits without calling the tool — the cheap
way to check a generated ``--items-file`` before it lands.

### `main`

```python
def main()
```

CLI entry point for the ``mempalace`` console script.

Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so
any subprocess this CLI spawns inherits a clean env. Host applications
that call ``main()`` programmatically should be aware that the parent
process loses ``PYTHONPATH`` as well. Library imports
(``import mempalace.searcher`` from a host app) do NOT trigger this
side effect; only the CLI/MCP entry points pop the env var.
