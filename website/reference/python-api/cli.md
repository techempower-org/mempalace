# `mempalace.cli`

Source: [`mempalace/cli/__init__.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/__init__.py)

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

### `class DaemonBusyError(DaemonError)`

The daemon answered that it is BUSY — JSON-RPC ``-32003``, or a message
saying so — and the request was not run (#526, PR 4).

A subclass so every ``except DaemonError`` keeps working. Its own class
because the reader's next move differs: an outage means check the daemon,
a refusal means fix the request, busy means retry shortly — and because it
was measured being rendered as **0 hits, exit 1**: the REST transports
returned the 200 error body verbatim and ``.get("results") or []`` turned
a saturated daemon into an empty corpus. Rendered by ``_fail_daemon`` as
``code: "daemon_busy"``, exit 2.

#### `__init__`

```python
def __init__(self, message: str, *, detail: str = '', route: str | None = None)
```

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

### `class DaemonAuthError(DaemonRequestError)`

The daemon answered and refused the CREDENTIAL — 401 or 403 (#518).

A subclass of ``DaemonRequestError`` because it is the same event class: the
daemon is up, it understood the request, and it declined. Under the #44
contract as restated by #476/#523 that is 64 ("the daemon answered and
rejected a well-formed request"), not 2 ("the operation could not run").
Reporting a wrong ``PALACE_API_KEY`` as *palace unavailable* sends the
operator to check whether the daemon is running when the fix is one line of
config — the same misattribution #499 removed for 400.

404 deliberately does NOT raise this. A missing route means "this daemon
cannot serve the verb", which is unavailability for that verb and stays 2;
it also stays ``None`` from ``_call_daemon_rest`` so the MCP fallback that
six call sites depend on is untouched.
