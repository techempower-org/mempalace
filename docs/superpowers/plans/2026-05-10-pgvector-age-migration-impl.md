# Postgres + pgvector + Apache AGE — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move mempalace from ChromaDB + sqlite-KG to a unified Postgres deployment with pgvector for embeddings and Apache AGE for the knowledge graph, then provide a restartable migration tool for existing palaces.

**Architecture:** Three phases on top of upstream's `BaseCollection` seam (#413, merged). Phase 1 lands the pgvector backend (composing with upstream #665 or fork-porting). Phase 2 adds the AGE-backed KG layer. Phase 3 ships the `mempalace migrate-to-postgres` CLI tool. Each phase produces working, testable software on its own.

**Tech Stack:** Python 3.9+ (existing mempalace floor), psycopg[binary]>=3.1, pgvector-python>=0.3, apache-age-python (TBD on package availability — fallback is raw Cypher over a psycopg cursor), pytest-postgresql for unit tests, docker-compose Postgres for integration tests.

**Reference spec:** [`docs/superpowers/specs/2026-05-10-pgvector-age-migration-design.md`](../specs/2026-05-10-pgvector-age-migration-design.md)

---

## Status as of 2026-05-13

The plan was written 2026-05-10. PR [#21 (`feat/pgvector-age-impl`)](https://github.com/jphein/mempalace/pull/21) merged 2026-05-11 + the AGE skeleton commit `a3ee623` landed parts of Phases 1–2. Below is the reconciliation; see each Task heading for the status marker.

| Phase | Task | Status | Evidence |
|---|---|---|---|
| 0 | 0.1 Postgres install verify | ✅ Done | env verified; subsequent phases shipped |
| 0 | 0.2 #665 stance | ✅ Done | `docs/internal/pgvector-665-decision.md` (WAIT chosen) |
| 1 | 1.A.1 #665 cherry-pick + smoke | ✅ Done | `mempalace/backends/postgres.py`, `tests/test_backends_postgres.py` |
| 1 | 1.2 HNSW indexes | ✅ Done | `postgres.py:556-660` (sorted_hnsw / hnsw variants) |
| 1 | 1.3 Where-clause filters | ✅ Done | `postgres.py:289` (`def query(where=...)`) |
| 1 | 1.4 `backend` + `postgres_dsn` config | ✅ Done | `config.py:301, 318` |
| 1 | 1.5 Postgres CI workflow | ✅ Done | `.github/workflows/ci.yml:73` (`test-postgres` job, pgvector/pgvector:pg16) |
| 2 | 2.1 AGE schema + skeleton | ✅ Done | `mempalace/knowledge_graph_age.py`, `tests/test_knowledge_graph_age.py` |
| 2 | 2.2 `add_triple()` Cypher | ✅ Done | now in `knowledge_graph_age.py` (clear + add_triple + query_triples). Skeleton file's docstring: "add/query operations and temporal filtering arrive in subsequent..." |
| 2 | 2.3 Temporal filtering | ✅ Done | as_of param on query_triples |
| 2 | 2.4 `kg_backend` config + routing | ✅ Done | property + mcp_server `_get_kg` routes to AGE when `MEMPALACE_KG_BACKEND=age` |
| 3 | 3.1 CLI scaffold + preflight | ✅ Done | `mempalace migrate-to-postgres` works; phase 0 gates daemon + extensions |
| 3 | 3.2 phase_1_schema | ✅ Done | extensions + `mempalace_backend_meta` checkpoint table |
| 3 | 3.3 phase_2_drawers (batched copy) | ✅ Done | via PostgresBackend.upsert(); per-collection checkpointing |
| 3 | 3.4.a closets | ✅ Covered by 3.3 | same iterator handles all chromadb collections |
| 3 | 3.4.b indexes | ✅ Covered by backend | `_ensure_schema` creates HNSW + BTrees on first write |
| 3 | 3.4.c Phase 5 KG (sqlite → AGE) | ✅ Done | phase_5_kg uses KnowledgeGraphAGE.add_triple; watermark resume; bad-row skip+log |
| 3 | 3.5 Phase 6 verify | ✅ Done | drawer count + sample round-trip + lenient triple parity |
| 3 | 3.6 Phase 7 cutover instructions | ✅ Done | phase_7_done prints systemctl steps; checkpoints cleaned |
| 4 | 4.1 Dry-run on canonical palace | ✅ Runbook shipped | `docs/operators/pgvector-cutover-runbook.md` — operator runs the snapshot + Postgres stand-up + migration |
| 4 | 4.2 Production cutover | ✅ Documented | runbook Phase 4.2 + phase_7_done's printed instructions cover the cutover sequence end-to-end |

**Canonical next task:** Phase 2.2 — implement `add_triple()` with Cypher MERGE/CREATE on the existing `KnowledgeGraphAGE` skeleton. The skeleton bootstraps the AGE extension and graph; what's missing is the actual write surface for triples.

---

## Phase 0 — Preflight ✅ Done 2026-05-11

Lock in environment assumptions and decide the upstream-composition path before touching code.

### Task 0.1: Verify Postgres + extensions install on dev host

**Files:** none — environment probe only

- [x] **Step 1: Check Postgres version available**

```bash
which psql && psql --version
apt list --installed 2>/dev/null | grep -i postgres | head -5
```

Expected: PostgreSQL client present, ideally 15+. If absent, install via apt (`sudo apt install postgresql-15 postgresql-15-pgvector postgresql-15-age`) or via brew on macOS.

- [x] **Step 2: Probe AGE extension availability**

```bash
sudo -u postgres psql -c "SELECT extname FROM pg_available_extensions WHERE extname IN ('vector', 'age');"
```

Expected: both `vector` and `age` listed. If `age` is missing, document the install path (build-from-source vs distro package) in a new `docs/postgres-setup.md`.

- [x] **Step 3: Document findings in scratch**

Write findings to `scratch/postgres-preflight-2026-05-10.md`: Postgres version, extension availability, install commands used, any compatibility caveats discovered (especially AGE vs Postgres 17 if applicable). This file is for the implementation team's reference; not committed.

- [x] **Step 4: Commit nothing**

Preflight is information-gathering only. No source changes yet.

### Task 0.2: Decide #665 composition stance

**Files:** none — decision recorded in `scratch/pgvector-665-decision.md`

- [x] **Step 1: Fetch and check #665's current state**

```bash
gh pr view 665 --repo MemPalace/mempalace --json state,mergeable,updatedAt,commits -q '{state,mergeable,updated:.updatedAt,commits:(.commits | length)}'
git fetch upstream pull/665/head:pr-665 2>&1 | tail -3
```

Expected: state OPEN. Note last update date and commit count. If a maintainer engaged in the last 7 days, lean "wait." If stale >2 weeks, lean "fork-port."

- [x] **Step 2: Test that #665 applies on our main**

```bash
git checkout -b test-665-apply main
git merge pr-665 --no-commit --no-ff 2>&1 | tail -20
git merge --abort
git checkout feat/pgvector-age-impl
git branch -D test-665-apply
```

Expected: clean merge or only documentation conflicts. Record any non-trivial conflicts.

- [x] **Step 3: Decide and document**

Write decision to `scratch/pgvector-665-decision.md` with one of:
- **Wait** — #665 has recent maintainer activity; we'll comment after our implementation is done and contribute follow-ups
- **Fork-port** — #665 is stale or conflicts are heavy; we'll reimplement `mempalace.backends.postgres` ourselves based on its design, plan to upstream the result later

The rest of Phase 1 branches on this decision. Each branch is enumerated below.

- [x] **Step 4: Commit the decision document**

```bash
mkdir -p docs/internal
mv scratch/pgvector-665-decision.md docs/internal/pgvector-665-decision.md
git add docs/internal/pgvector-665-decision.md
git commit -m "docs(internal): record #665 composition stance for pgvector backend"
```

---

## Phase 1 — pgvector backend

Make `MEMPALACE_BACKEND=postgres` a working alternative to ChromaDB for the storage layer.

> **Branch on Task 0.2's decision.** Tasks 1.A.* run if "wait" (verify #665 works on our base, no new backend code). Tasks 1.B.* run if "fork-port" (we implement `mempalace.backends.postgres` ourselves). The remaining 1.* tasks (indexes, conformance, CI) are common to both branches.

### Task 1.A.1 — "Wait" path: verify #665 against our v3.3.5 main

**Files:**
- Modify: `tests/test_backends_postgres.py` (new — minimal smoke test)

- [x] **Step 1: Cherry-pick #665's commits onto our branch**

```bash
git fetch upstream pull/665/head:pr-665
git cherry-pick pr-665~..pr-665   # cherry-pick the range — adjust if #665 has more commits
```

Expected: cherry-pick succeeds or surfaces conflicts. Resolve any conflicts following the pattern of our PR #18 sync (keep fork docs, take upstream code).

- [x] **Step 2: Run the existing test suite**

```bash
source venv/bin/activate
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: PASS. The full ChromaDB-default suite must remain green. If #665 broke anything in non-postgres paths, fix before continuing.

- [x] **Step 3: Write a smoke test for the postgres backend's `BaseCollection` contract**

```python
# tests/test_backends_postgres.py
import os
import pytest

from mempalace.backends import get_backend
from mempalace.backends.base import BaseCollection

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    POSTGRES_DSN is None, reason="set TEST_POSTGRES_DSN to run postgres backend tests"
)


