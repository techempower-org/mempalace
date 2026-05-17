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

from .config import sanitize_iso_temporal, sanitize_kg_value


def _load_psycopg2():
    """Lazy import so pure-Python helpers (e.g. _cypher_literal) don't
    require the optional [postgres] extra. KnowledgeGraphAGE itself
    obviously needs it; the import fires at __init__ time."""
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "AGE knowledge-graph backend requires optional dependencies. "
            'Install with: pip install "mempalace[postgres]"'
        ) from exc
    return psycopg2


# Unique dollar-quote tag for the cypher() outer SQL boundary. Picked so
# it can't appear inside a sanitized Cypher literal (we reject the tag
# substring in ``_cypher_literal``). Hex-suffixed at module load so
# tests/forks can change it without coordinating with attackers.
_AGE_DQ_TAG = "mp_age_q"
_AGE_DQ_OPEN = f"${_AGE_DQ_TAG}$"
_AGE_DQ_CLOSE = f"${_AGE_DQ_TAG}$"

# Cypher parameter reference: matches $name where name is a valid
# identifier. Used by ``_inline_cypher_params`` for one-pass substitution.
_CYPHER_PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _cypher_literal(value: Any) -> str:
    """Render a Python value as a Cypher literal for inline substitution.

    Used by ``_run_cypher`` to inline parameters into the Cypher source
    instead of using AGE's third-arg parameter form (which requires a
    prepared-statement bind that psycopg2 can't produce — see the
    ``_run_cypher`` docstring).

    Sanitization happens upstream in ``add_triple``/``query_triples``;
    this function handles formatting only.

    - ``None`` → bare Cypher ``NULL``
    - ``int``/``float`` → bare numeric literal
    - ``bool`` → bare ``true``/``false``
    - ``str`` → single-quoted with backslash-escaping
    - anything else → ``json.dumps`` and treated as a string

    Defense in depth: rejects strings containing the outer dollar-quote
    tag (``$mp_age_q$``) so a sanitized-but-adversarial value cannot
    close the outer SQL boundary that ``_run_cypher`` builds. Upstream
    sanitizers (``sanitize_kg_value``, ``sanitize_iso_temporal``) already
    strip control chars and quotes — this check is the last line.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = value if isinstance(value, str) else json.dumps(value)
    if _AGE_DQ_TAG in s:
        raise ValueError(
            f"Cypher literal contains the AGE dollar-quote tag "
            f"'{_AGE_DQ_TAG}'; reject upstream in the sanitizer."
        )
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _inline_cypher_params(cypher: str, params: dict) -> str:
    """Single-pass substitution of ``$name`` placeholders with literals.

    Uses ``re.sub`` with a callback so substitutions never re-enter the
    regex — a value containing ``$other_key`` is preserved verbatim,
    fixing the recursive-replacement bug in the prior length-sorted
    ``str.replace`` loop (`techempower-org/mempalace#101` Gemini review,
    2026-05-17).
    """

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in params:
            # Leave unknown placeholders alone — AGE will raise a clear
            # parse error rather than silently swallow the literal text.
            return match.group(0)
        return _cypher_literal(params[key])

    return _CYPHER_PARAM_RE.sub(_sub, cypher)


class KnowledgeGraphAGE:
    """Cypher-queryable KG using Apache AGE on a Postgres connection.

    Public surface mirrors the SQLite ``KnowledgeGraph``:

    - ``add_triple(subject, relation_type, object_, ...)`` — write a triple.
      Validates inputs (sanitize_kg_value, sanitize_iso_temporal) and rejects
      inverted temporal intervals at write time.
    - ``query_triples(subject=..., **filters)`` — read triples matching the
      filter. Filter set is intentionally small for now; temporal ``as_of``
      filtering arrives in the as_of-filter feature.
    - ``clear()`` — drop + recreate the graph (test isolation).

    Routing via ``MempalaceConfig.kg_backend`` arrives in the kg_backend feature.
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
        psycopg2 = _load_psycopg2()
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

    def add_entity(self, name: str, entity_type: str = "unknown", properties: Optional[dict] = None) -> str:
        """Add or update an entity node.

        Mirrors ``KnowledgeGraph.add_entity`` in the SQLite backend. MERGE
        creates the node if absent, and sets ``type``/``properties`` on
        creation only — AGE doesn't support ``ON CREATE SET``, so the
        property setting happens via ``MATCH ... SET`` in a follow-up
        Cypher call to keep semantics close to the SQLite ``INSERT OR
        REPLACE``.

        Returns the entity id (``name.lower().replace(' ', '_')``) for
        SQLite-callsite source compatibility.
        """
        name = sanitize_kg_value(name, "name")
        eid = self._entity_id(name)
        props_json = json.dumps(properties or {})
        # AGE's MERGE-without-ON-CREATE-SET means we always set type/props.
        # That diverges slightly from SQLite's "REPLACE if exists" behavior:
        # any concurrent writer's type would also be overwritten. For the
        # write-through use case (extractor populating new entities) that's
        # the right behavior; for the unusual case where two writers race
        # on the same entity name, last-write-wins is acceptable.
        self._run_cypher(
            """
            MERGE (e:Entity {name: $name})
            SET e.type = $type, e.properties = $props
            """,
            {"name": name, "type": entity_type, "props": props_json},
        )
        return eid

    @staticmethod
    def _entity_id(name: str) -> str:
        """Mirror SQLite KG's id derivation so cross-backend callers see
        the same id for the same entity name."""
        return name.lower().replace(" ", "_").replace("'", "")

    def invalidate(self, subject: str, predicate: str, obj: str, ended: Optional[str] = None) -> int:
        """Mark active triples matching (subject, predicate, object) as expired.

        Sets ``valid_to`` to ``ended`` (or today if None) on every RELATION
        whose ``valid_to`` is currently NULL. Mirrors SQLite KG's
        ``invalidate`` exactly.

        Returns the number of triples affected.

        Inverted-interval check: if the resulting ``valid_to`` would precede
        an existing ``valid_from`` on any affected triple, raises ValueError
        before any write happens.
        """
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_kg_value(predicate, "predicate")
        obj = sanitize_kg_value(obj, "object")
        if ended is None:
            from datetime import date as _date
            ended = _date.today().isoformat()
        ended = sanitize_iso_temporal(ended, "ended")

        # Inverted-interval guard: read current valid_from values first.
        rows = self._run_cypher(
            """
            MATCH (s:Entity {name: $subj})-[r:RELATION]->(o:Entity {name: $obj})
            WHERE r.relation_type = $pred AND r.valid_to IS NULL
            RETURN r.valid_from AS valid_from
            """,
            {"subj": subject, "obj": obj, "pred": predicate},
            fetch=True,
        )
        for row in rows:
            vf = self._unwrap_agtype(row[0])
            if vf is not None and ended < vf:
                raise ValueError(
                    f"valid_to={ended!r} is before valid_from={vf!r}; "
                    "an inverted interval would be invisible to every KG query"
                )

        # Apply the invalidation. SET-on-MATCH is the supported AGE form.
        self._run_cypher(
            """
            MATCH (s:Entity {name: $subj})-[r:RELATION]->(o:Entity {name: $obj})
            WHERE r.relation_type = $pred AND r.valid_to IS NULL
            SET r.valid_to = $ended
            """,
            {"subj": subject, "obj": obj, "pred": predicate, "ended": ended},
        )
        return len(rows)

    def query_entity(
        self,
        name: str,
        as_of: Optional[str] = None,
        direction: str = "both",
    ) -> list:
        """Return all triples touching ``name`` (entity name, not id).

        Mirrors ``KnowledgeGraph.query_entity``:

        - ``direction``: "outgoing" (entity → ?), "incoming" (? → entity), "both"
        - ``as_of``: only return facts whose interval covers this date

        Each result dict has: ``direction``, ``subject``, ``predicate``,
        ``object``, ``valid_from``, ``valid_to``, ``confidence``,
        ``source_closet`` (None on AGE — not yet plumbed), ``current``.
        """
        name = sanitize_kg_value(name, "name")
        results = []

        if as_of is not None:
            as_of = sanitize_iso_temporal(as_of, "as_of")
        # Build temporal WHERE fragment if as_of given.
        temporal_where = ""
        temporal_params: dict = {}
        if as_of:
            temporal_where = (
                " AND (r.valid_from IS NULL OR r.valid_from <= $as_of)"
                " AND (r.valid_to IS NULL OR r.valid_to >= $as_of)"
            )
            temporal_params["as_of"] = as_of

        if direction in ("outgoing", "both"):
            rows = self._run_cypher(
                f"""
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                WHERE s.name = $name {temporal_where}
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to,
                       r.confidence AS confidence, r.source AS source
                """,
                {"name": name, **temporal_params},
                fetch=True,
            )
            for r in rows:
                vt = self._unwrap_agtype(r[4])
                results.append({
                    "direction": "outgoing",
                    "subject": self._unwrap_agtype(r[0]),
                    "predicate": self._unwrap_agtype(r[1]),
                    "object": self._unwrap_agtype(r[2]),
                    "valid_from": self._unwrap_agtype(r[3]),
                    "valid_to": vt,
                    "confidence": self._unwrap_agtype(r[5]),
                    "source_closet": self._unwrap_agtype(r[6]),
                    "current": vt is None,
                })

        if direction in ("incoming", "both"):
            rows = self._run_cypher(
                f"""
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                WHERE o.name = $name {temporal_where}
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to,
                       r.confidence AS confidence, r.source AS source
                """,
                {"name": name, **temporal_params},
                fetch=True,
            )
            for r in rows:
                vt = self._unwrap_agtype(r[4])
                results.append({
                    "direction": "incoming",
                    "subject": self._unwrap_agtype(r[0]),
                    "predicate": self._unwrap_agtype(r[1]),
                    "object": self._unwrap_agtype(r[2]),
                    "valid_from": self._unwrap_agtype(r[3]),
                    "valid_to": vt,
                    "confidence": self._unwrap_agtype(r[5]),
                    "source_closet": self._unwrap_agtype(r[6]),
                    "current": vt is None,
                })

        return results

    def query_relationship(self, predicate: str, as_of: Optional[str] = None) -> list:
        """Return all triples with the given relation type.

        Mirrors SQLite ``KnowledgeGraph.query_relationship``.
        """
        predicate = sanitize_kg_value(predicate, "predicate")
        if as_of is not None:
            as_of = sanitize_iso_temporal(as_of, "as_of")

        temporal_where = ""
        params = {"pred": predicate}
        if as_of:
            temporal_where = (
                " AND (r.valid_from IS NULL OR r.valid_from <= $as_of)"
                " AND (r.valid_to IS NULL OR r.valid_to >= $as_of)"
            )
            params["as_of"] = as_of

        rows = self._run_cypher(
            f"""
            MATCH (s:Entity)-[r:RELATION]->(o:Entity)
            WHERE r.relation_type = $pred {temporal_where}
            RETURN s.name AS subject, r.relation_type AS predicate,
                   o.name AS object,
                   r.valid_from AS valid_from, r.valid_to AS valid_to
            """,
            params,
            fetch=True,
        )
        return [
            {
                "subject": self._unwrap_agtype(r[0]),
                "predicate": self._unwrap_agtype(r[1]),
                "object": self._unwrap_agtype(r[2]),
                "valid_from": self._unwrap_agtype(r[3]),
                "valid_to": self._unwrap_agtype(r[4]),
                "current": self._unwrap_agtype(r[4]) is None,
            }
            for r in rows
        ]

    def timeline(self, entity_name: Optional[str] = None, limit: int = 100) -> list:
        """Return triples in chronological order, optionally filtered by entity.

        Mirrors SQLite ``KnowledgeGraph.timeline``. Limit defaults to 100
        for parity. AGE ``ORDER BY ... LIMIT`` works inside cypher() so no
        workaround needed.
        """
        if entity_name is not None:
            entity_name = sanitize_kg_value(entity_name, "entity_name")
            rows = self._run_cypher(
                """
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                WHERE s.name = $name OR o.name = $name
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to
                ORDER BY r.valid_from
                LIMIT $limit
                """,
                {"name": entity_name, "limit": limit},
                fetch=True,
            )
        else:
            rows = self._run_cypher(
                """
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to
                ORDER BY r.valid_from
                LIMIT $limit
                """,
                {"limit": limit},
                fetch=True,
            )
        return [
            {
                "subject": self._unwrap_agtype(r[0]),
                "predicate": self._unwrap_agtype(r[1]),
                "object": self._unwrap_agtype(r[2]),
                "valid_from": self._unwrap_agtype(r[3]),
                "valid_to": self._unwrap_agtype(r[4]),
                "current": self._unwrap_agtype(r[4]) is None,
            }
            for r in rows
        ]

    def add_mention(
        self,
        drawer_id: str,
        entity_name: str,
        *,
        entity_type: str = "unknown",
        count: int = 1,
        confidence: float = 0.5,
        commit: bool = True,
    ) -> None:
        """Add a (Drawer)-[:MENTIONS]->(Entity) edge.

        Connects the palace-structure layer (Drawer nodes from
        ``palace_graph_age``) to the entity layer (Entity nodes).
        MERGE pattern on the nodes — re-running for the same
        (drawer, entity) pair creates a *new parallel edge* rather
        than incrementing count.

        CREATE-ALWAYS edge semantics is intentional and matches the
        SQLite KG's triples-table behavior (each ``add_triple`` inserts
        a new row, no UPSERT). Callers that need idempotency should
        track write state externally (e.g. ``backfill_age``'s
        ``mempalace_kg_backfill_state`` table) and skip the call if the
        drawer was already processed.

        AGE 1.6.0 Cypher dialect gaps respected:
          - No SET on edge properties inline (parser errors at '=').
          - No ON CREATE SET.
          - No coalesce() in SET.
        Edge properties are set at CREATE time and never modified after.
        """
        drawer_id = sanitize_kg_value(drawer_id, "drawer_id")
        entity_name = sanitize_kg_value(entity_name, "entity_name")
        # MERGE the nodes (idempotent on identity), CREATE the edge
        # (one per call — no upsert on edges in AGE 1.6.0).
        self._run_cypher(
            """
            MERGE (d:Drawer {id: $did})
            MERGE (e:Entity {name: $ename})
            CREATE (d)-[:MENTIONS {count: $count, confidence: $conf, etype: $etype}]->(e)
            """,
            {"did": drawer_id, "ename": entity_name, "count": count,
             "conf": confidence, "etype": entity_type},
            commit=commit,
        )

    def commit(self) -> None:
        """Commit the pending KG-write transaction.

        Used by bulk-write callers (``backfill_age``) that pass
        ``commit=False`` to ``add_mention``/``_run_cypher`` to batch many
        statements into one transaction, then call ``kg.commit()`` once
        per batch. The single-statement default still commits per call.
        """
        self._conn.commit()


    def seed_from_entity_facts(self, entity_facts: dict) -> int:
        """Seed the graph from fact_checker.py ENTITY_FACTS dict.

        Mirrors SQLite ``KnowledgeGraph.seed_from_entity_facts``. ENTITY_FACTS
        is a dict of {entity_name: {fact_label: value, ...}} — each
        non-empty value becomes a (entity_name, fact_label, value) triple
        with no temporal bounds and confidence 1.0.

        Returns the number of triples written.
        """
        n = 0
        for entity, facts in (entity_facts or {}).items():
            if not isinstance(facts, dict):
                continue
            for label, value in facts.items():
                if value is None or value == "":
                    continue
                self.add_triple(
                    subject=entity,
                    relation_type=label,
                    object_=str(value),
                )
                n += 1
        return n

    def stats(self) -> dict:
        """Return aggregate counts mirroring the SQLite KG's ``stats()`` shape.

        Result keys match ``mempalace/knowledge_graph.py::KnowledgeGraph.stats``
        so callers (``tool_kg_stats``, palace-daemon's ``/graph`` panel) get
        the same envelope regardless of backend:

        - ``entities`` — total Entity nodes
        - ``triples`` — total RELATION edges (active + expired)
        - ``current_facts`` — RELATIONs with ``valid_to IS NULL`` (still active)
        - ``expired_facts`` — triples − current_facts
        - ``relationship_types`` — sorted distinct ``relation_type`` values

        Three separate Cypher round-trips (entity count, triple counts +
        current, distinct relation_types). Could be folded into one with
        WITH-clause chaining, but AGE 1.6.0's parser is fussy about
        ``count(*) WHERE``-style aggregates inside subqueries and three
        small queries keep the implementation maintainable. Performance
        is fine — AGE walks the graph once per Cypher run, all three
        complete in <50ms on the production palace's graph size.

        Implemented to close techempower-org/mempalace#96: ``tool_kg_stats``
        was throwing ``AttributeError`` on AGE-backed daemons, breaking
        palace-daemon's ``/graph`` KG panel.
        """
        entity_rows = self._run_cypher(
            "MATCH (n:Entity) RETURN count(n) AS cnt",
            {},
            fetch=True,
        )
        entities = int(self._unwrap_agtype(entity_rows[0][0])) if entity_rows else 0

        triple_rows = self._run_cypher(
            """
            MATCH ()-[r:RELATION]->()
            RETURN count(r) AS total,
                   sum(CASE WHEN r.valid_to IS NULL THEN 1 ELSE 0 END) AS current
            """,
            {},
            fetch=True,
        )
        if triple_rows:
            triples = int(self._unwrap_agtype(triple_rows[0][0]))
            current = int(self._unwrap_agtype(triple_rows[0][1]) or 0)
        else:
            triples = 0
            current = 0

        type_rows = self._run_cypher(
            """
            MATCH ()-[r:RELATION]->()
            RETURN DISTINCT r.relation_type AS rt
            """,
            {},
            fetch=True,
        )
        # Some rows may have NULL relation_type if a write predates the
        # property; drop those before sorting so callers see a clean list.
        relationship_types = sorted(
            v for v in (self._unwrap_agtype(r[0]) for r in type_rows) if isinstance(v, str)
        )

        return {
            "entities": entities,
            "triples": triples,
            "current_facts": current,
            "expired_facts": triples - current,
            "relationship_types": relationship_types,
        }

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

    def _run_cypher(
        self,
        cypher: str,
        params: dict,
        fetch: bool = False,
        commit: bool = True,
    ) -> list:
        """Run a Cypher statement via AGE.

        Inlines parameters into the Cypher source. The conventional
        ``cypher(graph, $$...$$, $1)`` AGE parameter form requires a real
        prepared-statement bind that psycopg2's %s substitution does
        not produce — AGE rejects with "third argument of cypher
        function must be a parameter" (verified on AGE 1.6.0 + Postgres
        16, 2026-05-14).

        Inlining safety relies on a layered defense:
        - subject/relation_type/object pass through ``sanitize_kg_value``
          (rejects newlines, quotes, control chars) at the public-API
          boundary in ``add_triple``/``query_triples``.
        - source/valid_from/valid_to go through their respective
          sanitize_* helpers.
        - ``_inline_cypher_params`` does single-pass substitution so a
          value containing ``$other_key`` cannot trigger a recursive
          replacement.
        - ``_cypher_literal`` rejects any string containing the outer
          dollar-quote tag (``$mp_age_q$``); combined with the tagged
          envelope below, an adversarial value cannot escape into
          surrounding SQL.

        Pass ``commit=False`` for bulk write loops (see
        ``backfill_age._BulkWriter``) so the caller can batch many
        statements into one transaction; otherwise the method commits on
        success matching the original single-statement semantics.
        """
        aliases = self._extract_return_aliases(cypher) if fetch else []
        cols_decl = ", ".join(f"{c} agtype" for c in aliases) if aliases else "ok agtype"

        cypher_inlined = _inline_cypher_params(cypher, params)

        rows: list = []
        with self._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                f"SELECT * FROM cypher(%s, {_AGE_DQ_OPEN}{cypher_inlined}{_AGE_DQ_CLOSE}) AS ({cols_decl})",
                (self.GRAPH_NAME,),
            )
            if fetch:
                rows = cur.fetchall()
        if commit:
            self._conn.commit()
        return rows

    def _cypher_scalar(self, cypher: str, params: dict, commit: bool = True) -> Any:
        """Run a Cypher query returning at most one scalar (single column).

        Workaround for AGE's single-column RETURN parsing. We rewrite the
        RETURN to include an AS alias automatically if absent — AGE requires
        AS markers on every returned column even in single-column form, and
        a separate finding was that AGE's parser sometimes fails on unaliased
        return expressions wrapped in dollar-quoted cypher().

        Returns the unwrapped scalar value or None if no rows. See
        ``_run_cypher`` for substitution + dollar-quote safety; this
        method delegates to the same helpers.
        """
        cypher_inlined = _inline_cypher_params(cypher, params)
        if " AS " not in cypher_inlined.upper():
            cypher_inlined = cypher_inlined.rstrip() + " AS v"
        with self._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                f"SELECT * FROM cypher(%s, {_AGE_DQ_OPEN}{cypher_inlined}{_AGE_DQ_CLOSE}) AS (v agtype)",
                (self.GRAPH_NAME,),
            )
            row = cur.fetchone()
        if commit:
            self._conn.commit()
        if row is None:
            return None
        return self._unwrap_agtype(row[0])

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
