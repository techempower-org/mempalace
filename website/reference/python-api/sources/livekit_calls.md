# `mempalace.sources.livekit_calls`

Source: [`mempalace/sources/livekit_calls.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sources/livekit_calls.py)

livekit-voice call-record source adapter.

Ingests the phone calls the livekit-voice agent makes and takes: one JSON
record per call at ``~/.local/share/livekit-voice/calls/&lt;call_id>.json``,
written atomically by ``lkvoice.records``. Each record's ``transcript`` is a
list of ``&#123;"role", "t", "text"}`` turns, ``user`` being the other party and
``assistant`` the agent.

A call holds an outside party's words and, often, benefits or health
details, so the adapter's privacy class is ``sensitive`` (RFC 002 §6) and
core files its drawers in the private collection that search, status and
wake-up never open. Drawer content is only the spoken words: the
model-written ``summary`` is not verbatim and is dropped, ``metrics`` carry
timings, not content, and the ``task`` the call was placed for is kept as
``call_task`` metadata rather than as something said. No
audio is stored in a record. Records still in progress, and anything under
``~/private``, are skipped.

## Classes

### `class LivekitCallsSourceAdapter(BaseSourceAdapter)`

Ingest livekit-voice call records into the private collection.

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
