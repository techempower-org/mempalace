# MemPalace Hooks — Auto-Save for Terminal AI Tools

These hook scripts make MemPalace save automatically. No manual "save" commands needed.

This file covers the **Claude Code** and **Codex CLI** hooks that live
flat under `hooks/`. For the **Cursor IDE** hooks, see
[`hooks/cursor/README.md`](cursor/README.md) or the rendered docs at
[`website/guide/cursor-hooks.md`](../website/guide/cursor-hooks.md). The
two are additive and share the same `~/.mempalace/hook_state/`
directory.

If you are trying to protect existing Claude Code transcripts immediately,
use the short checklist first: [`website/guide/claude-code-retention.md`](../website/guide/claude-code-retention.md).
It covers hook wiring, JSONL backup, and one-time backfill.

## What They Do

| Hook | When It Fires | What Happens |
|------|--------------|-------------|
| **Save Hook** | Every 15 human messages | Saves a diary entry with theme extraction, auto-mines transcript into the palace |
| **SessionEnd Hook** | Clean session exit | Backgrounds a final transcript mine (when a transcript exists) so short sessions aren't lost; returns immediately so teardown is never delayed. A lightweight diary checkpoint is written in the detached child. |
| **PreCompact Hook** | Right before context compaction | Emergency save — diary entry + transcript mining before context is lost |
| **SessionStart Hook** | Every session start | One visible line about the palace (alive?, size, this wing's depth). On `source=compact` it instead runs post-compaction recovery: clears auto-query's per-session suppression list and re-injects the wake-up story as content — see below. |

## Post-Compaction Recovery (SessionStart, `source=compact`)

A compaction keeps the session id and throws the context away. Two things
follow, and both were measured on the fleet:

1. Auto-query's per-session dedupe
   (`~/.mempalace/auto_query/injected/<session>.json`) records which drawers
   have already been shown so they are not repeated. After a compaction those
   drawers are gone from the agent and still marked shown — so the dedupe
   suppresses exactly the material that was just lost, and suppresses more of
   it the longer the session ran. One live session had **119** ids in that file.
2. The old branch injected a *pointer* ("run `mempalace wake-up`"). Agents do
   not act on invitations; hooks that inject without asking do.

`hooks/palace-session-start.sh` therefore delegates the compact case to
`python -m mempalace.compact_recovery`, which

* clears the session's dedupe file (unconditionally, before any network call)
  and reports how many ids it dropped, and
* fetches the wake-up story for the project's wing and returns it as
  `additionalContext` — content, not a hint.

It reads Claude Code's SessionStart JSON on stdin, prints the hook payload on
stdout, and always exits 0. If the palace is unreachable the dedupe is still
cleared and the visible line says so. The injected text is capped by
`compact_recovery.max_chars` in `~/.mempalace/config.json` (env
`COMPACT_RECOVERY_MAX_CHARS`, default 4000 characters ≈ 1000 tokens).

## Save Architecture

Hooks have two **save modes**, controlled by `hook_silent_save` in `~/.mempalace/config.json`:

| Mode | Config | How It Saves | AAAK? | Deterministic? |
|------|--------|-------------|-------|----------------|
| **Silent** (default) | `hook_silent_save: true` | Direct Python API call — `tool_diary_write()` with plain text, no AI involved | No — plain English | Yes — save always happens |
| **Block** (legacy) | `hook_silent_save: false` | Blocks the AI, shows a reason message, asks AI to call MCP tools | Maybe — AI sees AAAK in MCP tool descriptions and may use it | No — AI may ignore, summarize, or fail |

**Silent mode is recommended.** It calls `tool_diary_write()` directly via Python import — no MCP roundtrip, no blocking, no AI interpretation needed. The save marker only advances after a confirmed write, so data loss is impossible. A one-line terminal notification (`"✦ N memories woven into the palace — themes"`) confirms each save.

**Block mode is the upstream default.** It returns `{"decision": "block", "reason": "..."}` asking the AI to call MemPalace MCP tools. This path is non-deterministic — the AI may ignore the instruction, summarize instead of quoting verbatim, or write to the wrong memory system. The save marker advances before the AI acts, so if the save fails, the checkpoint is silently lost.

Both modes also **auto-mine the JSONL transcript** directly into the palace, capturing raw tool output (Bash results, search findings, build errors) that the AI would otherwise summarize away. This is belt-and-suspenders — tool output is stored regardless of which save mode is active.

### AAAK and Save Paths

AAAK is upstream's compressed symbolic summary format (`mempalace/dialect.py`). It is **not a code feature** — it's a prompt embedded in MCP tool descriptions that coaches the AI to write diary entries in a shorthand notation.

- **Silent mode**: No AI reads the MCP tool descriptions. Diary entries are plain English. AAAK is irrelevant.
- **Block mode**: The AI sees `diary_write`'s tool description ("write in AAAK format"). It may produce AAAK-formatted entries. The `tool_diary_write()` function accepts any string — it doesn't validate or enforce AAAK.

### Tandem Memory Systems

Claude Code has its own auto-memory system (`~/.claude/projects/*/memory/*.md`) alongside MemPalace. Both are useful:

- **Auto-memory**: Lightweight preferences, context, feedback
- **MemPalace**: Verbatim conversations, tool output, code — deep searchable history

The hook block reasons say "For THIS save, use MemPalace MCP tools only" — scoped to the hook save event, not a permanent ban on auto-memory. Both systems are used in tandem during normal conversation.

## Install — Claude Code

Add to `.claude/settings.local.json`:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/hooks/mempal_save_hook.sh",
        "timeout": 30
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/hooks/mempal_session_end_hook.sh",
        "timeout": 10
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/hooks/mempal_precompact_hook.sh",
        "timeout": 30
      }]
    }]
  }
}
```

`SessionEnd` runs once on a clean exit and backgrounds its work, so it
returns instantly and stays within Claude Code's SessionEnd budget. Wired
through `settings.local.json` (above) the `timeout` can raise that budget;
the bundled plugin cannot, which is why the hook backgrounds rather than
mining in the foreground.

Make them executable:
```bash
chmod +x hooks/mempal_save_hook.sh hooks/mempal_session_end_hook.sh hooks/mempal_precompact_hook.sh
```

## Install — Antigravity (Google)

The Antigravity integration lives in its own subdirectory because the
wire format (camelCase JSON, `injectSteps[]` output) and event names
(`Stop`, `PreInvocation`) are Antigravity-specific. Use the dedicated
installer:

```bash
bash hooks/antigravity/install.sh
```

This installs to `~/.gemini/config/plugins/mempalace/`, registers the
MCP server, ships the `mempalace` skill, and wires the Stop +
PreInvocation hooks. See [`hooks/antigravity/README.md`](antigravity/README.md)
for the full guide and [`hooks/antigravity/INVESTIGATION.md`](antigravity/INVESTIGATION.md)
for the source-of-truth audit of which Antigravity surfaces the
integration uses.

## Install — Codex CLI (OpenAI)

Add to `.codex/hooks.json`:

```json
{
  "Stop": [{
    "type": "command",
    "command": "/absolute/path/to/hooks/mempal_save_hook.sh",
    "timeout": 30
  }],
  "PreCompact": [{
    "type": "command",
    "command": "/absolute/path/to/hooks/mempal_precompact_hook.sh",
    "timeout": 30
  }]
}
```

**Other harnesses:** the clean-exit save runs through the harness-agnostic
`mempalace hook run --hook session-end` entry point. This release wires it
for Claude Code. Antigravity exposes no dedicated session-end event (its
lifecycle hooks are PreToolUse/PostToolUse/PreInvocation/PostInvocation/Stop,
and MemPalace already saves there via `Stop`); Cursor and Codex can adopt the
same entry point as a follow-up wherever their own session-end event is available.

## Configuration

Edit `mempal_save_hook.sh` to change:

- **`SAVE_INTERVAL=15`** — How many human messages between saves. Lower = more frequent saves, higher = less interruption.
- **`STATE_DIR`** — Where hook state is stored (defaults to `~/.mempalace/hook_state/`)
- **`MEMPAL_DIR`** — Optional **project directory** (code, notes, docs) to also mine on each save trigger, with `--mode projects`. The hook ALWAYS mines the active conversation transcript automatically with `--mode convos` — `MEMPAL_DIR` is purely additive, never an override. Leave blank if you don't want to ingest project files.
- **`MEMPAL_PYTHON`** — Optional env var. Python interpreter with mempalace + chromadb installed. Auto-detects: `MEMPAL_PYTHON` env var → repo `venv/bin/python3` → system `python3`. Set this if your venv is in a non-standard location.

### Disabling Auto-Save (Silent Mode)

To keep hooks installed but disable auto-save blocking entirely, set `hooks.auto_save` to `false` in your config:

**Option 1 — config file** (`~/.mempalace/config.json`):
```json
{
  "hooks": {
    "auto_save": false
  }
}
```

**Option 2 — environment variable:**
```bash
export MEMPALACE_HOOKS_AUTO_SAVE=false
```

When disabled, both the stop hook and precompact hook pass through without blocking. You can still save manually with `mempalace mine <dir> --mode convos`.

### mempalace CLI

The relevant commands are:

```bash
mempalace mine <dir>               # Mine all files in a directory
mempalace mine <dir> --mode convos # Mine conversation transcripts only
```

The hooks resolve the repo root automatically from their own path, so they work regardless of where you install the repo.

## How It Works (Technical)

### Save Hook (Stop event)

```
User sends message → AI responds → Claude Code fires Stop hook
                                            ↓
                                    Hook counts human messages in JSONL transcript
                                            ↓
                              ┌─── < 15 since last save ──→ echo "{}" (let AI stop)
                              │
                              └─── ≥ 15 since last save
                                            ↓
                                    Auto-mine transcript → palace (tool output captured)
                                            ↓
                              ┌─── silent mode (default) ──────────────────────────┐
                              │     _save_diary_direct() — plain text diary entry  │
                              │     Marker advances AFTER confirmed write          │
                              │     {"systemMessage": "✦ N memories woven..."}     │
                              └────────────────────────────────────────────────────┘
                              ┌─── block mode (legacy) ────────────────────────────┐
                              │     {"decision": "block", "reason": "save..."}     │
                              │     Marker advances BEFORE AI acts (data loss risk)│
                              │     AI saves → tries to stop → stop_hook_active    │
                              │     → hook lets it through                         │
                              └────────────────────────────────────────────────────┘
