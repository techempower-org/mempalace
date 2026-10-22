# `mempalace.mcp_server`

Source: [`mempalace/mcp_server/__init__.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/__init__.py)

MemPalace MCP Server — read/write palace access for Claude Code
================================================================
Install: claude mcp add mempalace -- mempalace-mcp [--palace /path/to/palace]

The public import path remains ``mempalace.mcp_server``. Implementation is
split across this package so PRs can target a domain file instead of one
8 000-line module. Fragment files are executed into this package's globals
(the same namespace as the historical module), which preserves:

* ``from mempalace.mcp_server import tool_status, TOOLS, handle_request, main``
* test monkeypatches on ``mempalace.mcp_server._config`` / ``_get_kg`` / ...
* ``python -m mempalace.mcp_server``
* stdio protection running before chromadb import

Tools (read):
  mempalace_status          — total drawers, wing/room breakdown
  mempalace_list_wings      — all wings with drawer counts
  mempalace_list_rooms      — rooms within a wing
  mempalace_get_taxonomy    — full wing → room → count tree
  mempalace_search          — semantic search, optional wing/room/source_file filter
  mempalace_check_duplicate — check if content already exists before filing

Tools (write):
  mempalace_add_drawer      — file verbatim content into a wing/room
  mempalace_delete_drawer   — remove a drawer by ID
  mempalace_delete_by_source — bulk-remove all drawers mined from one source_file

Tools (maintenance):
  mempalace_reconnect       — force cache invalidation and reconnect after external writes
