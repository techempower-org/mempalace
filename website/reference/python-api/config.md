# `mempalace.config`

Source: [`mempalace/config.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/config.py)

MemPalace configuration system.

Priority: env vars > config file (~/.mempalace/config.json) > defaults

## Classes

### `class MempalaceConfig`

Configuration manager for MemPalace.

Load order: env vars > config file > defaults.

#### `__init__`

```python
def __init__(self, config_dir = None, palace_path = None)
```

Initialize config.

Args:
    config_dir: Override config directory (useful for testing).
                Defaults to ~/.mempalace.
    palace_path: Explicit palace data directory. This is primarily
                 used by CLI operations that received ``--palace``;
                 it takes precedence over environment and file config.

#### `search_config_fingerprint`

```python
def search_config_fingerprint(self) -> str
```

Stable digest of the effective search configuration for this process.

A long-running Hub keeps this configuration snapshot and may also keep
a collection opened from it.  CLI search forwarding compares this
digest with a freshly loaded config so a changed ``config.json`` falls
back to the direct path instead of querying stale Hub state.

Hash only resolved settings that affect the currently selected search
backend.  This detects relevant file edits and Hub-start environment
overrides without treating hook/UI settings or inactive-backend values
as stale search state.  The digest keeps secrets out of the registry.

#### `daemon_url`

```python
def daemon_url(self)
```

Optional palace-daemon URL. When set, mempalace's CLI and MCP
server route through palace-daemon's /mcp proxy instead of opening
a local chromadb client.

Resolution mirrors palace_path: env (``PALACE_DAEMON_URL``) wins,
``config.json`` key ``"daemon_url"`` as fallback, ``None`` means
run locally (current default).

See techempower-org/mempalace#49 — the env-only signal silently
failed when Claude Code's MCP spawn context didn't propagate the
env var, routing writes to a local palace while status read green.
Config-file fallback closes that gap for our multi-host deployment.

#### `daemon_strict`

```python
def daemon_strict(self) -> bool
```

True when daemon-strict routing is active.

Defaults True when ``daemon_url`` is set (env or config). Disable
explicitly via ``PALACE_DAEMON_STRICT=0`` env or ``"daemon_strict":
false`` in config.json — useful for test suites and offline
development where the daemon isn't reachable.

#### `auto_wake`

```python
def auto_wake(self)
```

Opt-in wake-on-demand for a sleeping palace host.

The daemon host may be a suspend-to-RAM machine where
"unreachable" routinely means "asleep", not "down". When
configured, connection-level failures in the CLI run the wake
command (a Wake-on-LAN sender or similar), wait for the daemon's
``/health``, and retry once. See :mod:`mempalace.auto_wake`.

``config.json`` accepts a command string::

    &#123;"auto_wake": "wakeonlan aa:bb:cc:dd:ee:ff"}

or an object with tuning knobs::

    &#123;"auto_wake": &#123;"command": "wakeonlan aa:bb:cc:dd:ee:ff",
                   "timeout_seconds": 45,
                   "poll_interval_seconds": 2}}

Returns a normalized dict (``command``, ``timeout_seconds``,
``poll_interval_seconds``) or ``None`` when disabled. The env
escape hatch ``PALACE_AUTO_WAKE=0`` force-disables without
editing config — useful for scripts that prefer fail-fast.
Garbage values fall back to defaults; a missing/empty command
disables (fail-open to "off": a typo must never make the CLI
run an unexpected shell command).

#### `palace_path`

```python
def palace_path(self)
```

Path to the memory palace data directory.

#### `tunnel_file`

```python
def tunnel_file(self)
```

Path to the tunnel file, sibling of palace_path.

#### `hallway_file`

```python
def hallway_file(self)
```

Path to the hallway file, sibling of palace_path.

Mirrors ``tunnel_file`` so within-wing hallway state is scoped to the
configured palace and survives palace rebuilds (it does not live in
ChromaDB which can be recreated). Prior to this property the path was
hardcoded under ``~/.mempalace/hallways.json`` and multiple palaces on
one host silently shared one file (see ``hallways._legacy_hallway_file``).

#### `collection_name`

```python
def collection_name(self)
```

Storage collection name.

#### `backend`

```python
def backend(self)
```

Storage backend name.

Chroma remains the default. PostgreSQL must be explicitly enabled with
MEMPALACE_BACKEND=postgres or config.json &#123;"backend": "postgres"}.

#### `backend_override`

```python
def backend_override(self)
```

Explicit backend selection from env/config, or None for auto/default resolution.

#### `cross_encoder_rerank`

```python
def cross_encoder_rerank(self) -> bool
```

Whether the optional cross-encoder rerank stage is enabled.

Off by default — preserves the zero-model-at-query-time default
(per JP's no-model-at-query-time rule from techempower-org/mempalace#179).
Opt in via ``MEMPALACE_RERANK_CROSS_ENCODER=1`` env or
``"cross_encoder_rerank": true`` in config.json. Env wins.

See ``mempalace.cross_encoder_rerank`` for the rerank stage
itself and the related model / top-N knobs.

#### `cross_encoder_model`

```python
def cross_encoder_model(self) -> str
```

Cross-encoder model name. Used only when ``cross_encoder_rerank``
is enabled. Defaults to ``cross-encoder/ms-marco-MiniLM-L-6-v2`` —
22M parameters, CPU-friendly, captures most of the rerank value
per the True Memory comparison (`docs/research/2026-05-24-true-memory-comparison.md`).
Override via ``MEMPALACE_RERANK_CROSS_ENCODER_MODEL`` env or
``"cross_encoder_model"`` in config.json.

#### `cross_encoder_top_n`

```python
def cross_encoder_top_n(self) -> int
```

How many top hits to rerank. Defaults to 25.

Override via ``MEMPALACE_RERANK_TOP_N`` env or
``"cross_encoder_top_n"`` in config.json. Latency scales linearly
with this value; the rerank only reorders, so it's a quality/cost
knob, not a recall floor.

#### `calibration_path`

```python
def calibration_path(self)
```

Optional path to a fitted confidence calibrator JSON.

When set, ``search_memories`` loads the calibrator and surfaces a
``confidence`` field (calibrated P(relevant)) on each vector hit.
When unset or the file is missing, no ``confidence`` field is
emitted — the system never fakes a calibrated score.

Resolution mirrors ``palace_path``: env (``MEMPALACE_CALIBRATION_PATH``)
wins, ``config.json`` key ``"calibration_path"`` as fallback,
``None`` means no calibration (current default).

#### `postgres_dsn`

```python
def postgres_dsn(self)
```

PostgreSQL DSN for the optional PostgreSQL backend.

#### `kg_backend`

```python
def kg_backend(self) -> str
```

Knowledge-graph backend name. SQLite stays the default.

Apache AGE is opt-in via ``MEMPALACE_KG_BACKEND=age`` or
``config.json &#123;"kg_backend": "age"}``. When set to ``age`` the
AGE backend uses ``postgres_dsn`` for its connection (AGE runs
in the same Postgres database as the storage backend can).

Lowercased before returning; falls back to ``"sqlite"`` on empty.

#### `hallway_backend`

```python
def hallway_backend(self) -> str
```

Hallway store backend. The JSON file stays the default.

Postgres is opt-in via ``MEMPALACE_HALLWAY_BACKEND=postgres`` or
``config.json &#123;"hallway_backend": "postgres"}``, and uses
``postgres_dsn`` for its connection.

The default is deliberately unchanged even on a postgres palace: the
hallway file is ~1 GB on the production host and the cutover is a
deliberate step taken after the migration has been verified against
real data, not a side effect of selecting a storage backend. A
chroma/sqlite install never needs postgres for hallways at all.

Lowercased before returning; falls back to ``"json"`` on empty.

#### `auto_query_enabled`

```python
def auto_query_enabled(self) -> bool
```

Whether the auto-query classifier is active.

Env ``AUTO_QUERY_ENABLED`` > config ``auto_query.enabled`` > False.

#### `auto_query_mode`

```python
def auto_query_mode(self) -> str
```

Auto-query mode: off, dry-run, conservative, balanced, aggressive.

Env ``AUTO_QUERY_MODE`` > config ``auto_query.mode`` > ``"off"``.

#### `auto_query_depth_cache_ttl`

```python
def auto_query_depth_cache_ttl(self) -> int
```

TTL (seconds) for the depth-refresh injection cache; 0 disables.

Env ``AUTO_QUERY_DEPTH_CACHE_TTL`` > config ``auto_query.depth_cache_ttl``
> 900. The depth query is deterministic per wing, so serving repeat
fires from cache trades sub-second staleness bounds for skipping a
~1s daemon round-trip on every 10th turn.

#### `auto_query_min_similarity`

```python
def auto_query_min_similarity(self) -> float
```

Confidence floor for auto-query injection (hybrid similarity).

Env ``AUTO_QUERY_MIN_SIMILARITY`` > config ``auto_query.min_similarity``
> 0.50. Fleet measurement (2026-09-03): relevant hits scored ~0.50–0.55,
uniformly irrelevant hits 0.44–0.47 with a 0.026 spread. Below the
floor the hook injects nothing — an honest "no hits" beats five bad
ones. Set to 0 to disable.

#### `auto_query_min_bm25`

```python
def auto_query_min_bm25(self) -> float
```

Floor for BM25-only candidates (no vector similarity) in auto-query.

Env ``AUTO_QUERY_MIN_BM25`` > config ``auto_query.min_bm25`` > 1.5.

#### `auto_query_max_per_turn`

```python
def auto_query_max_per_turn(self) -> int
```

Max auto-query invocations per turn.

Env ``AUTO_QUERY_MAX_PER_TURN`` > config ``auto_query.max_per_turn`` > 1.

#### `auto_query_max_per_minute`

```python
def auto_query_max_per_minute(self) -> int
```

Max auto-query invocations per minute (rate limit).

Env ``AUTO_QUERY_MAX_PER_MINUTE`` > config ``auto_query.max_per_minute`` > 6.

#### `compact_recovery_max_chars`

```python
def compact_recovery_max_chars(self) -> int
```

Ceiling on the palace text re-injected after a context compaction.

Env ``COMPACT_RECOVERY_MAX_CHARS`` > config
``compact_recovery.max_chars`` > 4000 (~1000 tokens). Wake-up L1 is
capped at ~800 tokens by construction; this is the backstop so a
recovery can never become the next context problem (#449).

#### `wing_aliases`

```python
def wing_aliases(self) -> dict
```

Mapping of directory basenames to canonical palace wing names.

Useful when a project directory name differs from its palace wing
(e.g., ``familiar.realm.watch`` → ``familiar_realm_watch``).

Config ``wing_aliases`` > empty dict.

#### `resolve_wing`

```python
def resolve_wing(self, directory_name: str) -> str
```

Resolve a project directory name to its canonical palace wing.

Checks ``wing_aliases`` first, then falls back to the default
normalization (lowercase, dots/dashes/spaces → underscores).

#### `room_aliases`

```python
def room_aliases(self) -> dict
```

Mapping of detected/input room names to canonical palace room names.

Useful for overriding auto-detected room names or unifying variants
(e.g., ``ui`` → ``frontend``, ``api`` → ``backend``).

Config ``room_aliases`` > empty dict.

#### `resolve_room`

```python
def resolve_room(self, room_name: str) -> str
```

Resolve a room name to its canonical palace room.

Checks ``room_aliases`` first, then falls back to the default
normalization (lowercase, dashes/spaces → underscores).

#### `qdrant_url`

```python
def qdrant_url(self)
```

Qdrant endpoint for the opt-in ``qdrant`` backend.

Defaults to localhost so selecting Qdrant never silently sends memory
to a remote service. Users can point at a LAN or cloud endpoint via
config or ``MEMPALACE_QDRANT_URL`` when they deliberately choose that.

#### `qdrant_api_key`

```python
def qdrant_api_key(self)
```

API key for the opt-in ``qdrant`` backend, if configured.

#### `qdrant_namespace`

```python
def qdrant_namespace(self)
```

Optional Qdrant collection namespace/prefix.

#### `qdrant_timeout`

```python
def qdrant_timeout(self)
```

Qdrant HTTP timeout in seconds.

#### `milvus_uri`

```python
def milvus_uri(self)
```

Milvus endpoint for the opt-in ``milvus`` backend.

Defaults to ``None`` so selecting Milvus uses per-palace Milvus Lite at
``&lt;palace>/milvus.db``. Set this only to deliberately use a shared
Milvus server, Zilliz Cloud, or a custom local Lite file.

#### `milvus_token`

```python
def milvus_token(self)
```

Token for the opt-in ``milvus`` backend, if configured.

#### `milvus_db_name`

```python
def milvus_db_name(self)
```

Optional Milvus database name for the opt-in ``milvus`` backend.

#### `milvus_namespace`

```python
def milvus_namespace(self)
```

Optional Milvus collection namespace/prefix.

#### `milvus_consistency_level`

```python
def milvus_consistency_level(self)
```

Milvus read consistency level for the opt-in ``milvus`` backend.

#### `pgvector_dsn`

```python
def pgvector_dsn(self)
```

Postgres DSN for the opt-in ``pgvector`` backend.

Defaults to a localhost DSN so selecting pgvector never silently sends
memory to a remote database. Point at a LAN or cloud Postgres via config
or ``MEMPALACE_PGVECTOR_DSN`` only when deliberately chosen.

#### `pgvector_namespace`

```python
def pgvector_namespace(self)
```

Optional pgvector table namespace/prefix for multi-tenant isolation.

#### `people_map`

```python
def people_map(self)
```

Mapping of name variants to canonical names.

#### `hooks_auto_save`

```python
def hooks_auto_save(self)
```

Whether the stop/precompact hooks should block for auto-save.

When False, hooks pass through without blocking — equivalent to
disabling auto-save while keeping hook scripts installed.

#### `topic_wings`

```python
def topic_wings(self)
```

List of topic wing names.

#### `hall_keywords`

```python
def hall_keywords(self)
```

Mapping of hall names to keyword lists.

#### `chunk_size`

```python
def chunk_size(self) -> int
```

Characters per drawer chunk (validated, ``>= 1``).

#### `chunk_overlap`

```python
def chunk_overlap(self) -> int
```

Overlap between adjacent chunks (validated, ``<= chunk_size // 2``).

#### `min_chunk_size`

```python
def min_chunk_size(self) -> int
```

Minimum chunk size — skip smaller chunks (validated, ``<= chunk_size``).

#### `min_chunk_size_explicit`

```python
def min_chunk_size_explicit(self)
```

Validated ``min_chunk_size`` iff the user explicitly set it.

Returns the coerced int when ``config.json`` defines a usable
``min_chunk_size`` (``>= 0`` and ``<= chunk_size``); ``None`` when
the key is absent/null or the value is unusable. ``convo_miner``
relies on the ``None`` sentinel to keep its lower 30-char floor
(more permissive than the 50-char project default, so short
exchanges are not dropped) for untuned users while still honoring
an explicit override —
replacing the raw, unvalidated ``_file_config`` reach that crashed
convo ingest on a bad key (#1024 review).

#### `entity_languages`

```python
def entity_languages(self)
```

Languages whose entity-detection patterns should be applied.

Reads from env var ``MEMPALACE_ENTITY_LANGUAGES`` (comma-separated)
first, then the ``entity_languages`` field in ``config.json``,
defaulting to ``["en"]``.

#### `set_entity_languages`

```python
def set_entity_languages(self, languages)
```

Persist the entity-detection language list to ``config.json``.

#### `embedding_device`

```python
def embedding_device(self)
```

Hardware device for the ONNX embedding model.

Values: ``"auto"`` (default), ``"cpu"``, ``"cuda"``, ``"coreml"``,
``"dml"``. Read from env ``MEMPALACE_EMBEDDING_DEVICE`` first, then
``embedding_device`` in ``config.json``, then ``"auto"``.

``auto`` resolves to the first available accelerator at runtime via
:mod:`mempalace.embedding`; requesting an unavailable accelerator
logs a warning and falls back to CPU.

#### `embedding_model`

```python
def embedding_model(self)
```

Embedding model identifier.

Values: ``"minilm"`` (ChromaDB's all-MiniLM-L6-v2 — English-only),
``"embeddinggemma"`` (multilingual, 100+ languages, default for
new installs since onboarding writes the choice), ``"adaptmem_ft"``
(a local fine-tuned SentenceTransformer checkpoint — see
:attr:`adaptmem_path`), or ``"openai-compat"`` (embeddings served by
an OpenAI-compatible ``/v1/embeddings`` endpoint — see
``embedding_api_url`` / ``embedding_api_model`` /
``embedding_api_key``). Read from env ``MEMPALACE_EMBEDDING_MODEL``
first, then ``embedding_model`` in ``config.json``, then ``"minilm"``
as a back-compat fallback for palaces created before onboarding asked
the question.

Switching models on an existing palace requires re-embedding
(different vector space) — ChromaDB rejects reads when the persisted
EF name doesn't match. Run ``mempalace repair rebuild-index`` after
changing this value.

#### `embedding_threads`

```python
def embedding_threads(self) -> int
```

Cap on the embedder's ONNX Runtime intra-op thread pool (#1068).

ChromaDB's ONNX embedder builds its ``InferenceSession`` with no thread
cap, so the intra-op pool defaults to the physical core count and a
background ``mine`` pins every core — stacked Stop-hook fires turn into
thermal events. ``OMP_NUM_THREADS`` is inert here (ORT owns its own
pool), so the cap is applied via ``SessionOptions`` in
:mod:`mempalace.embedding`.

Read from env ``MEMPALACE_EMBEDDING_THREADS`` first, then
``embedding_threads`` in ``config.json``. Semantics:

- unset / ``"auto"`` → half the logical CPUs (min 1), so a background
  mine leaves the machine usable out of the box.
- a positive integer → exactly that many intra-op threads.
- ``0`` or negative → uncapped: ORT's default (physical core count),
  for users who want maximum indexing throughput.

#### `embeddinggemma_batch_size`

```python
def embeddinggemma_batch_size(self) -> int
```

Documents per ``session.run()`` for the EmbeddingGemma ONNX model (#2330).

The sub-batching added for #1770 bounds a run by document COUNT, not
allocation: attention buffers scale with ``batch * padded_len ** 2``, so
a batch of long documents can still exceed available memory at the
module default (``mempalace.embedding._EMBEDDINGGEMMA_BATCH_SIZE``, 32).
Read from env ``MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE`` first, then
``embeddinggemma_batch_size`` in ``config.json``, then the module
default. Unset, non-numeric, or non-positive values fall back to the
default rather than raising; ``EmbeddinggemmaONNX.__init__`` still
raises on an explicitly-passed non-positive ``batch_size``.

#### `set_embedding_model`

```python
def set_embedding_model(self, model: str) -> None
```

Persist the embedding-model choice to ``config.json``.

Onboarding calls this once on first run. Accepts ``"minilm"`` or
``"embeddinggemma"``; other values are normalized to lowercase and
passed through (``embedding.get_embedding_function`` falls back to
minilm for unrecognized values).

#### `set_backend`

```python
def set_backend(self, backend: str) -> None
```

Persist the storage backend choice to ``config.json``.

#### `embedding_api_url`

```python
def embedding_api_url(self)
```

Base URL of the OpenAI-compatible ``/v1/embeddings`` endpoint.

Used only when ``embedding_model == "openai-compat"``. Resolved from
env ``MEMPALACE_EMBEDDING_API_URL`` first, then ``embedding_api_url``
in ``config.json``; ``None`` when unset. Accepts a bare host, a
``…/v1`` base, or a full endpoint URL.

#### `embedding_api_model`

```python
def embedding_api_model(self)
```

Server-side model id for the ``openai-compat`` embeddings endpoint.

Resolved from env ``MEMPALACE_EMBEDDING_API_MODEL`` first, then
``embedding_api_model`` in ``config.json``; ``None`` when unset.

#### `embedding_api_key`

```python
def embedding_api_key(self)
```

Optional bearer token / API key for the embeddings endpoint.

Resolved from env ``MEMPALACE_EMBEDDING_API_KEY`` first, then
``embedding_api_key`` in ``config.json``; ``None`` when unset (for
local endpoints that need no auth).

#### `adaptmem_path`

```python
def adaptmem_path(self)
```

Filesystem path to the AdaptMem fine-tuned encoder checkpoint.

Only consulted when ``embedding_model == "adaptmem_ft"``. Read from env
``MEMPALACE_ADAPTMEM_PATH`` first, then ``adaptmem_path`` in
``config.json``; ``None`` when neither is set (the encoder then raises a
clear error telling the user to set the path).

The checkpoint is a SentenceTransformer-shaped directory produced by
techempower-org/adaptmem. Switching an existing palace to this model is
a different vector space — run ``mempalace repair rebuild-index``.

#### `topic_tunnel_min_count`

```python
def topic_tunnel_min_count(self)
```

Minimum number of overlapping confirmed topics required to create
a cross-wing tunnel between two wings.

Default is ``1`` — any single shared topic produces a tunnel. Bump
to ``2+`` if your projects share lots of common-tech labels (Python,
Docker, Git) and you want only meaningfully overlapping wings to
link. Reads ``MEMPALACE_TOPIC_TUNNEL_MIN_COUNT`` env first, then the
config-file value, then ``1``.

#### `max_backups`

```python
def max_backups(self) -> int
```

Number of timestamped palace backups to retain before pruning.

Applies to the accumulating, timestamped backups created by
``mempalace migrate`` (``&lt;palace>.pre-migrate.&lt;timestamp>``) and
``mempalace repair max-seq-id``
(``chroma.sqlite3.max-seq-id-backup-&lt;timestamp>``). Each of those
commands writes a fresh full-size copy every run and historically
never deleted the old ones, so on a machine that mines or repairs on
a schedule the backup set could silently grow until it filled the
disk. After each backup is written, copies beyond this count (oldest
first) are removed.

Reads ``MEMPALACE_MAX_BACKUPS`` env first, then ``max_backups`` in
``config.json``, then the default of ``10``. A value of ``0`` disables
pruning and keeps every backup (use when an external retention policy
manages cleanup). Negative or non-numeric values fall back to the
default rather than crashing migrate/repair.

#### `lang_explicit`

```python
def lang_explicit(self)
```

Primary language code when explicitly configured, else ``None``.

Resolution order: ``MEMPALACE_LANG`` / ``MEMPAL_LANG`` env var, then
``config.json["lang"]``. Returns ``None`` if neither is set. Use this
when a caller needs to know whether the user has opted in to locale
behaviour (e.g. to avoid silently changing search scoring for palaces
that have never set a language).

#### `lang`

```python
def lang(self)
```

Primary language code for localized output and display.

Resolution order: ``lang_explicit`` (env or config.json), first entry
of ``entity_languages``, then ``"en"``. Always returns a non-empty
string so callers that need a language for display purposes never
have to handle ``None``. Code paths that must not silently change
behaviour for unconfigured palaces should read ``lang_explicit``
instead.

#### `hook_silent_save`

```python
def hook_silent_save(self)
```

Whether the stop hook saves directly (True) or blocks for MCP calls (False).

#### `hook_desktop_toast`

```python
def hook_desktop_toast(self)
```

Whether the stop hook shows a desktop notification via notify-send.

#### `resolve_write_routing`

```python
def resolve_write_routing(self, scope: str) -> ResolvedWriteRoutingPolicy
```

Resolve the configured write policy for ``hooks`` or ``cli``.

Precedence is:

1. scope-specific environment variable;
2. global environment variable;
3. legacy hook environment variable;
4. scope-specific config value;
5. global config value;
6. legacy hook config value;
7. ``direct``.

This foundation does not change current hook or CLI behavior. The
policy-aware consumers are introduced by follow-up PRs.

#### `hook_write_routing`

```python
def hook_write_routing(self) -> WriteRoutingPolicy
```

Resolved future routing policy for hook-triggered writes.

#### `cli_write_routing`

```python
def cli_write_routing(self) -> WriteRoutingPolicy
```

Resolved future routing policy for routine CLI writes.

#### `hook_verbatim_mode`

```python
def hook_verbatim_mode(self)
```

Skip truncation/noise-stripping in transcript ingest.

When True, ``normalize()`` preserves Claude Code system tags, hook
chrome, full Bash commands, full Bash output, full Grep/Glob match
lists, full Read/Edit/Write results, and uncapped tool inputs.
Default False — existing behavior is unchanged for upstream-shape
installs and for users who haven't opted in.

#### `hook_use_daemon`

```python
def hook_use_daemon(self)
```

Whether hooks should submit save/mine work to the opt-in daemon.

#### `set_hook_setting`

```python
def set_hook_setting(self, key: str, value: bool)
```

Update a hook setting and write config to disk.

#### `init`

```python
def init(self)
```

Create config directory and write default config.json if it doesn't exist.

#### `save_people_map`

```python
def save_people_map(self, people_map)
```

Write people_map.json to config directory.

Args:
    people_map: Dict mapping name variants to canonical names.

## Functions

### `strip_lone_surrogates`

```python
def strip_lone_surrogates(text: str) -> str
```

Replace lone UTF-16 surrogates with U+FFFD so the string is legal UTF-8 (#1235).

### `strip_nul_bytes`

```python
def strip_nul_bytes(text: str) -> str
```

Replace embedded NUL characters with U+FFFD before ChromaDB storage.

### `normalize_wing_name`

```python
def normalize_wing_name(name: str) -> str
```

Lower-case + collapse separators (`-`, ` `) to `_` for wing slugs.

The same rule is applied by ``init`` when persisting `topics_by_wing`
and when writing `mempalace.yaml`, so the miner's lookup matches at
mine time regardless of the source dirname.

Leading/trailing separators are stripped so a path-encoded dirname like
``-home-user-proj`` yields ``home_user_proj`` rather than a leading-
underscore slug that ``sanitize_name`` (and thus the MCP write tools)
would reject.

### `sanitize_name`

```python
def sanitize_name(value: str, field_name: str = 'name') -> str
```

Validate and sanitize a wing/room/entity name.

Raises ValueError if the name is invalid.

### `sanitize_kg_value`

```python
def sanitize_kg_value(value: str, field_name: str = 'value') -> str
```

Validate a knowledge-graph entity name (subject or object).

More permissive than sanitize_name — allows punctuation like commas,
colons, and parentheses that are common in natural-language KG values.
Only blocks null bytes and over-length strings.

Not used for wing/room names (which have filesystem constraints) or
predicates (which should be simple relationship identifiers).

### `sanitize_session_id`

```python
def sanitize_session_id(value) -> str
```

Reduce a harness session id to a safe, indexable token.

Returns ``""`` when nothing usable survives. Callers then *omit* the
metadata key rather than storing a placeholder: an absent key is
honest, whereas a synthetic ``"unknown"`` is a real id that matches
nothing and would pool unrelated sessions under one name.

Strips rather than raises, unlike ``sanitize_name``. ``session_id``
is a passenger on write paths whose whole job is to not lose a
memory, so a malformed id must degrade to "unattributed" and never
fail the write -- the same call ``tool_checkpoint`` already makes
when its dedup probe errors.

### `sanitize_iso_temporal`

```python
def sanitize_iso_temporal(value, field_name: str = 'date')
```

Validate an ISO-8601 date or canonical UTC datetime string.

Accepts ``None`` and ``""`` as pass-through values.

Accepted non-empty string forms:

- ``YYYY-MM-DD``
- ``YYYY-MM-DDTHH:MM:SSZ``
- ``YYYY-MM-DDTHH:MM:SS+00:00`` normalized to ``...Z``

Partial dates are rejected because KG queries compare TEXT temporal values.
Non-canonical datetime forms are rejected because mixed temporal string
formats can silently return wrong KG query results.

### `sanitize_iso_date`

```python
def sanitize_iso_date(value, field_name: str = 'date')
```

Backward-compatible wrapper for ISO temporal validation.

Historically this accepted only full dates. It now also accepts canonical
UTC datetimes, but the old name is kept so existing imports continue to
work.

### `sanitize_content`

```python
def sanitize_content(value: str, max_length: int = 100000) -> str
```

Validate drawer/diary content length.

### `normalize_milvus_consistency_level`

```python
def normalize_milvus_consistency_level(value) -> str
```

### `sqlite_read_uri`

```python
def sqlite_read_uri(db_path: str) -> str
```

Return a read-only ``file:`` URI for ``sqlite3.connect(..., uri=True)``.

A bare ``f"file:&#123;db_path}?mode=ro"`` mis-parses paths containing spaces or
other URI-reserved characters — common in real home directories (a Windows
user folder like ``First Last``, many macOS paths). ``pathname2url``
percent-encodes the path and normalizes separators so the database opens on
every platform.

### `get_configured_collection_name`

```python
def get_configured_collection_name() -> str
```

Return the configured drawer collection name without repeated config-file reads.
