# Metadata Reshape Root-Cause Investigation (#32)

> **Status (2026-10-22): the aliasing root cause is fixed.** `ChromaCollection._sanitize_metadatas_for_chromadb` now returns copies of the caller's inner dicts (arrived with the upstream sync, techempower-org/mempalace#578), so a post-sanitize mutation no longer reaches chromadb. `tests/test_metadata_reshape_bug.py` was inverted to pin the fix (`test_sanitizer_inner_dicts_do_not_alias_caller_dicts`, `test_caller_clear_after_sanitize_no_longer_reaches_chromadb`).


**Investigator:** Selene (dream-celestial team)
**Date:** 2026-05-22
**Branch:** `investigate/metadata-reshape-32`
**Status:** Root cause identified; minimal fix proposed.

## TL;DR

The chokepoint sanitizer
`ChromaCollection._sanitize_metadatas_for_chromadb`
(`mempalace/backends/chroma.py:1094-1111`) returns a new outer list but
**aliases** each non-empty caller dict by reference:

```python
return [
    m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
    for m in metadatas
]
```

When the branch is taken, `m` is returned as-is — the **same object**
the caller still holds. Any in-place mutation that empties one of those
dicts between the sanitize pass and chromadb's
`validate_metadata` call slips the check entirely. The exact same
aliasing pattern appears in `repair.py:_extract_drawers`
(`mempalace/repair.py:170-173`) and `repair.py:_rebuild_one_collection`
(`mempalace/repair.py:990`).

The minimal fix is to never alias — coerce *every* slot through a fresh
dict so the list chromadb iterates is its own private structure:

```python
return [
    dict(m) if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
    for m in metadatas
]
```

This adds one shallow copy per non-empty metadata entry (negligible on a
151K rebuild — ~one Python dict allocation per drawer) and makes the
sanitizer's guarantee impossible to violate by anything downstream of
it.

## Symptom recap

Issue #32 reports a 151,478-drawer rebuild failing at ~120K with:

```
ValueError: Expected metadata to be a non-empty dict, got 0
metadata attributes in add.
```

Traceback runs through:

```
mempalace/backends/chroma.py:add → chromadb Collection.add
  → validate_insert_record_set → validate_metadatas
  → validate_metadata
```

Three sanitizer layers were already in place:

| Layer | File | Commit |
|---|---|---|
| Extract | `repair.py:_extract_drawers` | `949cb20` |
| Stream | `repair.py:_rebuild_one_collection` | `949cb20` |
| Chokepoint | `chroma.py:ChromaCollection.{add,upsert}` | `f499814` |

All three coerce `None`/`{}` to `{"_repaired_empty_meta": True}`. The
question issue #32 poses: *what reshapes the metadatas list between our
sanitizer and chromadb's validator?*

## Investigation walk-through

### 1. Map the chromadb internal flow

`Collection.add` (`chromadb 1.5.9`) →
`_validate_and_prepare_add_request` → `normalize_insert_record_set` →
`validate_insert_record_set` → `validate_metadatas` →
`validate_metadata`.

The error message format
`"Expected metadata to be a non-empty dict, got {len(metadata)} metadata
attributes"` comes from
`chromadb/api/types.py:1070-1073`. The `" in add."` suffix is appended
by `validation_context("add")` (`CollectionCommon.py:93-113`).

Key fact: `validate_metadata` accepts `None` (line 1068) but rejects
`{}` (line 1070). It does **not** mutate the input.

`_apply_sparse_embeddings_to_metadatas`
(`CollectionCommon.py:605`) only runs when the collection has a sparse
embedding target configured. Our drawers collection has no sparse
target. Even if it did, the function copies via
`[dict(metadata) if metadata is not None else {} for metadata in metadatas]`
— it doesn't mutate the input list either.

**Verdict:** chromadb does not reshape the metadatas list. Hypotheses
H1 ("chromadb upsert splits into add+update internally") and H2
("deeper preprocessing step introduces empty dicts") in the issue body
are not supported by the 1.5.9 source.

