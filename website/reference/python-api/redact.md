# `mempalace.redact`

Source: [`mempalace/redact.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/redact.py)

Secrets never reach a drawer: every write replaces them with a placeholder first.

A session transcript can print a credential: a shell expansion, an ``env``
dump, a config file read for debugging. Mined as is, the value lands in a
drawer that any agent can search, and in the private collection it would
still be a stored copy (#596). So every backend collection's ``add``,
``upsert`` and ``update`` pass their documents and the string values of their
metadata through :func:`redact` before the backend sees them; see
``BaseCollection.__init_subclass__``. A secret becomes
``[redacted &lt;kind> sha256:&lt;first 8 hex>]``. The short hash lets someone who
already holds a candidate check it against the drawer (a vault item, a leak
report); for a provider token, a long random string, it recovers nothing. A
match of a configured pattern, which may be a guessable value, is
``[redacted custom]`` with no hash, and so is a one-time code
(``[redacted code]``), six digits being easy to guess. Existing placeholders are
never matched again, so a write is redacted the same way however many layers
pass it through.

Patterns are regular expressions: :data:`DEFAULT_PATTERNS`, keyed by kind,
plus any listed under ``secret_patterns`` in the palace config (kind
``custom``). They match a credential's shape, never a value, so the config
never holds one. Every write checks the patterns, even one with nothing to
scan: a config that cannot be read, or a pattern that does not compile,
raises :class:`SecretPatternsUnavailable` and the write fails rather than
store text unscanned.

Prefix-free shapes (#619, 2026-11-15, after a judge's free-text output quoted a bare 32-character
key): ``token`` (20+ of [A-Za-z0-9_-] with upper, lower and digit), ``hex`` (32+ hex digits EXCEPT
exactly 40, which is a git sha: a 40-hex secret passes, and sha256 digests are redacted), and
``quoted`` (a quoted 17+ run from the secret alphabet [A-Za-z0-9+/=_-] with three classes, or two
plus "+" or "="; not starting "/"; quoted paths, URLs, dotted names and timestamps are left, so a
quoted secret holding "." or ":" passes this rule). A bare lower-case password matches no shape:
for that, ``secret_vault_items`` in the config lists vault item ids and labels (never values);
each value is fetched in process at pattern load (``bw --nointeraction``), or handed over by the
writer that spawned this process (:func:`needle_seed_fd`), and replaced on word boundaries with
``[redacted vault &lt;label>]``. An item that cannot be fetched (a locked vault)
fails every write closed, as an unreadable config does: a stale palace is visible and
recoverable, a re-mined password is not.

Known limits. Vendor shapes catch tokens with a recognisable prefix. A bare
password or a provider token without one is caught only beside a secret-ish
name (``NEXTAUTH_SECRET=…``, ``"password": "…"``) or as a URL's password
(``postgres://user:…@``), by :func:`_redact_assignments`, which keeps the
name and writes ``[redacted assignment]``; one printed alone passes through.
A quoted value, or one after a tight ``=``, is replaced whatever it looks
like; a bare value after ``: `` or `` = `` (YAML, prose, code) only when it is
8+ characters of two or more kinds, so ``password: hunter2`` stays readable.
A PIN name takes only a run of 4+ digits (grouped or spelled out). A Bitwarden
custom field is redacted by a secret-ish sibling ``name`` or a hidden
``"type": 1`` in its own object, in any key order, and an item's ``notes``
only near ``"object": "item"`` (proximity: an adjacent ``notes`` may be
over-redacted). A one-time code is caught only beside the words that say what
it is ("is your Google verification code", "sign-in code is"), and only its
digits are replaced. A JWT is caught only with a compact ``eyJ`` header. A short ``sk-`` key (LiteLLM's) is
caught only when it holds two of upper case, lower case and digits, so a mixed-case name after
``sk-`` (``sk-ManagementDashboardOverview``) is over-redacted and a key of letters in one case passes.
A quote that never closes leaves only its first word redacted. Embeddings passed in by
the caller were computed from the text before redaction. Configured patterns
are trusted like the rest of the config: one with nested quantifiers can
backtrack badly on long text, so keep them to a prefix and a character class.
Metadata keys are not scanned, only values. A configured pattern is scanned
inside a named group, so it must not use numbered group references, must scope
any inline flag (``(?i:…)``, not ``(?i)``), and must not name a group
starting ``_mpr``; one that breaks these fails closed.

## Classes

### `class SecretPatternsUnavailable(RuntimeError)`

The configured secret patterns could not be read or compiled. Writing without them could
store a credential they would have caught, so the write stops instead.

## Functions

### `refresh_vault_needles`

```python
def refresh_vault_needles(stop: Optional[threading.Event] = None) -> Dict[str, str]
```

Refresh the configured vault items off the write path, fetching only the items that need it: no
good value kept, the item's revisionDate in the vault CLI's local copy moved since its last good
fetch (:func:`_vault_revisions`; no CLI call), or that fetch older than ``_VAULT_BACKSTOP``. When the
revisions cannot be read, every item is fetched, as before. Never syncs.
One refresh runs at a time, and a fetch is kept only if it is newer than the kept one. With ``stop``
set by the time the fetches return, nothing is kept. Returns &#123;label: "ok" | "unchanged (&lt;age> s)" |
"kept (&lt;age> s)" | "expired (&lt;age> s)" | "none"}, each failure followed by this module's own reason
(no CLI output); expired: kept past ``_VAULT_MAX_KEEP``, so writes fail closed. The next patterns()
call recompiles from the new values.

### `start_vault_refresher`

```python
def start_vault_refresher(interval: float = _VAULT_TTL / 2) -> bool
```

Start (once per process) a daemon thread that runs refresh_vault_needles() now and every
``interval`` seconds, so a long-lived writer (palace-daemon) never fetches on the write path. False
when one is already tracked (running, or told to stop and not yet exited).

### `stop_vault_refresher`

```python
def stop_vault_refresher(timeout: float = 5.0) -> bool
```

Tell the refresher to stop and wait up to ``timeout``. True when no refresher thread is left;
False when it is still inside a fetch: it stays tracked, and it keeps nothing it fetches.

### `vault_status`

```python
def vault_status() -> List[Tuple[str, Optional[float]]]
```

(label, seconds since its last good fetch, or None if it has none) per configured item.

### `needle_seed_fd`

```python
def needle_seed_fd() -> Optional[int]
```

The read end of a pipe that already holds this process's needles, for one child: pass it with
``pass_fds=(fd,)`` and ``&#123;NEEDLE_SEED_ENV: str(fd)}`` in the child's env, then close it here right
after the spawn. The write end is closed before this returns. None (spawn as before, the child
fetches) when there is nothing to hand over, off Linux, or the pipe cannot be made.

### `patterns`

```python
def patterns() -> List[Pass]
```

The compiled patterns, re-read when the config file changes (every vault value fetched again),
every ``_VAULT_TTL`` seconds while vault needles are configured (a value older than that fetched
again, unless the refresher keeps them), after the refresher brings new values, and when the oldest
kept value reaches ``_VAULT_MAX_KEEP``. A vault fetch that fails keeps that item's last good value
until then; an item with none fails closed, and so does an unreadable or invalid config.

### `redact`

```python
def redact(text: str, skip_hex: bool = False, skip_shapes: bool = False) -> str
```

``text`` with every secret-shaped run replaced by its placeholder. ``skip_hex=True`` skips
only the #619 ``hex`` shape, and is used only for a whole-hex value under a metadata key that
names a digest or an id (a content hash for dedup); every other rule still applies.
``skip_shapes=True`` skips the broad #619 shapes (token, hex, quoted) and nothing else; it is
used only for one identifier in an identity metadata field (``_redact_metadata``).

### `redact_documents`

```python
def redact_documents(documents)
```

Documents as a write passes them (a list, a single string, or None), redacted.

### `redact_metadatas`

```python
def redact_metadatas(metadatas)
```

Metadata as a write passes it (a list of dicts, one dict, or None), its string values
redacted. The caller's dicts are never changed in place.
