"""AGE-backed implementation of KnowledgeGraph (Apache AGE on Postgres).

Companion to `mempalace.knowledge_graph.KnowledgeGraph` (SQLite). Selectable
via `MEMPALACE_KG_BACKEND=age` once the config-routing layer is wired up.
Mirrors the public interface of the SQLite KG so callers can swap backends
without code changes.

The graph itself is `mempalace_kg` registered in AGE's `ag_catalog`. It is
created on first init and reused thereafter — initialization is idempotent.
"""

import json
import re
from typing import Any, Optional

import psycopg2

from .config import sanitize_iso_temporal, sanitize_kg_value


class KnowledgeGraphAGE:
    """Cypher-queryable KG using Apache AGE on a Postgres connection.

    Public surface mirrors the SQLite ``KnowledgeGraph``:

    - ``add_triple(subject, relation_type, object_, ...)`` — write a triple.
      Validates inputs (sanitize_kg_value, sanitize_iso_temporal) and rejects
      inverted temporal intervals at write time.
    - ``query_triples(subject=..., **filters)`` — read triples matching the
      filter. Filter set is intentionally small for now; temporal ``as_of``
      filtering arrives in Phase 2.3.
    - ``clear()`` — drop + recreate the graph (test isolation).

    Routing via ``MempalaceConfig.kg_backend`` arrives in Phase 2.4.
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

    # ── Triple write/read surface ─────────────────────────────────────

    def clear(self) -> None:
        """Drop and recreate the graph. Intended for test isolation only.

        Production callers should use targeted deletes; this nukes every
        triple in the graph. The graph is re-registered immediately so
        the instance remains usable for subsequent writes.
        """
        with self._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
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
    ) -> None:
        """Write a triple ``(subject)-[relation_type]->(object_)`` to AGE.

        Sanitizes the three positional values via ``sanitize_kg_value`` and
        the two temporal fields via ``sanitize_iso_temporal``. Rejects
        inverted intervals (``valid_to < valid_from``) at write time so
        bad data never reaches the graph.

        Entities are MERGE'd (created if absent, reused if present);
        the relation itself is always CREATE'd so multiple temporally-
        distinct facts between the same entities co-exist as parallel
        edges (matches the SQLite KG semantics — see knowledge_graph.py).
        """
        subject = sanitize_kg_value(subject, "subject")
        relation_type = sanitize_kg_value(relation_type, "relation_type")
        object_ = sanitize_kg_value(object_, "object")
        if valid_from is not None:
            valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = sanitize_iso_temporal(valid_to, "valid_to")
        if valid_from and valid_to and valid_to < valid_from:
            raise ValueError(f"valid_to ({valid_to}) cannot precede valid_from ({valid_from})")

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
            "subj": subject,
            "obj": object_,
            "rt": relation_type,
            "src": source,
            "vf": valid_from,
            "vt": valid_to,
            "conf": confidence,
        }
        self._run_cypher(cypher, params)

    def query_triples(
        self,
        subject: Optional[str] = None,
        as_of: Optional[str] = None,
        **_filters,
    ) -> list:
        """Return triples matching ``subject`` and active ``as_of`` a date.

        ``as_of`` filters to triples whose temporal interval contains the
        given date: ``valid_from <= as_of <= valid_to``, with NULL on
        either end interpreted as open (NULL valid_from = active since
        forever; NULL valid_to = still active).

        Empty list when no match. Each triple is a dict with keys:
        ``subject, relation_type, object, source, valid_from, valid_to,
        confidence``.
        """
        where_parts = []
        params: dict = {}
        if subject is not None:
            where_parts.append("s.name = $subject")
            params["subject"] = sanitize_kg_value(subject, "subject")
        if as_of is not None:
            as_of = sanitize_iso_temporal(as_of, "as_of")
            where_parts.append("(r.valid_from IS NULL OR r.valid_from <= $as_of)")
            where_parts.append("(r.valid_to IS NULL OR r.valid_to >= $as_of)")
            params["as_of"] = as_of
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
                "subject": self._unwrap_agtype(r[0]),
                "relation_type": self._unwrap_agtype(r[1]),
                "object": self._unwrap_agtype(r[2]),
                "source": self._unwrap_agtype(r[3]),
                "valid_from": self._unwrap_agtype(r[4]),
                "valid_to": self._unwrap_agtype(r[5]),
                "confidence": self._unwrap_agtype(r[6]),
            }
            for r in rows
        ]

    # ── AGE plumbing ──────────────────────────────────────────────────

    _RETURN_RE = re.compile(r"\bRETURN\b(.*?)(\bORDER\b|\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL)

    def _extract_return_aliases(self, cypher: str) -> list:
        """Pull alias names out of a Cypher RETURN clause.

        AGE's ``SELECT * FROM cypher(...) AS (col1 type, col2 type, ...)`` form
        requires the caller to declare column names + agtypes up front. We
        parse them from the RETURN line's ``AS <alias>`` markers.
        """
        m = self._RETURN_RE.search(cypher)
        if not m:
            return []
        clause = m.group(1)
        aliases = []
        for piece in clause.split(","):
            piece = piece.strip()
            if " AS " in piece.upper():
                idx = piece.upper().rfind(" AS ")
                name = piece[idx + 4 :].strip().split()[0].lower()
                aliases.append(name)
        return aliases

    def _run_cypher(self, cypher: str, params: dict, fetch: bool = False) -> list:
        """Run a Cypher statement via AGE with parameter binding.

        AGE accepts Cypher as a SQL function call:
            SELECT * FROM cypher('graph_name', $$ <cypher> $$, $1) AS (...)
        Parameters are bound via the trailing JSON argument. Column types
        are all ``agtype``; callers unwrap via ``_unwrap_agtype``.
        """
        aliases = self._extract_return_aliases(cypher) if fetch else []
        cols_decl = ", ".join(f"{c} agtype" for c in aliases) if aliases else "ok agtype"

        rows: list = []
        with self._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                f"SELECT * FROM cypher(%s, $${cypher}$$, %s) AS ({cols_decl})",
                (self.GRAPH_NAME, json.dumps(params)),
            )
            if fetch:
                rows = cur.fetchall()
        self._conn.commit()
        return rows

    @staticmethod
    def _unwrap_agtype(value: Any) -> Any:
        """Unwrap an AGE ``agtype`` value to a plain Python scalar.

        AGE's psycopg adapter returns strings shaped like ``"foo"`` (JSON-
        quoted) for scalars and bare numbers for ints/floats. We try
        ``json.loads`` first; on failure pass the raw value through.
        Null AGE values come back as the string ``"null"``.
        """
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "null":
                return None
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return value
        return value
