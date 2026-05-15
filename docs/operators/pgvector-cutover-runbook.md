# pgvector + AGE cutover runbook (operator-driven)

This runbook is for **you, the operator** — `mempalace migrate-to-postgres` does the heavy lifting, but the snapshot/stand-up/cutover decisions are operational and need a human at the wheel.

Spec: [`docs/superpowers/specs/2026-05-10-pgvector-age-migration-design.md`](../superpowers/specs/2026-05-10-pgvector-age-migration-design.md)
Plan: [`docs/superpowers/plans/2026-05-10-pgvector-age-migration-impl.md`](../superpowers/plans/2026-05-10-pgvector-age-migration-impl.md)

---

## Phase 4.1 — Dry-run on canonical 160K palace

### 1. Snapshot the canonical palace

**If you're stopping the daemon** (cleanest):
```bash
ssh disks 'sudo systemctl stop palace-daemon && \
           cp -al /mnt/raid/projects/mempalace-data/palace \
                  /mnt/raid/projects/mempalace-data/palace.dry-run-$(date +%Y-%m-%d)'
```
`cp -al` is O(N) hardlinks — instant.

**If you're leaving the daemon running** (use `cp -a`, NOT `cp -al`):
```bash
ssh disks 'cp -a /mnt/raid/projects/mempalace-data/palace \
                 /mnt/raid/projects/mempalace-data/palace.dry-run-$(date +%Y-%m-%d)'
```
Real copy (~2 min for 8 GB on local SSD). Hardlinks share inodes with the live palace — ChromaDB 1.5.x's concurrent-writer SIGSEGV will trip when the daemon's client and the migration's client touch the same HNSW files.

### 1b. (Likely required) Repair the snapshot before migrating

ChromaDB 1.5.x SIGSEGVs on raw `PersistentClient(path=...)` opens of long-lived palaces with stale HNSW segments or legacy index_metadata files. The daemon serves the live palace fine because it caches client state in-memory; fresh opens crash. The migration tool calls `ChromaBackend._prepare_palace_for_open` before opening, which quarantines invalid metadata + stale segments — but on a heavily-evolved palace that still isn't enough.

Symptom: faulthandler trace ends in `chromadb/api/rust.py:440 in _get` returning SIGSEGV / exit 139 even for metadata-only `col.get(limit=5, include=["metadatas"])`. Related upstream: chroma-core/chroma#6949.

Workaround: rebuild HNSW from sqlite (which has all the drawer data intact):

```bash
ssh disks '/mnt/raid/projects/mempalace-dryrun-venv/bin/mempalace \
    --palace /mnt/raid/projects/mempalace-data/palace.dry-run-$(date +%Y-%m-%d) \
    repair --mode from-sqlite --archive-existing --yes'
```

This moves the broken snapshot to `palace.dry-run-<date>.pre-rebuild-<timestamp>` and constructs a fresh palace at the original path with the sqlite data re-vectored into clean HNSW segments. Expect 30–60 min for ~270K embeddings.

### 2. Stand up Postgres on disks

Pragmatic option:

```bash
ssh disks 'docker run -d --name mempalace-dryrun-pg \
    -e POSTGRES_PASSWORD=palace \
    -e POSTGRES_DB=mempalace_dryrun \
    -p 5433:5432 \
    pgvector/pgvector:pg16'
```

`pgvector/pgvector:pg16` ships pgvector but NOT AGE. For full migration:

```bash
ssh disks 'docker run -d --name mempalace-dryrun-pg \
    -e POSTGRES_PASSWORD=palace \
    -e POSTGRES_DB=mempalace_dryrun \
    -p 5433:5432 \
    apache/age:release_PG16_1.6.0'
```

Then install pgvector inside the AGE container:

```bash
ssh disks 'docker exec mempalace-dryrun-pg \
    apt-get update && apt-get install -y postgresql-16-pgvector'
```

(Custom image with both baked in is the cleaner long-term path; this is enough for the dry-run.)

### 3. Stop palace-daemon (the migration refuses to run while it's responsive)

```bash
ssh disks 'sudo systemctl stop palace-daemon'
```

### 4. Run the migration

```bash
ssh disks '/home/jp/.local/share/palace-daemon/venv/bin/mempalace \
    migrate-to-postgres \
    --from /mnt/raid/projects/mempalace-data/palace.dry-run-$(date +%Y-%m-%d) \
    --to "postgresql://postgres:palace@localhost:5433/mempalace_dryrun" \
    --batch-size 1000 \
    2>&1 | tee /tmp/migrate-$(date +%Y-%m-%d).log'
```

What you'll see in the log:

- `[phase 0] preflight passed`
- `[phase 1] schema created`
- `[phase 2] copying N drawers from collection 'mempalace_drawers'` → `[phase 2]   1000/N copied` ... `[phase 2] drawers complete`
- `[phase 5] migrating N triples` → `[phase 5] kg complete — N copied, M skipped`
- `[phase 6] verifying parity (sample=10)` → final `verify OK` or `MISMATCH`
- `Migration complete.` block with cutover instructions IF verify passed

