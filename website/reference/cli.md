# CLI Commands

All commands accept `--palace <path>` to override the default palace location.
The top-level command also accepts `--backend <name>` to select a storage
backend such as `sqlite_exact`, `milvus`, `qdrant`, or `pgvector`.

## `mempalace update`

Release checks are disabled by default. Enable weekly checks explicitly with
`mempalace update configure --enable --installer uv-tool` (using `pipx` or
`pip` when that is how the runtime was installed), or disable them with `--disable`.
`mempalace update check` performs an explicit check regardless of that setting.
`mempalace update plan` prints the runtime, skill, restart, and tool-refresh
actions but never executes them. Command actions use structured `argv`; for a
`pip` installation the first argument is the exact interpreter running
MemPalace, not whichever `python` happens to be on `PATH`. For an explicit check made without saved setup
state, pass the matching installer to `plan --installer`. Cached state appears in `mempalace_status` so
agents can ask the user to authorize an upgrade without blocking on a network
request.

## `mempalace init`

Scan a project directory for people, projects, and rooms, and set up the palace.

```bash
mempalace init <dir>                 # <dir> is required
mempalace init <dir> --yes           # non-interactive mode
mempalace init ~/projects/myapp      # example
mempalace init .                     # initialize from the current directory
```

| Option  | Description                                                                  |
|---------|------------------------------------------------------------------------------|
| `<dir>` | **Required.** Project directory to scan. Pass `.` for the current directory. |
| `--yes` | Auto-accept all detected entities                                            |

What it does:

1. Scans `<dir>` for people and projects in file content
2. Detects rooms from `<dir>`'s folder structure
3. Saves detected entities to `<dir>/entities.json`
4. Ensures the global `~/.mempalace/` config directory exists

Running `mempalace init` with no argument will exit with
`error: the following arguments are required: dir`.

## `mempalace mine`

Mine files into the palace.

```bash
mempalace mine <dir>
mempalace mine <dir> --mode convos
mempalace mine <dir> --mode convos --extract general
mempalace mine <dir> --wing myapp
```

| Option | Default | Description |
|--------|---------|-------------|
| `<dir>` | — | Directory to mine |
| `--mode` | `projects` | `projects` for code/docs, `convos` for chat exports |
| `--wing` | directory name | Wing name override |
| `--agent` | `mempalace` | Agent name tag |
| `--limit` | `0` (all) | Max files to process |
| `--dry-run` | — | Preview without filing |
| `--extract` | `exchange` | `exchange` or `general` (for convos mode) |
| `--no-gitignore` | — | Don't respect .gitignore |
| `--include-ignored` | — | Always scan these paths even if ignored |

Exits `1` when one or more files could not be mined (a stale-drawer purge failed). The summary shows `Files failed: N`, and stderr reports the failed files. Those files keep their existing drawers and are retried by the next mine.

## `mempalace search`

Find anything by semantic search.

```bash
mempalace search "query"
mempalace search "query" --wing myapp
mempalace search "query" --wing myapp --room auth
mempalace search "query" --results 10
```

| Option | Default | Description |
|--------|---------|-------------|
| `"query"` | — | What to search for |
| `--wing` | all | Filter by wing |
| `--room` | all | Filter by room |
| `--results` | `5` | Number of results |

## `mempalace list`

Browse drawers by wing/room metadata — no ranking, no embedding. A fast,
direct-to-daemon read: it hits palace-daemon directly and skips the MCP/AI
round-trip, so it returns immediately.

```bash
mempalace list
mempalace list --wing myapp
mempalace list --wing myapp --room auth
mempalace list --limit 50 --offset 50
mempalace list --format json
```

| Option | Default | Description |
|--------|---------|-------------|
| `--wing` | all | Limit to one wing |
| `--room` | all | Limit to one room |
| `--limit` | `20` | Max drawers to return (max: 1000) |
| `--offset` | `0` | Pagination offset |
| `--format` | `table` | `table`, `compact` (one line per drawer), `full` (no truncation), or `json` |
| `--json` | — | Shorthand for `--format json` |

## `mempalace graph`

Structural snapshot of the knowledge graph and palace — wings, rooms,
tunnels, and KG entity/triple counts. Direct-to-daemon.

```bash
mempalace graph
mempalace graph --limit 1000
mempalace graph --format full
mempalace graph --json
```

| Option | Default | Description |
|--------|---------|-------------|
| `--limit` | `500` | Cap on KG entity count (max: 50000) |
| `--format` | `table` | `table` (summary + top wings), `full` (every wing + sampled triples), or `json` |
| `--json` | — | Shorthand for `--format json` |

