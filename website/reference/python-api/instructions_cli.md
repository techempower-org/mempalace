# `mempalace.instructions_cli`

Source: [`mempalace/instructions_cli.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/instructions_cli.py)

Instruction text output for MemPalace CLI commands.

Each instruction lives as a .md file in the instructions/ directory
inside the package. The CLI reads and prints the file content.

## Functions

### `run_instructions`

```python
def run_instructions(name: str)
```

Read and print the instruction .md file for the given name.

### `validate_identity_component`

```python
def validate_identity_component(name: str, value: str) -> str
```

Return a stripped identity component, or raise ValueError.

### `apply_mcp_shape`

```python
def apply_mcp_shape(body: str, mcp: str) -> str
```

Swap classic MCP tool names for the lightweight triad, or leave as-is.

### `render_shared_brain_rules`

```python
def render_shared_brain_rules(host: str, harness: str, project: str, mcp: str = 'full') -> str
```

Render the canonical shared-brain rules block for one agent identity.

The template ships inside the package and is test-pinned to the
System-Prompt Snippet in integrations/shared/coordination-protocol.md,
so every harness pastes the same battle-tested block and a protocol
lesson lands in one file instead of N system prompts. The output is
wrapped in HTML-comment markers so a later re-render can replace the
block in place.

``mcp`` selects tool names: ``full`` (default, the 52-tool server) or ``light``
(palace_query / palace_exec / palace_coordinate). Prose is identical;
only the tool tokens change.

### `run_rules`

```python
def run_rules(host: str, harness: str, project: str, mcp: str = 'full')
```

Print the rendered shared-brain rules block for the CLI.