```

In silent mode, no AI interaction is needed — the hook saves and returns immediately. In block mode, the `stop_hook_active` flag prevents infinite loops: block once → AI saves → tries to stop → flag is true → we let it through.

### PreCompact Hook

```
Context window getting full → Claude Code fires PreCompact
                                        ↓
                                Find transcript (from input or session_id lookup)
                                        ↓
                                Auto-mine transcript → palace (tool output captured)
                                        ↓
                              ┌─── silent mode: diary entry + systemMessage
                              │
                              └─── block mode: {"decision": "block", "reason": "save everything..."}
                                        ↓
                                Compaction proceeds
```

No counting needed — compaction always warrants a save. The auto-mine captures raw tool output before the AI gets a chance to summarize it away.

## Debugging

Check the hook log:
```bash
cat ~/.mempalace/hook_state/hook.log
```

Example output:
```
[14:30:15] Session abc123: 12 exchanges, 12 since last save
[14:35:22] Session abc123: 15 exchanges, 15 since last save
[14:35:22] TRIGGERING SAVE at exchange 15
[14:40:01] Session abc123: 18 exchanges, 3 since last save
```

## Backfill Past Conversations

The hooks only capture conversations going forward. To mine **past** Claude Code sessions into your palace, run a one-time backfill:

```bash
mempalace mine ~/.claude/projects/ --mode convos
```

This scans all JSONL transcripts from previous sessions and files them into the `conversations` wing. On a typical developer machine with months of history, this can yield 50K–200K drawers.

For Codex CLI sessions:
```bash
mempalace mine ~/.codex/sessions/ --mode convos
```

This only needs to be done once — after that, the hooks auto-mine each session as you go.

## Known Limitations

**Hooks require session restart after install.** Claude Code loads hooks from `settings.json` at session start only. If you run `mempalace init` or manually edit hook config mid-session, the hooks won’t fire until you restart Claude Code. This is a Claude Code limitation.

**`MEMPAL_PYTHON` override for the hook's internal Python calls.** The save hook parses its JSON input and counts transcript messages with `python3`. When the harness is launched from a GUI on macOS — `open -a`, Spotlight, the dock — its `PATH` is the minimal `/usr/bin:/bin:/usr/sbin:/sbin` inherited from `launchd`, not your shell PATH. If `python3` isn't on that PATH, those internal calls fail and the hook can't count exchanges.

Point the hook at any Python 3 interpreter to fix it:

```bash
export MEMPAL_PYTHON="/usr/bin/python3"                   # system Python is fine
export MEMPAL_PYTHON="$HOME/.venvs/mempalace/bin/python"  # or your venv
```

Resolution priority: `$MEMPAL_PYTHON` (if set and executable) → `$(command -v python3)` → bare `python3`. The interpreter only needs `json` and `sys` from the standard library — `mempalace` itself does not need to be installed in it.

Note: the `mempalace mine` auto-ingest runs via the `mempalace` CLI, so that command also needs to be on the hook's `PATH`. Installing with `pipx install mempalace` or `uv tool install mempalace` puts it on a stable global location; otherwise extend the hook environment's `PATH` to include your venv's `bin/`.

## Backfill Past Conversations

The hooks only capture conversations going forward. To mine **past** Claude Code sessions into your palace, run a one-time backfill:

```bash
mempalace mine ~/.claude/projects/ --mode convos
```

This scans all JSONL transcripts from previous sessions and files them into the `conversations` wing. On a typical developer machine with months of history, this can yield 50K–200K drawers.

For Codex CLI sessions:
```bash
mempalace mine ~/.codex/sessions/ --mode convos
```

This only needs to be done once — after that, the hooks auto-mine each session as you go.

## Cost

**Zero extra tokens.** The hooks save in the background — the AI doesn’t need to write anything in the chat. All filing is handled automatically.
