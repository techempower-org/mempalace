"""AGE-backed implementation of KnowledgeGraph (Apache AGE on Postgres).

Companion to `mempalace.knowledge_graph.KnowledgeGraph` (SQLite). Selectable
via `MEMPALACE_KG_BACKEND=age` once the config-routing layer is wired up.
Mirrors the public interface of the SQLite KG so callers can swap backends
without code changes.

The graph itself is `mempalace_kg` registered in AGE's `ag_catalog`. It is
created on first init and reused thereafter — initialization is idempotent.
"""

import psycopg2


class KnowledgeGraphAGE:
    """Cypher-queryable KG using Apache AGE on a Postgres connection.

    Currently a skeleton: instantiation + graph bootstrap only. Triple
    add/query operations and temporal filtering arrive in subsequent
    commits; the routing layer that lets `MempalaceConfig.kg_backend`
    select this backend over the SQLite one is its own change.
    """

    GRAPH_NAME = "mempalace_kg"

    def __init__(self, dsn: str):
        """Open a Postgres connection and ensure `mempalace_kg` exists.

        Args:
            dsn: PostgreSQL DSN. Must point at a database where the AGE
                extension is installed (CREATE EXTENSION succeeds). The
                ``apache/age:release_PG16_1.6.0`` image we deploy on the
                homelab already has the .so files baked in; bare-metal
                Postgres requires source-build of AGE first.
        """
        self._conn = psycopg2.connect(dsn)
        # KG writes need explicit commit semantics, not autocommit — keep
        # the same shape as the SQLite KG so the eventual unified write
        # API can be swapped underneath. The bootstrap below commits its
        # own changes; subsequent write operations control their own
        # transactions.
        self._conn.autocommit = False
        self._ensure_graph()

    def _ensure_graph(self) -> None:
        """Idempotent: load AGE, set search_path, create the graph if absent.

        Both `LOAD 'age'` and the `search_path` setting are session-scoped
        — anything new that takes a fresh cursor on this connection must
        re-run them before issuing Cypher. The pattern in this module is
        to always wrap `LOAD 'age'; SET search_path = ag_catalog, "$user",
        public` around each cypher block; for the bootstrap that's done
        here, downstream methods will repeat as needed.
        """
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS age")
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                "SELECT graphid FROM ag_catalog.ag_graph WHERE name = %s",
                (self.GRAPH_NAME,),
            )
            if cur.fetchone() is None:
                cur.execute("SELECT create_graph(%s)", (self.GRAPH_NAME,))
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying Postgres connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> "KnowledgeGraphAGE":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
