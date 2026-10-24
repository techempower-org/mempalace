# `mempalace.searcher.cli_search`

Source: [`mempalace/searcher/cli_search.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/cli_search.py)

## Functions

### `search`

```python
def search(*args, **kwargs)
```

The CLI search (see :func:`_search_delegating`). A model error raised on the way (the fork's pre-open
probes open the collection outside upstream's ``_open_collection_or_explain``) prints upstream's single
``mempalace: &lt;message>`` line on stderr and raises :class:`SearchError`, never a traceback.
