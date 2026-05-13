# pgvector + AGE cutover runbook (operator-driven)

This runbook is for **you, the operator** — `mempalace migrate-to-postgres` does the heavy lifting, but the snapshot/stand-up/cutover decisions are operational and need a human at the wheel.

Spec: [`docs/superpowers/specs/2026-05-10-pgvector-age-migration-design.md`](../superpowers/specs/2026-05-10-pgvector-age-migration-design.md)
Plan: [`docs/superpowers/plans/2026-05-10-pgvector-age-migration-impl.md`](../superpowers/plans/2026-05-10-pgvector-age-migration-impl.md)

---

## Phase 4.1 — Dry-run on canonical 160K palace

### 1. Snapshot the canonical palace (instant, hardlinks)

```bash
ssh disks 'cp -al /mnt/raid/projects/mempalace-data/palace \
                  /mnt/raid/projects/mempalace-data/palace.dry-run-$(date +%Y-%m-%d)'
```

`cp -al` is an O(N) hardlink copy — no extra disk usage unless files diverge. Confirms in ~10s for 160K drawers.

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