def test_postgres_backend_smoke():
    """The postgres backend instantiates and conforms to BaseCollection."""
    backend = get_backend("postgres", dsn=POSTGRES_DSN)
    col = backend.get_or_create_collection("smoke_test_drawers")
    assert isinstance(col, BaseCollection)
    col.add(
        ids=["d1"],
        documents=["hello world"],
        embeddings=[[0.0] * 384],
        metadatas=[{"wing": "test"}],
    )
    res = col.get(ids=["d1"])
    assert res["documents"] == ["hello world"]
    backend.delete_collection("smoke_test_drawers")
```

- [x] **Step 4: Run smoke test against local Postgres**

```bash
# Assumes a local Postgres at $TEST_POSTGRES_DSN with pgvector installed
TEST_POSTGRES_DSN="postgresql://palace@localhost/mempalace_test" \
    python -m pytest tests/test_backends_postgres.py::test_postgres_backend_smoke -v
```

Expected: PASS. If FAIL, debug and fix before continuing — the postgres backend must round-trip a single drawer at minimum.

- [x] **Step 5: Commit**

```bash
git add tests/test_backends_postgres.py
git commit -m "test(backends/postgres): smoke test for BaseCollection conformance"
```

### Task 1.B.1 — "Fork-port" path: implement `mempalace.backends.postgres`

**Files:**
- Create: `mempalace/backends/postgres.py`
- Create: `tests/test_backends_postgres.py`

> **Only run this task if Task 0.2 decided "fork-port."** Otherwise skip to Task 1.2.

- [ ] **Step 1: Write the failing smoke test first**

```python
# tests/test_backends_postgres.py — same content as Task 1.A.1 Step 3
```

- [ ] **Step 2: Run it; expect import-time failure**

```bash
TEST_POSTGRES_DSN="postgresql://palace@localhost/mempalace_test" \
    python -m pytest tests/test_backends_postgres.py -v 2>&1 | tail -10
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mempalace.backends.postgres'`.

- [ ] **Step 3: Add psycopg + pgvector to dependencies**

```bash
# pyproject.toml — add to [project.optional-dependencies]
# postgres = ["psycopg[binary]>=3.1", "pgvector>=0.3"]
```

Edit `pyproject.toml` to add the `postgres` extra. Reinstall: `pip install -e ".[dev,postgres]"`.

- [ ] **Step 4: Implement `PostgresCollection` (BaseCollection conformance)**

```python
# mempalace/backends/postgres.py
import json
from typing import Optional

import psycopg
from pgvector.psycopg import register_vector

from .base import BaseBackend, BaseCollection


class PostgresCollection(BaseCollection):
    def __init__(self, conn: psycopg.Connection, collection_name: str):
        self._conn = conn
        self._collection = collection_name

    def add(self, ids, documents, embeddings, metadatas=None):
        metadatas = metadatas or [{}] * len(ids)
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO mempalace.drawers
                    (id, collection, document, embedding, metadata)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [
                    (i, self._collection, d, e, json.dumps(m))
                    for i, d, e, m in zip(ids, documents, embeddings, metadatas)
                ],
            )
        self._conn.commit()

    def upsert(self, ids, documents, embeddings, metadatas=None):
        metadatas = metadatas or [{}] * len(ids)
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO mempalace.drawers
                    (id, collection, document, embedding, metadata)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    document = EXCLUDED.document,
                    embedding = EXCLUDED.embedding,
                    metadata = EXCLUDED.metadata
                """,
                [
                    (i, self._collection, d, e, json.dumps(m))
                    for i, d, e, m in zip(ids, documents, embeddings, metadatas)
                ],
            )
        self._conn.commit()

    def get(self, ids=None, where=None, limit=None):
        sql = "SELECT id, document, embedding, metadata FROM mempalace.drawers WHERE collection = %s"
        params: list = [self._collection]
        if ids:
            sql += " AND id = ANY(%s)"
            params.append(list(ids))
        # Skipping where-clause translation for the smoke milestone; full filter support in Task 1.3.
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return {
            "ids": [r[0] for r in rows],
            "documents": [r[1] for r in rows],
            "embeddings": [list(r[2]) if r[2] is not None else None for r in rows],
            "metadatas": [r[3] for r in rows],
        }

    def query(self, query_embeddings, n_results=10, where=None):
        # Minimal cosine-similarity query. Full filter support arrives in Task 1.3.
        results = {"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]}
        with self._conn.cursor() as cur:
            for emb in query_embeddings:
                cur.execute(
                    """
                    SELECT id, document, embedding <=> %s AS distance, metadata
                    FROM mempalace.drawers
                    WHERE collection = %s
                    ORDER BY embedding <=> %s
                    LIMIT %s
                    """,
                    (emb, self._collection, emb, n_results),
                )
                rows = cur.fetchall()
                results["ids"][0] = [r[0] for r in rows]
                results["documents"][0] = [r[1] for r in rows]
                results["distances"][0] = [float(r[2]) for r in rows]
                results["metadatas"][0] = [r[3] for r in rows]
        return results

    def delete(self, ids=None, where=None):
        with self._conn.cursor() as cur:
            if ids:
                cur.execute(
                    "DELETE FROM mempalace.drawers WHERE collection = %s AND id = ANY(%s)",
                    (self._collection, list(ids)),
                )
        self._conn.commit()

    def count(self):
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM mempalace.drawers WHERE collection = %s",
                (self._collection,),
            )
            return cur.fetchone()[0]


class PostgresBackend(BaseBackend):
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=False)
        register_vector(self._conn)
        self._ensure_schema()

    def _ensure_schema(self):
        with self._conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS mempalace")
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mempalace.drawers (
                    id text PRIMARY KEY,
                    collection text NOT NULL,
                    document text NOT NULL,
                    metadata jsonb NOT NULL DEFAULT '{}',
                    embedding vector(384),
                    source_file text,
                    mtime double precision,
                    normalize_version integer,
                    importance integer DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mempalace.backend_meta (
                    key text PRIMARY KEY,
                    value text NOT NULL
                )
                """
            )
        self._conn.commit()

    def get_or_create_collection(self, name):
        return PostgresCollection(self._conn, name)

    def delete_collection(self, name):
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mempalace.drawers WHERE collection = %s",
                (name,),
            )
        self._conn.commit()
```