## `mempalace cypher`

Run a read-only Cypher query against the Apache AGE knowledge graph. Write
verbs are rejected server-side. Direct-to-daemon.

```bash
mempalace cypher "MATCH (n) RETURN count(n)"
mempalace cypher "MATCH (n:Entity) RETURN n.name LIMIT 10"
mempalace cypher "MATCH (n) RETURN n.name" --format csv
mempalace cypher "MATCH (n) RETURN n" --json
```

| Option | Default | Description |
|--------|---------|-------------|
| `"query"` | — | Cypher query (`MATCH` / `RETURN`; write verbs are rejected) |
| `--graph` | palace graph | AGE graph name |
| `--limit` | — | Advisory cap (use `LIMIT` in the query for a hard cutoff) |
| `--format` | `table` | `table` (aligned columns), `json`, or `csv` |
| `--json` | — | Shorthand for `--format json` |

## `mempalace move`

Relocate a single drawer to a different wing and/or room — metadata only, the
verbatim content is never touched. Direct-to-daemon. Omitting both `--wing` and
`--room` is a no-op (nothing to change).

```bash
mempalace move <drawer_id> --wing myapp
mempalace move <drawer_id> --wing myapp --room auth
mempalace move <drawer_id> --room archive --format json
```

| Option | Default | Description |
|--------|---------|-------------|
| `drawer_id` | — | ID of the drawer to move (positional, required) |
| `--wing` | unchanged | New wing (omit to leave unchanged) |
| `--room` | unchanged | New room (omit to leave unchanged) |
| `--format` | `table` | `table` (old→new confirmation) or `json` (daemon pass-through) |

## `mempalace bulk-move`

Relocate every drawer matching a source wing/room filter to a target wing/room
— metadata only, content untouched. **Dry-run by default**: without `--apply`
it previews the matched drawers and the destination, changing nothing. Each
drawer is moved by an independent PATCH, so a single failure doesn't abort the
batch. At least one source filter (`--wing`/`--room`) and one target
(`--to-wing`/`--to-room`) are required.

```bash
# preview only (dry run)
mempalace bulk-move --wing scratch --to-wing archive
# execute, with interactive confirmation
mempalace bulk-move --wing scratch --room old --to-wing archive --apply
# execute non-interactively (CI / scripts)
mempalace bulk-move --wing scratch --to-wing archive --apply --yes
```

| Option | Default | Description |
|--------|---------|-------------|
| `--wing` | — | Source: select drawers in this wing (at least one of `--wing`/`--room`) |
| `--room` | — | Source: select drawers in this room (at least one of `--wing`/`--room`) |
| `--to-wing` | — | Target wing (at least one of `--to-wing`/`--to-room`) |
| `--to-room` | — | Target room (at least one of `--to-wing`/`--to-room`) |
| `--apply` | — | Actually move the drawers (without it, preview only) |
| `--yes`, `-y` | — | Skip the confirmation prompt (required to `--apply` non-interactively) |
| `--format` | `table` | `table` (preview/summary) or `json` (machine-readable) |

## `mempalace split`

Split concatenated transcript mega-files into per-session files.

```bash
mempalace split <dir>
mempalace split <dir> --dry-run
mempalace split <dir> --min-sessions 3
mempalace split <dir> --output-dir ~/split-output/
```

| Option | Default | Description |
|--------|---------|-------------|
| `<dir>` | — | Directory with transcript files |
| `--output-dir` | same dir | Write split files here |
| `--dry-run` | — | Preview without writing |
| `--min-sessions` | `2` | Only split files with N+ sessions |

## `mempalace wake-up`

Show L0 + L1 wake-up context (~600–900 tokens).

```bash
mempalace wake-up
mempalace wake-up --wing driftwood
```

| Option | Description |
|--------|-------------|
| `--wing` | Project-specific wake-up |

## `mempalace compress`

Compress drawers using AAAK Dialect.

```bash
mempalace compress --wing myapp
mempalace compress --wing myapp --dry-run
mempalace compress --config entities.json
```

| Option | Description |
|--------|-------------|
| `--wing` | Wing to compress (default: all) |
| `--dry-run` | Preview without storing |
| `--config` | Entity config JSON file |

## `mempalace status`

Show what's been filed — drawer count, wing/room breakdown.

```bash
mempalace status
```

## `mempalace stats`

Palace analytics dashboard — wings, rooms, knowledge graph, tunnels, and
optionally tags. Direct-to-daemon (one `GET /stats` REST call), so it's
faster than the equivalent MCP tool fan-out.

