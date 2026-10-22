# `mempalace.query_parser`

Source: [`mempalace/query_parser.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/query_parser.py)

query_parser.py — Parser for Palace Query Language (PQL) and Command DSL.

Converts concise single-line/multi-line DSL queries and structured JSON payloads
into normalized action dictionaries for palace_query, palace_exec, and palace_coordinate.

## Classes

### `class QueryParseError(ValueError)`

Raised when a PQL DSL string cannot be parsed.

### `class QuotedToken(str)`

A DSL token that was written in quotes; never treated as a keyword flag.

## Functions

### `tokenize_dsl`

```python
def tokenize_dsl(text: str) -> List[str]
```

Tokenize a DSL string into logical tokens, preserving quoted blocks.

### `parse_query_input`

```python
def parse_query_input(input_data: Any) -> Tuple[str, Dict[str, Any]]
```

Parse input to `palace_query`.

Returns:
    (target_operation, parsed_parameters_dict)

### `parse_exec_input`

```python
def parse_exec_input(input_data: Any) -> Tuple[str, Dict[str, Any]]
```

Parse input to `palace_exec`.

Returns:
    (action_operation, parsed_parameters_dict)

### `parse_coordinate_input`

```python
def parse_coordinate_input(input_data: Any) -> Tuple[str, Dict[str, Any]]
```

Parse input to `palace_coordinate`.

Returns:
    (coord_action, parsed_parameters_dict)