Record total duration per phase. Phase 2 is the long pole (drawer batch copy through the postgres backend); expect roughly 1ms/drawer on local Postgres, so ~3 min for 160K drawers.

### 5. Smoke-test the dry-run Postgres palace

```bash
ssh disks 'MEMPALACE_BACKEND=postgres \
    MEMPALACE_POSTGRES_DSN="postgresql://postgres:palace@localhost:5433/mempalace_dryrun" \
    MEMPALACE_KG_BACKEND=age \
    /home/jp/.local/share/palace-daemon/venv/bin/mempalace search "palace taxonomy"'
```

Pick 3–5 queries you know return distinctive content in the ChromaDB original. Compare result sets. If a top-1 result differs, that's worth investigating before cutover.

### 6. Document findings

Append timings + any surprises to the spec's "Performance budget" section. If anything failed verify, note it in [`docs/superpowers/plans/2026-05-10-pgvector-age-migration-impl.md`](../superpowers/plans/2026-05-10-pgvector-age-migration-impl.md) as a follow-up before production cutover.

---

## Phase 4.2 — Production cutover

The migration tool's phase 7 output prints the exact steps. Reproduced here for reference:

### 1. Confirm dry-run was clean (no `MISMATCH` in the log)

### 2. Snapshot the production palace one more time

```bash
ssh disks 'cp -al /mnt/raid/projects/mempalace-data/palace \
                  /mnt/raid/projects/mempalace-data/palace.pre-cutover-$(date +%Y-%m-%d)'
```

### 3. Stand up a production Postgres (not the dry-run container)

Decision point: same container with a different DB name, or a separate system Postgres service. Long-running production likely wants the latter so the migration container can be retired.

### 4. Stop palace-daemon

```bash
ssh disks 'sudo systemctl stop palace-daemon'
```

### 5. Run the migration against production

Same command as Phase 4.1 Step 4, but pointing at the canonical palace and the production Postgres.

### 6. Update the daemon's EnvironmentFile