```bash
mempalace stats
mempalace stats --section kg
mempalace stats --section graph
mempalace stats --tags
mempalace stats --json
```

| Option | Default | Description |
|--------|---------|-------------|
| `--top` | `10` | Max rows per section (`0` shows all) |
| `--tags` | — | Include the tag-count breakdown (extra daemon call) |
| `--section` | `all` | `kg`, `graph`, `status`, or `all` |
| `--no-relationship-types` | — | Suppress the (potentially long) relationship-types list |
| `--format` | `table` | `table` or `json` |
| `--json` | — | Shorthand for `--format json` |

## `mempalace repair`

Rebuild palace vector index from stored data. Fixes segfaults after database corruption.

```bash
mempalace repair
```

Creates a backup at `<palace_path>.backup` before rebuilding, replacing any backup already there.

| Flag | Description |
|------|-------------|
| `rebuild-index` | Positional alias for `--mode from-sqlite --archive-existing` |
| `--mode` | `legacy` (default), `max-seq-id`, or `from-sqlite` |
| `--dry-run` | Print what the repair would do and exit without modifying the palace |
| `--yes` | Skip confirmation for destructive changes |
| `--backup` | Back up SQLite before mutation (default: on) |
| `--source` | Source palace for `--mode from-sqlite` (defaults to `--palace`) |
| `--archive-existing` | Rename the existing palace to `<palace>.pre-rebuild-<timestamp>` first |
| `--segment` | Segment UUID filter for `--mode max-seq-id` |
| `--from-sidecar` | Pre-corruption `chroma.sqlite3` to copy clean `max_seq_id` values from |
| `--confirm-truncation-ok` | Override the truncation safety guard. Disables the abort that protects you when the collection layer returns fewer drawers than SQLite holds |

## `mempalace prune`

Delete drawers older than `--stale-days N`. Dry-run by default — reports a
count without deleting unless you pass `--confirm`.

```bash
mempalace prune --stale-days 90              # dry-run: report only
mempalace prune --stale-days 90 --confirm    # actually delete
mempalace prune --stale-days 90 --wing scratch --confirm
```

| Option | Default | Description |
|--------|---------|-------------|
| `--stale-days` | **required** | Prune drawers whose `filed_at` is older than this many days |
| `--wing` | all | Limit prune to this wing |
| `--room` | all | Limit prune to this room |
| `--confirm` | — | Actually delete (without it, prune only reports a dry-run count) |

## `mempalace mcp`

Helper command that outputs setup syntax (like `claude mcp add...`) to connect MemPalace to your AI client, automatically handling paths.

```bash
mempalace mcp
mempalace mcp --palace ~/.custom-palace
```

## `mempalace hook`

Run hook logic for Claude Code / Codex integration.

```bash
mempalace hook run --hook stop --harness claude-code
mempalace hook run --hook precompact --harness claude-code
mempalace hook run --hook session-start --harness codex
```

| Option | Values | Description |
|--------|--------|-------------|
| `--hook` | `session-start`, `stop`, `precompact` | Hook name |
| `--harness` | `claude-code`, `codex` | Harness type |

## `mempalace instructions`

Output skill instructions to stdout.

```bash
mempalace instructions init
mempalace instructions search
mempalace instructions mine
mempalace instructions help
mempalace instructions status
```

## `mempalace rules`

Render the canonical shared-brain system-prompt block, marker-wrapped so
a later re-render can replace it in place. Identity is
`host:harness:project` (stable lowercase tokens). `--project` is the
example workspace name; the block tells the agent to compose the project
component from the current workspace.

```bash
mempalace rules --host mac --harness claude --project myapp
mempalace rules --host windows --harness grok --project mempalace --mcp light
```

`--mcp full` (default) names the 52-tool `mempalace-mcp` tools.
`--mcp light` names the 3-tool triad. Prose is identical.

## `mempalace logstream`

Agent coordination events — delegate work, wait for replies, acknowledge
outcomes (RFC 003). Operates on `logstream.sqlite3` in the palace directory;
safe to run alongside a live hub. See [Agent Logstream](/concepts/agent-logstream).

```bash
mempalace logstream append --type task.request --stream project/myapp \
  --room delegation --from-agent mac --to-agent windows \
  --correlation-id task_123 --body "Please fix the flaky test."

mempalace logstream list --stream project/myapp --room delegation --json
mempalace logstream wait --correlation-id task_123 --type patch.ready \
  --timeout-ms 300000 --json
mempalace logstream ack evt_... --from-agent mac --status applied

# Background watcher: blocks, wakes on what needs you, exits 0 on a match
mempalace logstream watch --agent mac:claude:myapp --type task.request --type task.reply --type patch.ready \
  --json
```