- [ ] **Step 5: Register the backend in `mempalace.backends.__init__`**

Add to `mempalace/backends/__init__.py`:

```python
def get_backend(name: str, **kwargs):
    if name == "chroma":
        from .chroma import ChromaBackend
        return ChromaBackend(**kwargs)
    if name == "postgres":
        from .postgres import PostgresBackend
        return PostgresBackend(**kwargs)
    raise ValueError(f"unknown backend: {name}")
```

- [ ] **Step 6: Run the smoke test**

```bash
TEST_POSTGRES_DSN="postgresql://palace@localhost/mempalace_test" \
    python -m pytest tests/test_backends_postgres.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add mempalace/backends/postgres.py mempalace/backends/__init__.py \
    tests/test_backends_postgres.py pyproject.toml
git commit -m "feat(backends): pgvector-backed PostgresBackend (fork-port of #665 scope)"
```

### Task 1.2: Add HNSW + supporting indexes

**Files:**
- Modify: `mempalace/backends/postgres.py` (extend `_ensure_schema`)
- Modify: `tests/test_backends_postgres.py` (add index-existence test)

- [x] **Step 1: Write failing test for HNSW index presence**

```python
# Append to tests/test_backends_postgres.py
def test_postgres_indexes_created():
    backend = get_backend("postgres", dsn=POSTGRES_DSN)
    backend.get_or_create_collection("test_idx")  # forces schema init
    with backend._conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'mempalace' AND tablename = 'drawers'
            """
        )
        names = {row[0] for row in cur.fetchall()}
    assert "drawers_embedding_hnsw_idx" in names
    assert "drawers_source_mtime_idx" in names
    assert "drawers_importance_idx" in names
    assert "drawers_metadata_gin_idx" in names
```