`/home/jp/.config/palace-daemon/env` (per the systemd unit's `EnvironmentFile=` line). Add:

```
MEMPALACE_BACKEND=postgres
MEMPALACE_POSTGRES_DSN=postgresql://postgres:<prod-pass>@localhost:5432/mempalace
MEMPALACE_KG_BACKEND=age
```

### 7. Reload + start

```bash
ssh disks 'sudo systemctl daemon-reload && sudo systemctl start palace-daemon'
```

### 8. Smoke

```bash
curl http://disks:8085/health
curl -H "X-API-Key: $PALACE_API_KEY" 'http://disks:8085/search?q=palace+taxonomy'
```

### 9. Watch hook activity for an hour

```bash
ssh disks 'journalctl -u palace-daemon -f'
```

Confirm Stop hooks, transcript ingests, diary writes all succeed. Time them — postgres should match or beat ChromaDB.

### 10. After 24h of clean operation, archive the chromadb backup

```bash
ssh disks 'mv /mnt/raid/projects/mempalace-data/palace \
              /mnt/raid/projects/mempalace-data/palace.chromadb-backup-$(date +%Y-%m-%d)'
```

The borg backup on disks picks the directory up by name; the rename means the next backup snapshots BOTH the new postgres palace AND the chromadb backup until you decide to drop the latter.

---

## Rollback (if cutover goes wrong)

1. `ssh disks 'sudo systemctl stop palace-daemon'`
2. Remove the three `MEMPALACE_*` env additions from the daemon's `EnvironmentFile`
3. `ssh disks 'sudo systemctl daemon-reload && sudo systemctl start palace-daemon'`
4. The daemon resumes against the ChromaDB palace as if nothing happened.

The Postgres palace stays around; you can re-attempt or investigate.

---

## What the real 2026-05-13/14 cutover actually hit (post-mortem)

The runbook above is the theoretical happy path. The real cutover
discovered six gotchas that the spec/plan didn't predict. None were
catastrophic, but all are worth knowing before the next operator runs
this on a fresh palace.

### 1. chromadb open SIGSEGVs on long-lived palaces — even with repair

The runbook's Step 1b suggests `mempalace repair --mode from-sqlite` to
rebuild HNSW from sqlite before migration. On disks's 270k-drawer
palace, that repair script estimated **7+ hours** of wall time (disks's
2011 i5 maxes at ~6 vectors/sec for ONNX embedding). Migration timed
out before it finished.

**Workaround used:** out-of-band substrate-portable pipeline, bypassing
chromadb entirely. Lives at `~/palace-snapshot/`:
- `extract_drawers.py` reads raw `chroma.sqlite3` directly (no client open)
- `embed.py` runs sentence-transformers on katana's 2080 Ti (~700 vec/s; finished in 6m22s)
- `load_pgvector.py` bulk-loads into postgres via `INSERT … FROM unnest()`
- `load_age_kg.py` reads `knowledge_graph.sqlite3` triples and replays via AGE Cypher

Total: ~14 min wall. Tracked as fork-roadmap [#70](https://github.com/techempower-org/mempalace/issues/70) — rewriting
`migrate_to_postgres.phase_2_drawers` to read raw sqlite would close
the gap.

### 2. migrate_to_postgres.py used pre-#995 API

`backend.get_or_create_collection(name)` calls scattered through
phase_2_drawers + phase_6_verify. Pre-RFC-001 API. After #995 landed
upstream, the canonical signature is
`backend.get_collection(*, palace=PalaceRef(...), collection_name=name, create=True)`.
Fixed in commit 2f40880.

### 3. psycopg2-binary missing in daemon venv

The daemon's pre-cutover install didn't include the postgres extras.
Setting `MEMPALACE_BACKEND=postgres` + restart returned a "degraded"
health status until we ran `pip install psycopg2-binary` in the
daemon's venv. Tracked at jphein/palace-daemon#15 (fork issue, also
fixed in palace-daemon's pyproject extras).

### 4. mempalace's `_get_collection` was chromadb-only

The MCP server's `_get_collection` (which `mempalace_search` /
`mempalace_add_drawer` route through) hardcoded chromadb path. Setting
`MEMPALACE_BACKEND=postgres` had no effect on reads — daemon was on
postgres for writes but reading from chromadb. Fixed via backend
routing in commit b6b1740: `_get_collection` now branches on
`_config.backend` and calls `_get_collection_postgres` for the new path.

### 5. pgvector lazy-index race wedges the database

`PostgresBackend._maybe_create_vector_index` had a SELECT-then-CREATE
race + name-coupled existence check. Three concurrent writers crossing
`VECTOR_INDEX_CHECK_INTERVAL_ROWS` simultaneously each issued their
own `CREATE INDEX` with no advisory lock or `IF NOT EXISTS`. All three
took `ACCESS EXCLUSIVE` on `mempalace_drawers`, blocking every other
write for ~30 minutes.

**Fix landed locally** as commit 4566f8a:
`pg_advisory_xact_lock(hashtext('vec_idx:<table>'))` around the
check+create, plus `IF NOT EXISTS` belt-and-suspenders. Operator
comment posted on upstream MemPalace/mempalace#665. Tracked at
[#73](https://github.com/techempower-org/mempalace/issues/73).

**Operational recovery** (in case it happens again):
1. `psql ... -c "SELECT pid FROM pg_stat_activity WHERE state='active' AND query ILIKE '%CREATE INDEX%mempalace_drawers%'"` to find the racing backends
2. `pg_cancel_backend(pid)` for the duplicates (keep one — check `pg_stat_progress_create_index` for the actual builder)
3. After it finishes, drop any leftover name-mismatched index, rename the survivor to the expected `mempalace_drawers_vec_idx`.

### 6. load_pgvector.py wrote wing/room into metadata jsonb, not top-level columns

`PostgresBackend._create_table` expected wing/room as top-level NOT
NULL columns. The bundle's `load_pgvector.py` shoved them into the
metadata jsonb. Daemon `/search` errored with `column "wing" does not
exist` until we ran `ALTER TABLE … ADD COLUMN wing/room` + `UPDATE FROM
metadata->>'wing'/'room'` + `CREATE INDEX`.

Now structurally fixed: commit 5f9d087's `_create_table` emits
wing/room top-level from the start, and the doc_tsv generated column
+ GIN + trgm GIN come with it. Fresh-migration runs won't repeat the
mismatch. Tracked at [#71](https://github.com/techempower-org/mempalace/issues/71) (closed).

### 7. (bonus) systemd file changes hidden by syncthing mirror

Daemon's systemd `WorkingDirectory` is `/mnt/raid/projects/palace-daemon/`,
but I'd been scp-ing my hand-patches to `/home/jp/.local/share/palace-daemon/`
trusting syncthing to mirror. It does, eventually, but a fresh disks
reboot could read either side first. Resolved by writing a proper
deploy script (palace-daemon `b303b29`) that rsyncs directly to the
canonical `/mnt/raid/projects/palace-daemon/`.

---

## Recommended fresh-migration command (post-2026-05-14 fixes)

After the above commits land, the canonical happy path is:

```bash
ssh disks 'sudo systemctl stop palace-daemon'
ssh disks '/home/jp/.local/share/palace-daemon/venv/bin/mempalace \
    migrate-to-postgres \
    --from /mnt/raid/projects/mempalace-data/palace \
    --to "postgresql://postgres:<pass>@localhost:5432/mempalace" \
    --batch-size 1000'
# Phase 1 schema now includes pg_trgm, doc_tsv generated column,
# GIN/HNSW/trigram indexes — no manual ALTERs needed.
# Phase 2 still uses chromadb.PersistentClient (#70 not yet done);
# if your palace is >150K drawers and >2 months old, expect SIGSEGV
# and fall back to the substrate-portable pipeline above.
```

For palaces large enough that step 2 SIGSEGVs, run `~/palace-snapshot/`
pipeline instead — until #70 lands, that's the canonical big-palace
path.