| Subcommand | Description |
|------------|-------------|
| `append` | Append an immutable event (`--type`, `--stream`, `--room`, `--from-agent` required; `--body`/`--body-file`, `--artifact-id` repeatable) |
| `list` | List events, oldest first (all routing fields as filters, `--since-event-id`, `--limit`) |
| `wait` | Long-poll until a match or timeout (`--timeout-ms`, max 300000; exits `2` on timeout) |
| `watch` | Background watcher: re-arms past the `wait` cap, carries the cursor, and exits `0` on a match / `2` on `--idle-exit-ms`. `--agent ID` is shorthand for `--to-agent ID --exclude-from-agent ID` so your own `*` broadcasts never wake you. Filters repeat to mean "or"; `--state-file` resumes exactly (defaults from `--agent`, with `_` doubled and `:` sanitized to `_`); `--follow` stays alive past the first match; a cursorless first run starts at the tip (`--from-start` to replay); exits `130` if interrupted; `--follow --json` emits NDJSON — one batch envelope per line (`{"events": [...], "count": N, "cursor": ...}`), not one event per line |
| `ack` | Append an `event.ack` for an event (`--from-agent` required, `--status`, `--body`) |
| `sync` | Pull missing events/artifacts from peer replicas (`--peer URL --token T`, or all peers in `peers.json`) |

All subcommands accept `--json` for scriptable output.

## `mempalace task`

High-level task creation and controlled execution over the logstream. This
interface creates the complete canonical `task.request` envelope and prints a
short handoff that can be pasted into a destination agent; callers do not need
to construct event fields or correlation ids themselves. Like the other
logstream CLI commands, it operates on the local palace. A client connected to
a remote shared-brain hub should call the equivalent
`mempalace_task_create` MCP tool so the task is appended on the hub.

```bash
mempalace task create \
  --project myapp \
  --from-agent mac-claude \
  --to-agent windows-codex \
  --goal "Fix the flaky integration test." \
  --branch fix/flaky-integration \
  --base-commit 2668053 \
  --done "The focused test passes and a patch is submitted."
```

The command appends an immutable `task.request`, generates a
`task_<goal>_<entropy>` correlation id, and prints a `Ready to paste` line.
Use `--goal-file` or `--done-file` when the exact text is multiline. `--json`
returns `{"task": <event>, "handoff": <line>}`.
`--base-commit` must be an immutable hexadecimal object id (abbreviated or
full), not a branch or tag whose target could move after the event is stored.

An explicitly controlled workflow can start a supported headless runner from
the stored task:

```bash
mempalace task launch task_fix_the_flaky_integration_test_7f3a9c10 \
  --runner codex --workspace /path/to/trusted/checkout
```

For a remote-only MCP client, fetch the full single `task.request` event through
`mempalace_event_list`, save the exact event object as JSON on the destination
machine, and use `--task-file` instead of a task id. This avoids accidentally
resolving the task from an unrelated local palace:

```bash
mempalace task launch --task-file task-request.json \
  --runner codex --workspace /path/to/trusted/checkout
```

Supported runners are `codex` and `claude`. The launcher verifies the task's
addressed identity, refuses to override it, rejects a runner that conflicts
with a conventional `*-codex` or `*-claude` identity, releases its logstream
connection, and starts the runner without a shell. Broadcast tasks require a
concrete `--agent`. It does not add permission-bypass flags or weaken the
runner's sandbox and approval policy.

| Subcommand | Description |
|------------|-------------|
| `create` | Append a canonical task request and print a pasteable handoff (`--project`, `--from-agent`, `--to-agent`, `--goal`/`--goal-file`, `--branch`, `--base-commit`, and `--done`/`--done-file`) |
| `launch` | Resolve and execute an existing task with `--runner codex\|claude` in a trusted `--workspace`; `--agent` accepts broadcasts but cannot impersonate an addressed worker |

## `mempalace artifact`

Exact artifact exchange for agent handoffs — unified diffs, files, logs.
Content is stored verbatim with a SHA-256.

```bash
git diff | mempalace artifact put --kind patch --created-by windows --json
mempalace artifact get art_... | git apply --3way
mempalace artifact get art_... --out /tmp/handoff.patch
```

| Subcommand | Description |
|------------|-------------|
| `put` | Store content (`--kind patch\|file\|log\|json\|note`, `--created-by` required; `--content`, `--file`, or stdin) |
| `get` | Print exact content to stdout, or `--out FILE`; `--json` for metadata |