### 2. Re-examine our own sanitizer

```python
@staticmethod
def _sanitize_metadatas_for_chromadb(metadatas):
    if metadatas is None:
        return None
    return [
        m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
        for m in metadatas
    ]
```

The comprehension returns `m` when the guard passes — the **same
reference** the caller still owns. Verified empirically:

```python
metas = [{"wing": "x", "room": "y"}, {}]
sanitized = ChromaCollection._sanitize_metadatas_for_chromadb(metas)
assert sanitized is not metas              # outer list is new
assert sanitized[0] is metas[0]            # inner dicts are aliased
metas[0].clear()
assert sanitized[0] == {}                  # the alias sees the mutation
```

Now feed the post-mutation `sanitized` to chromadb's raw
`Collection.add`:

```python
raw.add(ids=["x"], documents=["d"], embeddings=[...],
        metadatas=sanitized)
```

→ raises `ValueError: Expected metadata to be a non-empty dict, got 0
metadata attributes in add.`

**This is the exact symptom from #32, reproduced in
`tests/test_metadata_reshape_bug.py::test_sanitized_dict_empty_after_caller_clear_passes_into_chromadb`.**

### 3. Who is the mutator in the 151K run?

The aliasing makes the sanitizer *susceptible* to post-sanitize mutation
— but the bug only triggers if something actually mutates. Candidates
considered:

1. **Concurrent mutation across the `mine_palace_lock` boundary.** The
   sanitize step in `ChromaCollection.upsert` runs **before**
   `with self._write_lock():`. Another writer holding the lock could in
   principle mutate a shared metadata dict during the gap. *Unlikely*
   on the rebuild path (single-threaded), but the aliasing leaves the
   door open.

2. **Caller reuses metadata dicts across batches.** A miner that
   recycles a single `metadata = {...}` dict in a loop, mutates it for
   each chunk, and appends references (rather than copies) to a batch
   list would let later mutations clear earlier batch entries. The
   current `_build_drawer` allocates fresh dicts per call, but future
   refactors could regress here.

3. **`mempalace/tags.py:apply_tags_to_metadata`** explicitly mutates
   the dict in place (`metadata.pop(TAGS_METADATA_KEY, None)` etc.).
   If invoked on a dict that's already on a queued batch list, with
   `tags=[]` and no other keys, it would empty the dict. The miner
   path always sets wing/room first so this is not currently
   reachable.

4. **HNSW segment cache reuse (most plausible at 151K scale).**
   chromadb's segment layer hands out dict objects from
   `Collection.get(...)` whose lifecycle is owned by the segment cache.
   On a multi-hour rebuild, segment-cache eviction or WAL compaction
   could in principle replace the underlying storage while our
   sanitized list still holds references. We did not isolate this in
   the synthetic reproduction (small palace doesn't trigger
   compactor), but it is consistent with the report of "fails at ~80%
   through a multi-hour run" (i.e., scale-dependent, not
   deterministic).

**The investigation is conclusive enough at the mechanism layer**: the
sanitizer's contract ("the list chromadb sees has no empty dicts") is
not enforced by the sanitizer itself once it returns. Whichever
specific mutator triggered the 151K run is then a downstream question
that the non-aliasing fix renders moot.

## Proposed minimal fix

Three identically-shaped fixes in three files; each is a one-token
change (`m` → `dict(m)`):

```diff
# mempalace/backends/chroma.py:1108-1111
     return [
-        m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
+        dict(m) if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
         for m in metadatas
     ]

# mempalace/repair.py:170-173
     sanitized_metas = [
-        m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
+        dict(m) if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
         for m in batch["metadatas"]
     ]

# mempalace/repair.py:990
-    metas.append(meta if (meta and len(meta) > 0) else {"_repaired_empty_meta": True})
+    metas.append(dict(meta) if (meta and len(meta) > 0) else {"_repaired_empty_meta": True})
```

**Cost analysis:**

