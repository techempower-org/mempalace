# `mempalace.searcher.render`

Source: [`mempalace/searcher/render.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/render.py)

## Functions

### `render_with_line_numbers`

```python
def render_with_line_numbers(text: 'str | None', start_line: int = 1) -> str
```

Prefix each line of ``text`` with ``[N] `` for read-time grid display.

Lines that already begin with ``[&lt;digits>]`` pass through unchanged,
but the counter still advances on them so callers can rely on positional
alignment with the original line indices.

``None`` is treated as empty string. Pure function.

### `extract_line_range`

```python
def extract_line_range(text: str, line_start: int, line_end: int) -> str
```

Return the 1-indexed inclusive slice ``[line_start, line_end]`` rendered with line numbers.

This is the closet-pointer read path. A pointer like ``→2026-01-18:L55-L72``
resolves by opening the day-drawer and calling ``extract_line_range(drawer_text, 55, 72)``.
Out-of-bounds ranges are clamped. Invalid ranges return ``""``.
