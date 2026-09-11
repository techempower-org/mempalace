# OpenCode + MemPalace integration (fork-routed via palace-daemon)

Two-direction integration between [OpenCode](https://opencode.ai) and a MemPalace running behind palace-daemon:

| Direction | Mechanism | What you get |
|---|---|---|
| **Read** (agent → palace) | MCP server entry in `~/.config/opencode/opencode.jsonc` pointing at the daemon-aware stdio wrapper | OpenCode agents can call `mempalace_search`, `mempalace_kg_query`, `mempalace_diary_read`, etc. |
| **Push** (live capture, conversation → palace) | This fork's `examples/opencode/live-capture/` plugin — small JS shim + Python helper that POSTs to the daemon's `/silent-save`. For local palaces, [`opencode-plugin-mempalace`](https://www.npmjs.com/package/opencode-plugin-mempalace) (option-K) works after two patches in `examples/opencode/`. | On every `session.idle`, the current session's transcript is extracted from OpenCode's SQLite DB and POSTed to the palace as a diary entry |
| **Pull** (retrospective backfill, OpenCode SQLite → palace) | This fork's `OpenCodeSourceAdapter` (cherry-picked from upstream PR #1484) | One-shot ingest of historical OpenCode sessions from `~/.local/share/opencode/opencode.db` |

Together these match the same shape as MemPalace's Claude Code stop-hook pattern: live capture during use, retrospective fill-in when needed, and read-side MCP for agent recall.

## Why fork-ahead

This fork carries the integration surface ahead of upstream because the canonical merge points are still open:

| Upstream PR | What | Status (last checked 2026-05-21) |
|---|---|---|
| [#1484](https://github.com/MemPalace/mempalace/pull/1484) | `OpenCodeSourceAdapter` (RFC 002) | OPEN — CI green except a transient test-windows runner failure |
| [#1567](https://github.com/MemPalace/mempalace/pull/1567) | `.opencode/opencode.json` MCP config in repo root | OPEN |
| [#23](https://github.com/MemPalace/mempalace/pull/23) | Earlier OpenCode SQLite spadework (JakobSachs) | OPEN, CONFLICTING — superseded by #1484; my comment on #23 offered three coordination paths |
| [#297](https://github.com/MemPalace/mempalace/pull/297) | Milofax's auto-plugin | OPEN — codebase-mining + protocol injection design; not what this fork uses |
| [#1524](https://github.com/MemPalace/mempalace/pull/1524) | geco's npm-plugin integration guide | OPEN — uses opinionated 5-wing taxonomy that doesn't fit project-keyed palaces |

The cherry-picks land #1484 + #1567 onto this fork's `main` so the fork ships with OpenCode support immediately. When upstream merges happen, the cherry-picked commits become no-ops and the fork-changes entries can be retired.

## Setup recipe

### 1. Install MemPalace CLI

```bash
pipx install "mempalace>=3.3.5"
```

`mempalace` and `mempalace-mcp` end up on PATH. The CLI auto-routes through palace-daemon when `PALACE_DAEMON_URL` is in the env.

### 2. Daemon env file

Put the daemon URL + API key in `~/.config/palace-daemon/env` (mode 600):

```
PALACE_API_KEY=<your-daemon-api-key>
PALACE_DAEMON_URL=http://your-daemon-host:8085
```

### 3. MCP wrapper

The mempalace-mcp stdio bridge needs `PALACE_API_KEY` in its environment, but MCP clients (OpenCode, Claude Code) spawn server subprocesses without inheriting shell rc. The wrapper at `palace-daemon/clients/mempalace-mcp-wrapper.sh` (see [palace-daemon PR #26](https://github.com/techempower-org/palace-daemon/pull/26)) sources the env file before exec'ing the bridge:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mempalace": {
      "type": "local",
      "command": ["/home/<user>/Projects/palace-daemon/clients/mempalace-mcp-wrapper.sh"],
      "enabled": true
    }
  }
}
```

This goes in `~/.config/opencode/opencode.jsonc`.

### 4. Live-capture plugin

Two options, depending on whether your palace-daemon is local or remote:

#### Option A (recommended for daemon-routed setups): this fork's `examples/opencode/live-capture/`

Drop the bundled plugin + helper into OpenCode's global plugin directory:

```bash
mkdir -p ~/.config/opencode/plugins
cp $REPO/examples/opencode/live-capture/mempalace-live-capture.js ~/.config/opencode/plugins/
```

The plugin subscribes to `session.idle` / `session.deleted` / `session.status[idle]`
and, on each idle, spawns the companion `capture-session.py` helper which:

1. Reads OpenCode's local SQLite session DB
   (`~/.local/share/opencode/opencode.db`).
2. Extracts the role-pair transcript using this fork's
   `OpenCodeSourceAdapter` (RFC 002 contract).
3. POSTs the transcript to the daemon's `/silent-save` endpoint.

Why this fork ships its own plugin (instead of just using
`opencode-plugin-mempalace`): the option-K plugin is broken for
daemon-routed setups in two compounding ways. See
[Compatibility notes — option-K plugin](#compatibility-notes--option-k-plugin) below.

The bundled plugin needs the helper script on disk to import the adapter
helpers. By default it looks at
`~/Projects/memorypalace/examples/opencode/live-capture/capture-session.py`;
override with the env var `MEMPALACE_LIVE_CAPTURE_SCRIPT` if your checkout
lives elsewhere. Failure output is logged to
`~/.local/share/opencode/mempalace-live-capture.log` (append-only). Set
`MEMPALACE_LIVE_CAPTURE_DEBUG=1` to also surface plugin-side notes via
OpenCode's normal log.

#### Option B (local palaces only): the option-K npm plugin

If your palace-daemon runs on the same host as OpenCode (or you don't use
the daemon at all and have a local palace under `~/.mempalace/palace`),
the upstream option-K plugin will work after applying the two patches in
[Compatibility notes — option-K plugin](#compatibility-notes--option-k-plugin):

```bash
npm install -g opencode-plugin-mempalace
patch -d ~/.npm-global/lib/node_modules/opencode-plugin-mempalace/dist \
  < $REPO/examples/opencode/option-k-plugin-daemon-routing.patch
patch -d ~/.npm-global/lib/node_modules/opencode-plugin-mempalace/dist \
  < $REPO/examples/opencode/option-k-plugin-message-updated.patch
```

Add to `opencode.jsonc`:

```jsonc
{
  "plugin": ["opencode-plugin-mempalace"]
}
```

The plugin uses project-basename wings (`wing_<sanitized-dirname>`),
default 15-message threshold, session.idle flush, SIGINT/SIGTERM rescue,
pre-compaction injection. Closest semantics to MemPalace's Claude Code
stop-hook — but only works against a *local* palace because of the
"path-context" issue described in the compatibility notes.

### Compatibility notes — option-K plugin

`opencode-plugin-mempalace` v1.2.1 has three known issues filed upstream:

| Issue | What | Patch in this fork |
|---|---|---|
| [option-K#1](https://github.com/option-K/opencode-plugin-mempalace/issues/1) | `isInitialized()` passes `--palace` as a positional arg, forcing local-only behavior on daemon setups | `examples/opencode/option-k-plugin-daemon-routing.patch` |
| [option-K#4](https://github.com/option-K/opencode-plugin-mempalace/issues/4) | Plugin subscribes to `chat.message`, which OpenCode never publishes — counter never increments, plugin never mines | `examples/opencode/option-k-plugin-message-updated.patch` |
| [option-K#5](https://github.com/option-K/opencode-plugin-mempalace/issues/5) | Calls `mempalace mine <dir>`; remote daemon evaluates `<dir>` against its own filesystem, returns 400 (architectural) | No patch — Option A bypasses this entirely |

Issue #4 is the load-bearing bug for any setup: without it the plugin appears to
work (MCP connects, LLM responds) but **writes zero drawers**. #5 means
even with #4 patched, remote daemon setups still can't mine via the option-K
plugin — which is why this fork ships its own plugin (Option A).

Re-apply both patches after `npm update opencode-plugin-mempalace` (they
are idempotent — `patch --dry-run` first to confirm).

#### Patch target gotcha — opencode caches plugins independently of npm

OpenCode does **not** run plugins from `~/.npm-global/lib/node_modules/`.
On first use it copies the package into
`~/.cache/opencode/packages/opencode-plugin-mempalace@latest/` and runs
from there. Patching the global npm install has **no effect** on the
copy OpenCode actually executes. To make the patches stick:

```bash
patch -d ~/.cache/opencode/packages/opencode-plugin-mempalace@latest/dist \
  < $REPO/examples/opencode/option-k-plugin-daemon-routing.patch
patch -d ~/.cache/opencode/packages/opencode-plugin-mempalace@latest/dist \
  < $REPO/examples/opencode/option-k-plugin-message-updated.patch
```

Re-apply if the cache is cleared or the version pin moves.

### 5. Retrospective backfill

After install, ingest historical OpenCode sessions in one shot using the
bundled helper directly (the equivalent `mempalace mine --source opencode`
CLI flag will land with the upstream PR #1484 merge):

```bash
# Sweep the last 100 sessions (idempotent — daemon dedupes via entry hash):
python $REPO/examples/opencode/live-capture/capture-session.py --recent 100
```

The helper script reads `~/.local/share/opencode/opencode.db` (or the macOS
`~/Library/Application Support/opencode/...` path), yields one drawer per
session-exchange-pair, and POSTs through the daemon's `/silent-save`
endpoint. Wing routes from `session.directory` basename, matching the
live-capture plugin's taxonomy.

## What gets stored

A typical OpenCode turn produces a drawer like:

| Field | Value |
|---|---|
| `wing` | `wing_<project-basename>` (matches both adapter and live-capture plugin) |
| `room` | content-detected via `convo_miner.detect_convo_room` (`technical` / `decisions` / `problems` / etc.) |
| `source_file` | `opencode://<absolute-db-path>#session=<sid>` (adapter) or session export path (plugin) |
| `content` | verbatim user + assistant text, no summarization |
| `extract_mode` | `exchange` |
| Adapter-specific metadata | `session_id`, `session_title`, `project_dir`, `session_created_at`, `message_count`, `opencode_session_version`, `opencode_db_path` |

## Read-side recall

OpenCode agents can call any of the 49 MCP tools the daemon exposes:

- `mempalace_search` — semantic search across all drawers
- `mempalace_list_wings` / `mempalace_list_rooms` / `mempalace_get_taxonomy` — palace navigation
- `mempalace_kg_query` / `mempalace_kg_timeline` — knowledge graph
- `mempalace_diary_read` / `mempalace_diary_write` — agent diaries
- `mempalace_traverse` / `mempalace_find_tunnels` — cross-wing connections

The daemon serializes all writes through a single chokepoint, so multiple OpenCode windows + Claude Code + the live-capture plugin all coexist without HNSW corruption.

### A note on automatic context injection

The option-K plugin ships `experimental.session.compacting` and
`experimental.chat.system.transform` hooks that would, in principle,
inject palace context into the LLM's system prompt before every turn.
As of OpenCode 1.15.7:

- `experimental.session.compacting` is the only one that exists in the
  plugin API. It fires when the conversation is about to be compacted —
  rare in short sessions and never in `opencode run` mode.
- `experimental.chat.system.transform` is **not** in the documented plugin
  hook list ([opencode.ai/docs/plugins](https://opencode.ai/docs/plugins))
  and does not fire.

So today, agents recall memories by explicitly invoking the MCP tools
(`mempalace_search` etc.) — not via implicit per-turn system-prompt
injection. The instructions in
[`MEMPALACE.md`](https://code.claude.com/docs/en/memory)-style
project memory help nudge the agent to consult `mempalace_search`
proactively.

## Verifying the integration

After setup, in any OpenCode session:

```
> Use mempalace_status to confirm we're connected.
```

Expected: agent calls the tool and reports drawer count + wing list. If you see "no palace found" or auth errors, check that the wrapper script can read `~/.config/palace-daemon/env`.

To confirm live-capture is firing, watch `mempalace status` over a few minutes of OpenCode use — drawer count should increment.

### What "looks like an error but isn't"

OpenCode launched from this repo root logs the following on session start. None of these are errors despite ERROR-level appearance:

| Log line | What it is |
|---|---|
| `mcp stderr: palace-daemon: connected at http://familiar.jphe.in:8085` | The bridge announcing it found the daemon. Good. |
| (Older sessions) `mcp stderr: routing → local palace @ ~/.mempalace/palace` | Pre-2026-05-21 behavior. Indicates the spawn was opening the legacy local palace. Fixed by clearing the repo `.opencode/opencode.json` `mcp` block. If you still see it, your repo checkout is on an older commit — `git pull`. |
| (Older sessions) `mcp stderr: HNSW capacity divergence detected` | Same root cause as the line above — the bridge was opening the legacy local ChromaDB store whose HNSW index lagged its sqlite. Gone after the local palace was archived to `~/.mempalace/palace.retired-pre-pgcutover-2026-05-14/` and the `~/.mempalace/RETIRED` marker was added. |

### If you see a different palace count

If `mempalace_status` (via MCP) and `mempalace status` (via CLI) disagree on drawer count, you're hitting two different palaces. Common cause: the CLI ran from a shell without `PALACE_DAEMON_URL` exported, so it tried the local palace. On this fork the local palace is intentionally retired; the CLI will print the `~/.mempalace/RETIRED` marker text telling you to set `PALACE_DAEMON_URL`. If it shows a real drawer count instead of the retire message, you're running an older `mempalace` install (pre-2026-05-22) — pull main + restart the daemon.

## Coordination notes

- Upstream PR #1484 carries co-authored credit to JakobSachs for the original DB-schema spadework on PR #23.
- This fork-ahead carries 5 commits from #1484 + 2 commits from #1567 (see the `opencode-adapter-cherry-pick` and `opencode-mcp-config-cherry-pick` entries under `docs/fork-changes/`).
- option-K's plugin v1.2.1 has three open issues filed by JP — [#1](https://github.com/option-K/opencode-plugin-mempalace/issues/1) (daemon-routing), [#4](https://github.com/option-K/opencode-plugin-mempalace/issues/4) (`chat.message` vs `message.updated`), and [#5](https://github.com/option-K/opencode-plugin-mempalace/issues/5) (remote-daemon path mismatch). #1 and #4 have patches in `examples/opencode/`; #5 is architectural — the bundled `examples/opencode/live-capture/` plugin sidesteps it by reading OpenCode's SQLite DB client-side and POSTing drawers via the daemon's `/silent-save` endpoint.