- One `dict(m)` per drawer write call. Average drawer metadata is
  ~10 keys; `dict(d)` for 10-key dicts is sub-microsecond in CPython.
- 151K drawers × 1 shallow copy ≈ 0.15s amortized over a multi-hour
  rebuild. Effectively free.
- The downstream cost (chromadb serializes metadata to JSON before
  writing to SQLite anyway) far dominates.

**Safety analysis:**

- Shallow copy is sufficient: chromadb's `validate_metadata` and
  internal handling never mutate metadata values, only inspect them
  (the comprehension at `CollectionCommon.py:622` makes its own
  `dict(metadata)` copy before any modification).
- For nested values (lists, SparseVectors), the references are shared;
  but those are immutable at the validator's call site, and an
  attempt to `metadata.clear()` from outside would not empty the
  sanitized dict (it now owns its own top-level dict object).

## Architectural recommendation

After the non-aliasing fix lands and the regression test in
`tests/test_metadata_reshape_bug.py` passes, the question issue #32
raises about *which sanitizer layer to drop* can be answered cleanly:

- **`repair.py:_extract_drawers`** can be dropped. Its purpose was to
  coerce historical None/{} entries before they reached the rebuild
  loop. With a non-aliasing chokepoint sanitizer, the only thing
  `_extract_drawers` adds is one *additional* coercion that the
  chokepoint will apply again anyway.
- **`repair.py:_rebuild_one_collection`** can also be dropped — same
  rationale. The chokepoint in `ChromaCollection.upsert` is the only
  layer that actually owns the contract "no empty dict reaches
  chromadb."
- **The chokepoint sanitizer in `ChromaCollection.{add,upsert}`** is
  the right architectural layer and worth upstreaming: every code path
  that writes to chromadb naturally flows through the backend
  wrapper, and the wrapper is the only structurally-correct place to
  enforce a chromadb-input invariant.

That said, dropping the repair-layer sanitizers should be a separate
PR — the failure mode the chokepoint catches is now provably the same
one the repair layers catch, but the conservative call is to land the
non-aliasing fix first, watch a real rebuild, and only then trim
redundant defence-in-depth.

## Reproduction artifact

`tests/test_metadata_reshape_bug.py` contains nine tests:

| # | Test | Demonstrates |
|---|---|---|
| 1 | `test_chromadb_validate_metadata_rejects_empty_dict` | chromadb 1.5.x rejects `{}` |
| 2 | `test_chromadb_validate_metadata_accepts_none_entries` | …but accepts `None` in the list |
| 3 | `test_chromacollection_add_coerces_empty_dict` | the chokepoint sanitizer fires on `add` |
| 4 | `test_chromacollection_add_coerces_none_entry` | …also for `None` entries |
| 5 | `test_chromacollection_upsert_coerces_empty_dict` | …and for `upsert` |
| 6 | `test_sanitizer_inner_dicts_alias_caller_dicts` | **root-cause mechanism: aliasing** |
| 7 | `test_sanitized_dict_empty_after_caller_clear_passes_into_chromadb` | **exploitation: post-sanitize mutation → ValueError** |
| 8 | `test_extract_drawers_sanitizes_none_and_empty` | the repair extract layer also sanitizes |
| 9 | `test_raw_chromadb_add_with_empty_dict_raises` | upstream behaviour without our sanitizer |

Tests 6 and 7 are the regression contract for the proposed fix: after
the non-aliasing change, test 6 will need its assertions inverted (the
inner dict should *not* alias) and test 7 will need to change from
"raises ValueError" to "succeeds" (because the sanitizer's snapshot of
the dict survives the post-sanitize clear).

## Related

- `f499814` — chokepoint sanitizer commit (this investigation's
  starting point).
- `949cb20` — `_extract_drawers` / `_rebuild_one_collection`
  sanitizers (the upstream half of the same defence-in-depth chain).
- jphein/mempalace#28 — fork PR carrying the repair-layer sanitizer.
- MemPalace/mempalace#1458 — upstream context for the repair-layer
  sanitizers.
