"""Contract tests for the Claude Code plugin's MCP server config.

Claude Code reads the plugin's MCP server from ``.claude-plugin/.mcp.json``.
In this fork that file had drifted from upstream to a host-specific
interpreter, ``${CLAUDE_PLUGIN_ROOT}/venv/bin/python``. That venv is never
committed and never synced between hosts, so on any host where it had not been
built by hand the server failed with ``ENOENT`` and every session outside the
memorypalace project had no palace search. Measured on familiar on 2026-10-03
with ``claude mcp list``.

These tests keep the three places that declare the server in agreement, and
keep the command something every install actually provides: the
``mempalace-mcp`` console script.

All tests are pure file inspection, with no subprocesses or network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 on CI
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MCP = REPO_ROOT / ".claude-plugin" / ".mcp.json"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
PROJECT_MCP = REPO_ROOT / ".mcp.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _servers(path):
    """Server map from an MCP config, with or without the ``mcpServers`` wrapper."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("mcpServers", data)


def _console_scripts():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return set(data["project"]["scripts"])


def test_plugin_mcp_config_is_valid_json_with_a_mempalace_server():
    servers = _servers(PLUGIN_MCP)
    assert "mempalace" in servers, f"no 'mempalace' server in {PLUGIN_MCP.name}: {servers}"


def test_plugin_mcp_command_is_a_shipped_console_script():
    """The command must be something every install provides, not a hand-built venv."""
    command = _servers(PLUGIN_MCP)["mempalace"]["command"]
    assert command in _console_scripts(), (
        f"plugin MCP command {command!r} is not a [project.scripts] entry point. "
        "An interpreter path such as ${CLAUDE_PLUGIN_ROOT}/venv/bin/python only "
        "exists where someone built that venv by hand, so the server fails with "
        "ENOENT everywhere else."
    )


def test_plugin_mcp_config_has_no_host_specific_values():
    """No absolute home paths and no hard-coded daemon host: those belong in the user's env."""
    text = PLUGIN_MCP.read_text(encoding="utf-8")
    assert not re.search(r"/home/[^/\s\"]+/", text), f"host-specific path in {PLUGIN_MCP.name}"
    assert not re.search(r"https?://[A-Za-z0-9.-]+:\d+", text), (
        f"hard-coded daemon URL in {PLUGIN_MCP.name}; mempalace-mcp reads it from the environment"
    )


def test_all_three_declarations_of_the_server_agree():
    """plugin .mcp.json, plugin.json mcpServers and the project .mcp.json name one server."""
    plugin_mcp = _servers(PLUGIN_MCP)["mempalace"]
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))["mcpServers"]["mempalace"]
    project = _servers(PROJECT_MCP)["mempalace"]
    assert plugin_mcp == manifest == project, (
        f"server declarations disagree:\n  .claude-plugin/.mcp.json: {plugin_mcp}\n"
        f"  .claude-plugin/plugin.json: {manifest}\n  .mcp.json: {project}"
    )