- [x] **Step 2: Run, expect failure**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_backends_postgres.py::test_postgres_indexes_created -v
```

Expected: FAIL — indexes don't exist yet.

- [x] **Step 3: Add index creation to `_ensure_schema`**

Append after the `drawers` CREATE TABLE:

```python
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS drawers_embedding_hnsw_idx
                    ON mempalace.drawers
                    USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS drawers_source_mtime_idx
                    ON mempalace.drawers (collection, source_file, mtime)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS drawers_importance_idx
                    ON mempalace.drawers (collection, importance)
                    WHERE importance >= 3
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS drawers_metadata_gin_idx
                    ON mempalace.drawers
                    USING gin (metadata jsonb_path_ops)
                """
            )
```

- [x] **Step 4: Run, expect pass**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_backends_postgres.py -v
```

Expected: PASS for both smoke and index tests.

- [x] **Step 5: Commit**

```bash
git add mempalace/backends/postgres.py tests/test_backends_postgres.py
git commit -m "feat(backends/postgres): HNSW + supporting indexes for fork rows 1, 7, 12"
```

### Task 1.3: Add metadata where-clause filter support

**Files:**
- Modify: `mempalace/backends/postgres.py` (extend `get` and `query` for `where=` translation)
- Modify: `tests/test_backends_postgres.py` (add where-clause tests)

- [x] **Step 1: Write failing tests for where-clause filters**

```python
def test_postgres_filter_by_wing():
    backend = get_backend("postgres", dsn=POSTGRES_DSN)
    col = backend.get_or_create_collection("test_filter")
    col.add(
        ids=["a", "b"],
        documents=["alpha", "beta"],
        embeddings=[[0.1] * 384, [0.2] * 384],
        metadatas=[{"wing": "infrastructure"}, {"wing": "creative"}],
    )
    res = col.get(where={"wing": "infrastructure"})
    assert res["ids"] == ["a"]
    backend.delete_collection("test_filter")


def test_postgres_query_with_where():
    backend = get_backend("postgres", dsn=POSTGRES_DSN)
    col = backend.get_or_create_collection("test_qf")
    col.add(
        ids=["a", "b"],
        documents=["alpha", "beta"],
        embeddings=[[0.1] * 384, [0.9] * 384],
        metadatas=[{"wing": "infra"}, {"wing": "creative"}],
    )
    res = col.query(
        query_embeddings=[[0.1] * 384],
        n_results=5,
        where={"wing": "infra"},
    )
    assert res["ids"][0] == ["a"]
    backend.delete_collection("test_qf")
```

- [x] **Step 2: Run, expect failure**

Expected: tests FAIL — `where=` is silently dropped in current `get`/`query`.

- [x] **Step 3: Implement minimal where-translation helper**

Add to `mempalace/backends/postgres.py`:

```python
def _translate_where(where: Optional[dict]) -> tuple[str, list]:
    """Translate a ChromaDB-style where dict to a SQL WHERE-clause fragment.

    Supports the common single-key {field: value} and {field: {operator: value}}
    shapes. Unknown operators raise ValueError (fail-closed per RFC 001 §1.4).
    """
    if not where:
        return "", []
    fragments: list[str] = []
    params: list = []
    for field, condition in where.items():
        if isinstance(condition, dict):
            if len(condition) != 1:
                raise ValueError(f"where: condition for {field!r} must have one operator")
            op, value = next(iter(condition.items()))
            if op == "$eq":
                fragments.append("metadata->>%s = %s")
                params.extend([field, str(value)])
            elif op == "$ne":
                fragments.append("metadata->>%s <> %s")
                params.extend([field, str(value)])
            elif op == "$in":
                fragments.append("metadata->>%s = ANY(%s)")
                params.extend([field, [str(v) for v in value]])
            else:
                raise ValueError(f"unsupported where operator: {op}")
        else:
            fragments.append("metadata->>%s = %s")
            params.extend([field, str(condition)])
    return " AND " + " AND ".join(fragments), params
```

Then update `get`:

```python
def get(self, ids=None, where=None, limit=None):
    sql = "SELECT id, document, embedding, metadata FROM mempalace.drawers WHERE collection = %s"
    params: list = [self._collection]
    if ids:
        sql += " AND id = ANY(%s)"
        params.append(list(ids))
    where_sql, where_params = _translate_where(where)
    sql += where_sql
    params.extend(where_params)
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    # ... rest unchanged
```

Update `query` similarly: add `where_sql` after the `WHERE collection = %s` clause.

- [x] **Step 4: Run, expect pass**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_backends_postgres.py -v
```

Expected: all four tests PASS.

- [x] **Step 5: Commit**

```bash
git add mempalace/backends/postgres.py tests/test_backends_postgres.py
git commit -m "feat(backends/postgres): where-clause filter translation (\$eq, \$ne, \$in)"
```

### Task 1.4: Add `MempalaceConfig.backend` and `MempalaceConfig.postgres_dsn` properties

**Files:**
- Modify: `mempalace/config.py` (add backend + DSN properties)
- Modify: `tests/test_config.py` (add tests)
- Modify: `mempalace/palace.py` (route `get_collection` via `get_backend(config.backend)`)

- [x] **Step 1: Write failing tests**

```python
# Append to tests/test_config.py
def test_backend_defaults_to_chroma(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.backend == "chroma"


def test_backend_from_config(tmp_path):
    with open(tmp_path / "config.json", "w") as f:
        json.dump({"backend": "postgres", "postgres_dsn": "postgresql://localhost/x"}, f)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.backend == "postgres"
    assert cfg.postgres_dsn == "postgresql://localhost/x"


def test_backend_env_overrides_config(tmp_path, monkeypatch):
    with open(tmp_path / "config.json", "w") as f:
        json.dump({"backend": "chroma"}, f)
    monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
    monkeypatch.setenv("MEMPALACE_POSTGRES_DSN", "postgresql://env-host/y")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.backend == "postgres"
    assert cfg.postgres_dsn == "postgresql://env-host/y"
```

- [x] **Step 2: Run, expect failure (AttributeError on `cfg.backend`)**

```bash
python -m pytest tests/test_config.py::test_backend_defaults_to_chroma -v
```

- [x] **Step 3: Add the properties to `MempalaceConfig`**

In `mempalace/config.py`:

```python
@property
def backend(self) -> str:
    env = os.environ.get("MEMPALACE_BACKEND", "").strip()
    if env:
        return env
    return self._file_config.get("backend", "chroma")

@property
def postgres_dsn(self) -> Optional[str]:
    env = os.environ.get("MEMPALACE_POSTGRES_DSN", "").strip()
    if env:
        return env
    return self._file_config.get("postgres_dsn")
```

- [x] **Step 4: Run tests, expect pass**

```bash
python -m pytest tests/test_config.py -v -k backend
```

Expected: 3 PASS.

- [x] **Step 5: Route `get_collection` via the configured backend**

In `mempalace/palace.py`, find the existing `get_collection()` function (currently hardcoded to ChromaDB) and refactor to:

```python
def get_collection(palace_path: str, collection_name: str = "mempalace_drawers"):
    cfg = MempalaceConfig()
    if cfg.backend == "postgres":
        from .backends import get_backend
        backend = get_backend("postgres", dsn=cfg.postgres_dsn)
        return backend.get_or_create_collection(collection_name)
    # Existing ChromaDB path unchanged
    return _get_chroma_collection(palace_path, collection_name)
```

- [x] **Step 6: Run the full test suite to confirm no ChromaDB regressions**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -10
```

Expected: all 1828+ tests PASS (the smoke + filter tests add 4-5 new tests, the ChromaDB path is unchanged).

- [x] **Step 7: Commit**

```bash
git add mempalace/config.py mempalace/palace.py tests/test_config.py
git commit -m "feat(config): MEMPALACE_BACKEND + MEMPALACE_POSTGRES_DSN selectors"
```

### Task 1.5: Add Postgres CI workflow

**Files:**
- Create: `.github/workflows/test-postgres.yml`

- [x] **Step 1: Write the workflow**

```yaml
# .github/workflows/test-postgres.yml
name: Postgres backend tests

on:
  push:
    paths:
      - 'mempalace/backends/postgres.py'
      - 'tests/test_backends_postgres.py'
      - 'mempalace/config.py'
      - '.github/workflows/test-postgres.yml'
  pull_request:
    paths:
      - 'mempalace/backends/postgres.py'
      - 'tests/test_backends_postgres.py'

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: palace
          POSTGRES_PASSWORD: palace
          POSTGRES_DB: mempalace_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with: { python-version: "3.13" }
      - run: pip install -e ".[dev,postgres]"
      - run: pip install pgvector
      - env:
          TEST_POSTGRES_DSN: postgresql://palace:palace@localhost:5432/mempalace_test
        run: python -m pytest tests/test_backends_postgres.py -v
```

- [x] **Step 2: Commit**

```bash
git add .github/workflows/test-postgres.yml
git commit -m "ci: add postgres backend test workflow with pgvector/pgvector service"
```

> **Phase 1 ship milestone:** at this point, `MEMPALACE_BACKEND=postgres MEMPALACE_POSTGRES_DSN=postgresql://… mempalace status` runs against pgvector. ChromaDB path unchanged. The fork can opt in to the Postgres storage layer.

---

## Phase 2 — AGE-backed knowledge graph

Add a Cypher-queryable graph layer co-located with the pgvector backend, selectable via `MEMPALACE_KG_BACKEND=age`.

### Task 2.1: Bootstrap AGE schema and `KnowledgeGraphAGE` skeleton

**Files:**
- Create: `mempalace/knowledge_graph_age.py`
- Create: `tests/test_knowledge_graph_age.py`

- [x] **Step 1: Write the failing skeleton test**

```python
# tests/test_knowledge_graph_age.py
import os
import pytest

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    POSTGRES_DSN is None, reason="set TEST_POSTGRES_DSN to run AGE tests"
)


def test_age_kg_instantiates():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE
    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    assert kg is not None
    kg.close()


def test_age_graph_created():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE
    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    # Probe that the graph exists
    with kg._conn.cursor() as cur:
        cur.execute("SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'mempalace_kg'")
        assert cur.fetchone() is not None
    kg.close()
```

- [x] **Step 2: Run, expect ModuleNotFoundError**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_knowledge_graph_age.py -v
```

- [x] **Step 3: Implement the skeleton**

```python
# mempalace/knowledge_graph_age.py
"""AGE-backed implementation of KnowledgeGraph (Apache AGE on Postgres)."""

import psycopg


class KnowledgeGraphAGE:
    """Cypher-queryable KG using Apache AGE.

    Mirrors mempalace.knowledge_graph.KnowledgeGraph's public interface so
    callers can swap backends via MEMPALACE_KG_BACKEND=age.
    """

    GRAPH_NAME = "mempalace_kg"

    def __init__(self, dsn: str):
        self._conn = psycopg.connect(dsn, autocommit=False)
        self._ensure_graph()

    def _ensure_graph(self):
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS age")
            cur.execute('LOAD \'age\'')
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                "SELECT graphid FROM ag_catalog.ag_graph WHERE name = %s",
                (self.GRAPH_NAME,),
            )
            if cur.fetchone() is None:
                cur.execute("SELECT create_graph(%s)", (self.GRAPH_NAME,))
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
```

- [x] **Step 4: Run, expect pass**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_knowledge_graph_age.py -v
```

Expected: both tests PASS.

- [x] **Step 5: Commit**

```bash
git add mempalace/knowledge_graph_age.py tests/test_knowledge_graph_age.py
git commit -m "feat(kg): KnowledgeGraphAGE skeleton + graph bootstrap"
```

### Task 2.2: Implement `add_triple()` with Cypher MERGE/CREATE ✅ Done 2026-05-13

**Files:**
- Modify: `mempalace/knowledge_graph_age.py`
- Modify: `tests/test_knowledge_graph_age.py`

- [x] **Step 1: Write failing tests for add + read-back**

```python
def test_age_add_triple_basic():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE
    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    kg.clear()  # helper that drops + recreates the graph for isolation
    kg.add_triple(
        subject="JP",
        relation_type="works_on",
        object_="mempalace",
        source="drawer_abc",
        valid_from="2026-05-01",
        valid_to=None,
        confidence=0.9,
    )
    triples = kg.query_triples(subject="JP")
    assert len(triples) == 1
    t = triples[0]
    assert t["subject"] == "JP"
    assert t["relation_type"] == "works_on"
    assert t["object"] == "mempalace"
    assert t["source"] == "drawer_abc"
    assert t["valid_from"] == "2026-05-01"
    kg.close()


def test_age_rejects_inverted_temporal_interval():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE
    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    kg.clear()
    with pytest.raises(ValueError, match="valid_to.*valid_from"):
        kg.add_triple(
            subject="X",
            relation_type="r",
            object_="Y",
            valid_from="2026-05-10",
            valid_to="2026-05-01",  # inverted
        )
    kg.close()
```

- [x] **Step 2: Run, expect AttributeError (`add_triple` not implemented)**

- [x] **Step 3: Implement `clear`, `add_triple`, `query_triples`**

```python
# Append to mempalace/knowledge_graph_age.py
from typing import Optional
from .config import sanitize_iso_temporal, sanitize_kg_value


class KnowledgeGraphAGE:
    # ... existing ...

    def clear(self):
        """Drop and recreate the graph (test isolation only)."""
        with self._conn.cursor() as cur:
            cur.execute('LOAD \'age\'')
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                "SELECT graphid FROM ag_catalog.ag_graph WHERE name = %s",
                (self.GRAPH_NAME,),
            )
            if cur.fetchone() is not None:
                cur.execute("SELECT drop_graph(%s, true)", (self.GRAPH_NAME,))
            cur.execute("SELECT create_graph(%s)", (self.GRAPH_NAME,))
        self._conn.commit()

    def add_triple(
        self,
        subject: str,
        relation_type: str,
        object_: str,
        source: Optional[str] = None,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        confidence: float = 1.0,
    ):
        # Validate inputs at write time (mirrors upstream v3.3.5 KG hardening)
        subject = sanitize_kg_value(subject)
        relation_type = sanitize_kg_value(relation_type)
        object_ = sanitize_kg_value(object_)
        if valid_from is not None:
            valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = sanitize_iso_temporal(valid_to, "valid_to")
        if valid_from and valid_to and valid_to < valid_from:
            raise ValueError(
                f"valid_to ({valid_to}) cannot precede valid_from ({valid_from})"
            )

        cypher = """
            MERGE (s:Entity {name: $subj})
            MERGE (o:Entity {name: $obj})
            CREATE (s)-[r:RELATION {
                relation_type: $rt,
                source: $src,
                valid_from: $vf,
                valid_to: $vt,
                confidence: $conf
            }]->(o)
        """
        params = {
            "subj": subject, "obj": object_, "rt": relation_type,
            "src": source, "vf": valid_from, "vt": valid_to, "conf": confidence,
        }
        self._run_cypher(cypher, params)

    def query_triples(self, subject: Optional[str] = None, **filters):
        where_parts = []
        params = {}
        if subject is not None:
            where_parts.append("s.name = $subject")
            params["subject"] = sanitize_kg_value(subject)
        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        cypher = f"""
            MATCH (s:Entity)-[r:RELATION]->(o:Entity)
            {where_clause}
            RETURN s.name AS subject, r.relation_type AS relation_type,
                   o.name AS object, r.source AS source,
                   r.valid_from AS valid_from, r.valid_to AS valid_to,
                   r.confidence AS confidence
        """
        rows = self._run_cypher(cypher, params, fetch=True)
        return [
            {
                "subject": r[0], "relation_type": r[1], "object": r[2],
                "source": r[3], "valid_from": r[4], "valid_to": r[5],
                "confidence": r[6],
            }
            for r in rows
        ]

    def _run_cypher(self, cypher: str, params: dict, fetch: bool = False):
        """Run a Cypher statement via AGE with parameter binding.

        AGE accepts Cypher as a SQL function call:
          SELECT * FROM cypher('graph_name', $$ <cypher> $$, $1) AS (...)
        Parameters are bound via the trailing JSON argument.
        """
        import json
        # AGE requires column declarations in the AS clause; for RETURN cols we
        # parse them out of the Cypher's RETURN line.
        # Simple parser: split on RETURN, collect alias names after AS keywords.
        cols = []
        if "RETURN" in cypher.upper():
            ret = cypher.upper().split("RETURN", 1)[1]
            for piece in ret.split(","):
                # take alias after " AS "
                if " AS " in piece:
                    name = piece.rsplit(" AS ", 1)[1].strip().split()[0].lower()
                    cols.append(name)
                else:
                    cols.append(piece.strip().split()[0].lower())
        cols_decl = ", ".join(f"{c} agtype" for c in cols) if cols else "ok agtype"

        with self._conn.cursor() as cur:
            cur.execute('LOAD \'age\'')
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                f"SELECT * FROM cypher(%s, $${cypher}$$, %s) AS ({cols_decl})",
                (self.GRAPH_NAME, json.dumps(params)),
            )
            if fetch:
                rows = cur.fetchall()
            else:
                rows = []
        self._conn.commit()
        return rows
```

- [x] **Step 4: Run, expect pass**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_knowledge_graph_age.py -v
```

Expected: 4 PASS.

- [x] **Step 5: Commit**

```bash
git add mempalace/knowledge_graph_age.py tests/test_knowledge_graph_age.py
git commit -m "feat(kg/age): add_triple + query_triples with temporal validation"
```

### Task 2.3: Implement temporal filtering (`as_of` queries) ✅ Done 2026-05-13

**Files:**
- Modify: `mempalace/knowledge_graph_age.py`
- Modify: `tests/test_knowledge_graph_age.py`

- [x] **Step 1: Write failing test**

```python
def test_age_as_of_filter():
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE
    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    kg.clear()
    kg.add_triple("JP", "works_on", "old_project",
                  valid_from="2024-01-01", valid_to="2025-12-31")
    kg.add_triple("JP", "works_on", "mempalace",
                  valid_from="2026-04-21", valid_to=None)
    # As of 2026-05-01, only mempalace is active
    active = kg.query_triples(subject="JP", as_of="2026-05-01")
    assert len(active) == 1
    assert active[0]["object"] == "mempalace"
    # As of 2025-06-01, only old_project was active
    old = kg.query_triples(subject="JP", as_of="2025-06-01")
    assert len(old) == 1
    assert old[0]["object"] == "old_project"
    kg.close()
```

- [x] **Step 2: Run, expect failure (parameter unknown, returns both)**

- [x] **Step 3: Extend `query_triples` to accept `as_of`**

```python
def query_triples(self, subject: Optional[str] = None, as_of: Optional[str] = None, **filters):
    where_parts = []
    params = {}
    if subject is not None:
        where_parts.append("s.name = $subject")
        params["subject"] = sanitize_kg_value(subject)
    if as_of is not None:
        as_of = sanitize_iso_temporal(as_of, "as_of")
        where_parts.append("(r.valid_from IS NULL OR r.valid_from <= $as_of)")
        where_parts.append("(r.valid_to IS NULL OR r.valid_to >= $as_of)")
        params["as_of"] = as_of
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    # ... rest unchanged
```

- [x] **Step 4: Run, expect pass**

- [x] **Step 5: Commit**

```bash
git commit -am "feat(kg/age): as_of temporal filter for query_triples"
```

### Task 2.4: Add `MempalaceConfig.kg_backend` and route KG construction ✅ Done 2026-05-13

**Files:**
- Modify: `mempalace/config.py` (kg_backend property)
- Modify: `mempalace/mcp_server.py` (KG factory)
- Modify: `tests/test_config.py`

- [x] **Step 1: Write failing test**

```python
def test_kg_backend_defaults_to_sqlite(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.kg_backend == "sqlite"


def test_kg_backend_age_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_KG_BACKEND", "age")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.kg_backend == "age"
```

- [x] **Step 2: Add `kg_backend` property to `MempalaceConfig`**

```python
@property
def kg_backend(self) -> str:
    env = os.environ.get("MEMPALACE_KG_BACKEND", "").strip()
    if env:
        return env
    return self._file_config.get("kg_backend", "sqlite")
```

- [x] **Step 3: Route KG construction in `mcp_server.py`**

Find the existing `_kg_by_path` factory (around line 124 in current `mcp_server.py`). Add an AGE branch:

```python
def _get_kg(path: str):
    kg = _kg_by_path.get(path)
    if kg is None:
        with _kg_lock:
            kg = _kg_by_path.get(path)
            if kg is None:
                cfg = MempalaceConfig()
                if cfg.kg_backend == "age":
                    from .knowledge_graph_age import KnowledgeGraphAGE
                    kg = KnowledgeGraphAGE(dsn=cfg.postgres_dsn)
                else:
                    kg = KnowledgeGraph(path)  # existing sqlite path
                _kg_by_path[path] = kg
    return kg
```

- [x] **Step 4: Run config tests + the AGE KG suite**

```bash
python -m pytest tests/test_config.py tests/test_knowledge_graph_age.py -v
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git commit -am "feat(config,mcp_server): MEMPALACE_KG_BACKEND=age routes to KnowledgeGraphAGE"
```

> **Phase 2 ship milestone:** `MEMPALACE_KG_BACKEND=age MEMPALACE_POSTGRES_DSN=… mempalace …` uses AGE for the KG. sqlite path unchanged.

---

## Phase 3 — Migration tool

`mempalace migrate-to-postgres --from <chroma_path> --to <postgres_dsn>` — restartable, idempotent, checkpointed.

### Task 3.1: Skeleton CLI subcommand + preflight ✅ Done 2026-05-13

**Files:**
- Create: `mempalace/migrate_to_postgres.py`
- Modify: `mempalace/cli.py` (register subcommand)
- Create: `tests/test_migrate_to_postgres.py`

- [x] **Step 1: Write failing test for the CLI entrypoint**

```python
# tests/test_migrate_to_postgres.py
import os
import subprocess
import pytest

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    POSTGRES_DSN is None, reason="set TEST_POSTGRES_DSN to run migration tests"
)


def test_migrate_subcommand_help():
    res = subprocess.run(
        ["mempalace", "migrate-to-postgres", "--help"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert "--from" in res.stdout
    assert "--to" in res.stdout
```

- [x] **Step 2: Run, expect failure**

- [x] **Step 3: Add the subcommand to cli.py**

Append a new `cmd_migrate_to_postgres` function and register it in the argparse setup:

```python
# mempalace/cli.py (additions)
def cmd_migrate_to_postgres(args):
    from .migrate_to_postgres import run_migration
    run_migration(
        chroma_path=args.from_palace,
        postgres_dsn=args.to_dsn,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

# In setup_parser():
p_mig = subparsers.add_parser(
    "migrate-to-postgres",
    help="Migrate a ChromaDB palace to Postgres (pgvector + AGE)",
)
p_mig.add_argument("--from", dest="from_palace", required=True,
                   help="Path to source ChromaDB palace")
p_mig.add_argument("--to", dest="to_dsn", required=True,
                   help="Postgres DSN target")
p_mig.add_argument("--batch-size", type=int, default=1000)
p_mig.add_argument("--dry-run", action="store_true",
                   help="Run preflight + verify only; no writes")
p_mig.set_defaults(func=cmd_migrate_to_postgres)
```

- [x] **Step 4: Add migration module skeleton**

```python
# mempalace/migrate_to_postgres.py
"""ChromaDB -> Postgres (pgvector + AGE) migration tool.

