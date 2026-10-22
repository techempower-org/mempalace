"""Allow ``python -m mempalace.mcp_server``."""

from . import _ensure_server_argv, main

# The package was imported before this file ran, when argv[0] was still "-m"; apply --palace/--backend now.
_ensure_server_argv()
main()
