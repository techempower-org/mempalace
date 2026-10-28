# `mempalace.sources.slack`

Source: [`mempalace/sources/slack.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sources/slack.py)

Slack source adapter: channel history, one drawer set per channel per day.

Reads the channels listed under ``slack_sources`` in the palace config::

    "slack_sources": [
      &#123;"channel": "events", "wing": "homefront"},
      &#123;"channel": "D0123ABCD", "wing": "plan_realm_watch", "require": "_— JP Queue_"}
    ]

``channel`` is a channel name or id. ``require`` is an optional regular
expression a top-level message must match to be kept: it lets a direct-message
channel yield one kind of message (a daily brief) and nothing else. The token
comes from ``MEMPALACE_SLACK_TOKEN`` in the environment, never from the config,
and only read methods are called.

Each day of a channel becomes one verbatim transcript (``[HH:MM] Name: text``,
thread replies indented under their parent), chunked like a conversation, with
``source_file`` ``slack://&lt;team_id>/&lt;channel_id>/&lt;YYYY-MM-DD>`` (stable ids, and the day in
``slack_timezone`` from the palace config, UTC by default). A chunk that matches a
sensitive marker (``mempalace.sensitive``) labels its whole day ``sensitive``,
and core files that day into the private collection. Every run re-reads the full history and
declares each channel a snapshot scope, so core removes whatever the run no longer
produced: a shrunk or vanished day, a message the filter now excludes, or a day's
copy in the collection it no longer belongs to.

## Classes

### `class SlackSourceAdapter(BaseSourceAdapter)`

Ingest the Slack channels configured under ``slack_sources``.

#### `__init__`

```python
def __init__(self, call: Optional[SlackCall] = None, sources: Optional[List[dict]] = None, tz_name: Optional[str] = None)
```

#### `snapshot_scopes`

```python
def snapshot_scopes(self) -> List[dict]
```

#### `ingest`

```python
def ingest(self, *, source: SourceRef, palace: PalaceContext) -> Iterator[DrawerRecord]
```

#### `describe_schema`

```python
def describe_schema(self) -> AdapterSchema
```

#### `source_summary`

```python
def source_summary(self, *, source: SourceRef) -> SourceSummary
```