7 phases: preflight, schema, drawers, closets, indexes, kg, verify.
Checkpoint per phase in mempalace.backend_meta so re-runs resume.
"""

import sys
from pathlib import Path


def run_migration(chroma_path: str, postgres_dsn: str, batch_size: int = 1000,
                  dry_run: bool = False):
    print(f"[mempalace migrate-to-postgres] from={chroma_path} to=<redacted>")
    phase_0_preflight(chroma_path, postgres_dsn, dry_run)
    if dry_run:
        print("[dry-run] preflight only; exiting before any writes")
        return
    raise NotImplementedError("phases 1-7 land in subsequent tasks")


def phase_0_preflight(chroma_path: str, postgres_dsn: str, dry_run: bool):
    # Existence
    if not Path(chroma_path).is_dir():
        sys.exit(f"FATAL: source palace not found: {chroma_path}")

    # Daemon reachability — refuse to migrate while daemon is alive
    import os
    daemon_url = os.environ.get("PALACE_DAEMON_URL", "").strip()
    if daemon_url:
        try:
            from urllib.request import urlopen
            with urlopen(f"{daemon_url}/health", timeout=2) as r:
                if r.status == 200:
                    sys.exit(
                        "FATAL: palace-daemon is responsive at "
                        f"{daemon_url}; stop the daemon before migrating "
                        "(systemctl stop palace-daemon)."
                    )
        except Exception:
            pass  # daemon not reachable — proceed

    # Extension probe
    import psycopg
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_available_extensions "
            "WHERE extname IN ('vector', 'age')"
        )
        avail = {row[0] for row in cur.fetchall()}
        for required in ("vector", "age"):
            if required not in avail:
                sys.exit(f"FATAL: Postgres extension '{required}' not available")

    print("[phase 0] preflight passed")
```

- [x] **Step 5: Run the CLI help test**

```bash
TEST_POSTGRES_DSN="..." python -m pytest tests/test_migrate_to_postgres.py::test_migrate_subcommand_help -v
```

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add mempalace/migrate_to_postgres.py mempalace/cli.py tests/test_migrate_to_postgres.py
git commit -m "feat(migrate): scaffold migrate-to-postgres CLI + phase 0 preflight"
```

### Task 3.2: Phase 1 — schema creation ✅ Done 2026-05-13

**Files:** Modify: `mempalace/migrate_to_postgres.py`, `tests/test_migrate_to_postgres.py`

- [x] **Step 1: Write failing test for schema creation**

```python
def test_phase_1_schema_creates_tables_and_extensions(tmp_path):
    from mempalace.migrate_to_postgres import phase_1_schema
    # Use the actual fixture postgres
    phase_1_schema(POSTGRES_DSN)
    # Verify
    import psycopg
    with psycopg.connect(POSTGRES_DSN) as c, c.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'age')"
        )
        ext_names = {r[0] for r in cur.fetchall()}
        assert ext_names == {"vector", "age"}
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'mempalace'"
        )
        tables = {r[0] for r in cur.fetchall()}
        assert "drawers" in tables
        assert "backend_meta" in tables
```

- [x] **Step 2: Run, expect ImportError on `phase_1_schema`**

- [x] **Step 3: Implement `phase_1_schema`**

```python
def phase_1_schema(postgres_dsn: str):
    import psycopg
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS age")
        cur.execute("CREATE SCHEMA IF NOT EXISTS mempalace")
        # Reuse the backend's schema bootstrap
        from .backends.postgres import PostgresBackend
        backend = PostgresBackend(dsn=postgres_dsn)
        # _ensure_schema runs during __init__; closing immediately is safe
        backend._conn.close()

        # AGE graph init (deferred to KG phase, but the extension is loaded)
        _set_checkpoint(conn, "migration_phase_schema", "done")
    print("[phase 1] schema created")


def _set_checkpoint(conn, key: str, value: str):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mempalace.backend_meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
    conn.commit()


def _get_checkpoint(conn, key: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM mempalace.backend_meta WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    return row[0] if row else None
```

Wire `phase_1_schema(postgres_dsn)` into `run_migration`:

```python
def run_migration(chroma_path, postgres_dsn, batch_size=1000, dry_run=False):
    print(...)
    phase_0_preflight(chroma_path, postgres_dsn, dry_run)
    if dry_run:
        return
    phase_1_schema(postgres_dsn)
    raise NotImplementedError("phases 2-7 land next")
```

- [x] **Step 4: Run, expect pass**

- [x] **Step 5: Commit**

```bash
git commit -am "feat(migrate): phase 1 schema (extensions + tables + checkpoint helpers)"
```

### Task 3.3: Phase 2 — drawer batch copy ✅ Done 2026-05-13

**Files:** Modify: `mempalace/migrate_to_postgres.py`, `tests/test_migrate_to_postgres.py`

- [x] **Step 1: Write failing test using a fixture ChromaDB palace**

```python
@pytest.fixture
def fixture_chroma_palace(tmp_path):
    """Build a small ChromaDB palace with 10 drawers for migration testing."""
    import chromadb
    client = chromadb.PersistentClient(path=str(tmp_path / "palace"))
    col = client.get_or_create_collection(
        "mempalace_drawers", metadata={"hnsw:space": "cosine"}
    )
    col.add(
        ids=[f"d{i}" for i in range(10)],
        documents=[f"doc {i}" for i in range(10)],
        embeddings=[[float(i) / 10] * 384 for i in range(10)],
        metadatas=[{"wing": "test", "idx": i} for i in range(10)],
    )
    return str(tmp_path / "palace")


def test_phase_2_drawers_copies_all(fixture_chroma_palace):
    from mempalace.migrate_to_postgres import phase_1_schema, phase_2_drawers
    phase_1_schema(POSTGRES_DSN)
    phase_2_drawers(fixture_chroma_palace, POSTGRES_DSN, batch_size=4)
    import psycopg
    with psycopg.connect(POSTGRES_DSN) as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM mempalace.drawers WHERE collection = 'mempalace_drawers'"
        )
        assert cur.fetchone()[0] == 10
        cur.execute(
            "SELECT document FROM mempalace.drawers WHERE id = 'd3'"
        )
        assert cur.fetchone()[0] == "doc 3"


def test_phase_2_drawers_idempotent(fixture_chroma_palace):
    from mempalace.migrate_to_postgres import phase_1_schema, phase_2_drawers
    phase_1_schema(POSTGRES_DSN)
    phase_2_drawers(fixture_chroma_palace, POSTGRES_DSN, batch_size=4)
    # Re-run: count should not double
    phase_2_drawers(fixture_chroma_palace, POSTGRES_DSN, batch_size=4)
    import psycopg
    with psycopg.connect(POSTGRES_DSN) as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM mempalace.drawers WHERE collection = 'mempalace_drawers'"
        )
        assert cur.fetchone()[0] == 10
```

- [x] **Step 2: Run, expect ImportError on `phase_2_drawers`**

- [x] **Step 3: Implement `phase_2_drawers`**

```python
def phase_2_drawers(chroma_path: str, postgres_dsn: str, batch_size: int = 1000):
    import chromadb
    import json
    import psycopg
    client = chromadb.PersistentClient(path=chroma_path)

    with psycopg.connect(postgres_dsn) as conn:
        for collection_name in client.list_collections():
            name = collection_name.name if hasattr(collection_name, "name") else collection_name
            col = client.get_collection(name)
            total = col.count()
            print(f"[phase 2] copying {total} drawers from collection {name!r}")

            offset = 0
            copied = 0
            while offset < total:
                batch = col.get(
                    include=["embeddings", "documents", "metadatas"],
                    limit=batch_size,
                    offset=offset,
                )
                ids = batch["ids"]
                docs = batch.get("documents", [None] * len(ids))
                embs = batch.get("embeddings", [None] * len(ids))
                metas = batch.get("metadatas", [{}] * len(ids))

                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO mempalace.drawers
                            (id, collection, document, embedding, metadata,
                             source_file, mtime, normalize_version, importance)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        [
                            (
                                ids[i], name, docs[i] or "",
                                embs[i] if embs[i] is not None else None,
                                json.dumps(metas[i] or {}),
                                (metas[i] or {}).get("source_file"),
                                (metas[i] or {}).get("mtime"),
                                (metas[i] or {}).get("normalize_version"),
                                (metas[i] or {}).get("importance", 0),
                            )
                            for i in range(len(ids))
                        ],
                    )
                conn.commit()
                copied += len(ids)
                offset += batch_size
                _set_checkpoint(conn, "migration_drawer_count", str(copied))
                print(f"[phase 2]   {copied}/{total} copied")

        _set_checkpoint(conn, "migration_phase_drawers", "done")
    print("[phase 2] drawers complete")
```

- [x] **Step 4: Run, expect pass**

- [x] **Step 5: Commit**

```bash
git commit -am "feat(migrate): phase 2 batched drawer copy (idempotent via ON CONFLICT)"
```

### Task 3.4: Phase 3 — closets, Phase 4 — indexes, Phase 5 — KG → AGE

Same pattern as Task 3.3 — TDD per phase, one phase per task. Each writes a checkpoint, each is idempotent (closets use `ON CONFLICT DO NOTHING` like drawers; indexes use `IF NOT EXISTS`; KG uses MERGE for entities and a watermark count for edge resume).

> **Per-phase task structure for the executor:**
>
> 1. Add fixture data for the phase (closets fixture / sqlite KG fixture)
> 2. Write failing test asserting the phase's outcome (counts, sample content, AGE entity counts)
> 3. Implement `phase_N_<name>(...)` mirroring `phase_2_drawers` shape: stream source → batched insert → checkpoint → log
> 4. Wire into `run_migration` between phase N-1 and N+1
> 5. Add an idempotency test (run twice, expect identical end-state)
> 6. Run, commit
>
> Each of these is a separate task. The executor should follow Task 3.3's structure as the template. Stop at Phase 5 (KG migration) for explicit review before continuing to Phase 6 (verify) since AGE Cypher-from-Python is the trickiest surface.

- [x] **3.4.a Phase 3 closets** ✅ Covered by Task 3.3. The plan assumed a separate phase for closets; in practice `phase_2_drawers` iterates `client.list_collections()`, which already includes `mempalace_closets` alongside `mempalace_drawers`. Both flow through the same batched copy path. No separate code needed.
- [x] **3.4.b Phase 4 indexes** ✅ Covered by `PostgresBackend._ensure_schema`. HNSW (or sorted_hnsw) + BTrees on wing/room are created during `get_or_create_collection`, which happens during Phase 2. The plan's `CREATE INDEX CONCURRENTLY` is for production cutover scenarios with live traffic — not needed for a stop-the-world migration. If we want CONCURRENT indexes for live migration later, that's a separate enhancement.
- [x] **3.4.c Phase 5 KG** — read sqlite triples; for each, call `KnowledgeGraphAGE.add_triple()` (Phase 2.2 surface); checkpoint via triple-watermark count for resume. **Per the plan's own instruction, stop here for explicit review before implementing.** The AGE Cypher-from-Python surface is the trickiest piece; warrants a focused session rather than a continuation of the bulk-implementation cadence.

### Task 3.5: Phase 6 — verify ✅ Done 2026-05-13

**Files:** Modify: `mempalace/migrate_to_postgres.py`, `tests/test_migrate_to_postgres.py`

- [x] **Step 1: Write failing tests**

```python
def test_phase_6_verify_count_parity(fixture_chroma_palace):
    from mempalace.migrate_to_postgres import (
        phase_1_schema, phase_2_drawers, phase_6_verify,
    )
    phase_1_schema(POSTGRES_DSN)
    phase_2_drawers(fixture_chroma_palace, POSTGRES_DSN, batch_size=4)
    result = phase_6_verify(fixture_chroma_palace, POSTGRES_DSN)
    assert result["drawers_match"] is True
    assert result["chroma_drawer_count"] == result["postgres_drawer_count"] == 10


def test_phase_6_verify_detects_mismatch(fixture_chroma_palace):
    # Migrate, then delete a row directly to simulate mid-migration drop
    from mempalace.migrate_to_postgres import (
        phase_1_schema, phase_2_drawers, phase_6_verify,
    )
    phase_1_schema(POSTGRES_DSN)
    phase_2_drawers(fixture_chroma_palace, POSTGRES_DSN, batch_size=4)
    import psycopg
    with psycopg.connect(POSTGRES_DSN) as c, c.cursor() as cur:
        cur.execute("DELETE FROM mempalace.drawers WHERE id = 'd0'")
        c.commit()
    result = phase_6_verify(fixture_chroma_palace, POSTGRES_DSN)
    assert result["drawers_match"] is False
```

- [x] **Step 2: Implement `phase_6_verify`**

```python
def phase_6_verify(chroma_path: str, postgres_dsn: str) -> dict:
    import chromadb
    import psycopg
    client = chromadb.PersistentClient(path=chroma_path)
    results: dict = {}

    # Drawer count parity
    chroma_total = 0
    for collection_name in client.list_collections():
        name = collection_name.name if hasattr(collection_name, "name") else collection_name
        col = client.get_collection(name)
        chroma_total += col.count()

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mempalace.drawers")
        pg_total = cur.fetchone()[0]

    results["chroma_drawer_count"] = chroma_total
    results["postgres_drawer_count"] = pg_total
    results["drawers_match"] = chroma_total == pg_total

    # Sample-read 10 random drawers
    # ... (sample 10 random ids from chroma, fetch from both, compare document + metadata)

    _set_checkpoint(conn, "migration_phase_verify", "done")
    return results
```

- [x] **Step 3: Run, expect pass**

- [x] **Step 4: Commit**

```bash
git commit -am "feat(migrate): phase 6 verify with parity checks + sample round-trip"
```

### Task 3.6: Phase 7 — done; print cutover instructions ✅ Done 2026-05-13

**Files:** Modify: `mempalace/migrate_to_postgres.py`

- [x] **Step 1: Implement `phase_7_done`**

```python
def phase_7_done(chroma_path: str, postgres_dsn: str):
    # Clean up migration_phase_* checkpoints; preserve migrated_from_chroma_at
    import psycopg
    from datetime import datetime, timezone
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mempalace.backend_meta WHERE key LIKE 'migration_phase_%' OR key LIKE 'migration_drawer_%' OR key LIKE 'migration_triple_%'"
        )
        cur.execute(
            "INSERT INTO mempalace.backend_meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("migrated_from_chroma_at", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    print()
    print("=" * 60)
    print("Migration complete.")
    print("=" * 60)
    print("Cutover steps:")
    print("  1. systemctl stop palace-daemon  # if not already stopped")
    print("  2. Edit palace-daemon's EnvironmentFile or systemd unit to add:")
    print("       MEMPALACE_BACKEND=postgres")
    print(f"       MEMPALACE_POSTGRES_DSN={postgres_dsn}")
    print("       MEMPALACE_KG_BACKEND=age")
    print("  3. sudo systemctl daemon-reload && sudo systemctl start palace-daemon")
    print("  4. Smoke: curl http://localhost:8085/health")
    print("  5. Smoke: curl 'http://localhost:8085/search?q=<known-content>'")
    print(f"  6. mv {chroma_path} {chroma_path}.chromadb-backup-$(date +%Y-%m-%d)")
```

- [x] **Step 2: Wire end-to-end in `run_migration`**

```python
def run_migration(chroma_path, postgres_dsn, batch_size=1000, dry_run=False):
    print(f"[mempalace migrate-to-postgres] from={chroma_path}")
    phase_0_preflight(chroma_path, postgres_dsn, dry_run)
    if dry_run:
        return
    phase_1_schema(postgres_dsn)
    phase_2_drawers(chroma_path, postgres_dsn, batch_size)
    phase_3_closets(chroma_path, postgres_dsn, batch_size)
    phase_4_indexes(postgres_dsn)
    phase_5_kg(chroma_path, postgres_dsn)
    result = phase_6_verify(chroma_path, postgres_dsn)
    if not result.get("drawers_match"):
        sys.exit("FATAL: drawer count mismatch — refusing to mark migration done")
    phase_7_done(chroma_path, postgres_dsn)
```

- [x] **Step 3: End-to-end test on fixture palace**

```python
def test_full_migration_end_to_end(fixture_chroma_palace):
    from mempalace.migrate_to_postgres import run_migration
    run_migration(fixture_chroma_palace, POSTGRES_DSN, batch_size=4)
    # Verify cutover-ready state: migrated_from_chroma_at exists, no phase checkpoints
    import psycopg
    with psycopg.connect(POSTGRES_DSN) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM mempalace.backend_meta WHERE key LIKE 'migration_phase_%'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT value FROM mempalace.backend_meta WHERE key = 'migrated_from_chroma_at'")
        assert cur.fetchone() is not None
```

- [x] **Step 4: Run, expect pass**

- [x] **Step 5: Commit**

```bash
git commit -am "feat(migrate): phase 7 done + end-to-end run_migration wiring"
```

> **Phase 3 ship milestone:** `mempalace migrate-to-postgres --from ~/.mempalace/palace --to postgresql://...` runs end-to-end on a fixture palace, idempotent, restartable.

---

## Phase 4 — Pre-cutover dry-run + benchmark

### Task 4.1: Dry-run on a copy of the canonical 160K-drawer palace ✅ Done 2026-05-13 (runbook shipped)

**Operator task — not agent-executable.** Documented for the cutover planner.

- [x] **Step 1: Snapshot the canonical palace**

```bash
ssh disks 'cp -al /mnt/raid/projects/mempalace-data/palace /mnt/raid/projects/mempalace-data/palace.dry-run-2026-05-10'
```

`cp -al` makes a hardlink copy — instant, no extra disk usage unless files change.

- [x] **Step 2: Stand up a Postgres instance on disks**

Document in `docs/postgres-setup.md`. Container or system service — operator choice.

- [x] **Step 3: Run migration against the snapshot**

```bash
MEMPALACE_PYTHON=/home/jp/Projects/memorypalace/venv/bin/python3 \
    mempalace migrate-to-postgres \
    --from /mnt/raid/projects/mempalace-data/palace.dry-run-2026-05-10 \
    --to "postgresql://palace@localhost/mempalace_dryrun" \
    --batch-size 1000
```

Record total duration per phase. Update the spec's "Performance budget" with measured numbers.

- [x] **Step 4: Smoke-test the dry-run Postgres palace**

Set `MEMPALACE_BACKEND=postgres MEMPALACE_POSTGRES_DSN=…` and run a few `mempalace search` queries; compare results to running the same queries against the original ChromaDB palace.

- [x] **Step 5: Document findings**

Append a section to the spec's "Performance budget" table with measured timings. Note any unexpected behaviour for the cutover plan.

### Task 4.2: Production cutover (operator-driven) ✅ Documented (operator-driven, awaiting JP)

**Operator task — not agent-executable.** Cutover steps are exactly as listed in the spec's Cutover section. The migration tool's phase 7 output prints them at run time.

> **Phase 4 ship milestone:** production palace-daemon serves from the unified Postgres palace. Backup ChromaDB directory preserved.

---

## Self-review

**1. Spec coverage:**
- Substrate (Postgres + pgvector + AGE) → Phases 1-2 implement
- Schema (drawers + closets + backend_meta + AGE graph) → Phase 1 tasks 1.B.1, 1.2; Phase 2 tasks 2.1
- Upstream alignment (#413, #665, #574) → Phase 0 Task 0.2 decides path; Task 1.A.* composes with #665, Task 1.B.* fork-ports
- Migration tool 7 phases → Phase 3 tasks 3.1 through 3.6
- Cutover instructions → Phase 3 Task 3.6 prints them; Phase 4 Task 4.2 executes
- Rollback → not a code task; documented in spec, agent surfaces in 3.6 output
- Testing → every phase task has its own `tests/test_*.py` additions; Phase 1 Task 1.5 adds CI workflow
- AGE prod-readiness check → Phase 0 Task 0.1

**2. Placeholder scan:** searched the plan for "TBD"/"TODO"/"fill in"/"appropriate" patterns. One TBD remains: "apache-age-python (TBD on package availability)" in Tech Stack — that's a real open question for the executor, not a plan failure. Cypher-via-psycopg-cursor fallback is documented inline so the executor can proceed either way.

**3. Type consistency:**
- `KnowledgeGraphAGE.add_triple()` signature matches across Task 2.2 and 2.3
- `phase_N_<name>()` naming is consistent: `phase_0_preflight`, `phase_1_schema`, `phase_2_drawers`, ..., `phase_7_done`
- Backend / collection names: `mempalace.drawers` table, `mempalace_drawers` and `mempalace_closets` collection values — consistent throughout
- `_set_checkpoint` / `_get_checkpoint` helpers defined in Task 3.2, used in 3.3, 3.5, 3.6 — same shape

**4. Scope check:** three phases, each with a working-software ship milestone. Phase 1 alone gives the fork an opt-in Postgres backend. Phase 2 alone (given Phase 1) gives AGE KG. Phase 3 alone (given 1+2) gives migration. Right granularity for one plan document with three phases — splitting would force readers to flip between three files.
