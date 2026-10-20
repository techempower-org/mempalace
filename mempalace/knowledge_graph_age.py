"""AGE-backed implementation of KnowledgeGraph (Apache AGE on Postgres).

Companion to `mempalace.knowledge_graph.KnowledgeGraph` (SQLite). Selectable
via `MEMPALACE_KG_BACKEND=age` once the config-routing layer is wired up.
Mirrors the public interface of the SQLite KG so callers can swap backends
without code changes.

The graph itself is `mempalace_kg` registered in AGE's `ag_catalog`. It is
created on first init and reused thereafter — initialization is idempotent.
"""

import json
import logging
import re
import threading
from typing import Any, Optional

from .config import sanitize_iso_temporal, sanitize_kg_value

logger = logging.getLogger(__name__)


def _load_psycopg2():
    """Lazy import so pure-Python helpers (e.g. _cypher_literal) don't
    require the optional [postgres] extra. KnowledgeGraphAGE itself
    obviously needs it; the import fires at __init__ time.

    Name preserved for monkeypatch compatibility; driver is now psycopg3
    (``import psycopg``) — see ``mempalace.backends.postgres._load_psycopg2``.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "AGE knowledge-graph backend requires optional dependencies. "
            'Install with: pip install "mempalace[postgres]"'
        ) from exc
    return psycopg


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


# AGE graph names are unquoted SQL identifiers. cypher(name, ...) requires
# a *name constant* (literal) — psycopg3's %s binds it as a server-side
# parameter that AGE rejects ("a name constant is expected"). We render
# the graph name into the SQL text. Tight identifier regex prevents
# anything but [A-Za-z_][A-Za-z0-9_]* even if a future caller passes
# something exotic.
_AGE_GRAPH_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _compose_cypher_sql(graph_name: str, cypher_body: str, cols_decl: str) -> str:
    """Build ``SELECT * FROM cypher('<graph>', $$<body>$$) AS (<cols>)``.

    AGE expects the first argument of ``cypher()`` to be a single-quoted
    string literal — what AGE calls a "name constant". psycopg3 binds
    ``%s`` as a server-side ``$1`` parameter (extended query protocol)
    which AGE rejects. We render the graph name inline as a quoted
    literal so psycopg3 sends a simple-query without binds.

    The Cypher body is already inside a dollar-quoted envelope
    (``_AGE_DQ_OPEN``/``_AGE_DQ_CLOSE``) and contains only sanitized
    literals — see ``_inline_cypher_params`` and ``_cypher_literal``.
    """
    if not _AGE_GRAPH_NAME_RE.match(graph_name):
        raise ValueError(f"invalid AGE graph name: {graph_name!r}")
    return (
        f"SELECT * FROM cypher('{graph_name}', "
        f"{_AGE_DQ_OPEN}{cypher_body}{_AGE_DQ_CLOSE}) "
        f"AS ({cols_decl})"
    )


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
        self._dsn = dsn
        self._conn = None
        self._connect()
        # Serialize transaction spans across threads. The single ``self._conn``
        # is shared whenever one KnowledgeGraphAGE instance is reached from
        # multiple threads — e.g. the inline KG write-through hook
        # (``MEMPALACE_KG_WRITETHROUGH=1``) runs synchronously on the daemon's
        # write threadpool, so two concurrent writes share this connection.
        # psycopg3's per-connection lock makes each ``execute``/``commit`` atomic
        # on the wire, but with ``autocommit = False`` it does NOT isolate the
        # *transaction span*: thread A's CREATE and thread B's CREATE land in one
        # shared transaction, and whichever ``commit()`` / ``rollback()`` fires
        # first decides both — silently dropping or mis-committing the other
        # thread's writes. An RLock around each public read/write span restores
        # one-transaction-per-thread semantics. Reentrant so a guarded method
        # (e.g. ``add_triple``) can call another guarded helper (``_run_cypher``)
        # on the same thread without deadlocking. Mirrors the ``self._lock``
        # already present on the SQLite ``KnowledgeGraph`` (knowledge_graph.py).
        self._lock = threading.RLock()
        self._age_loaded = False
        self._ensure_graph()

    # ------------------------------------------------------------------
    # Connection lifecycle (#405)
    # ------------------------------------------------------------------
    #
    # This instance is cached process-wide by the MCP server (``_call_kg``)
    # and by the daemon, so ``self._conn`` outlives any single request. Two
    # things used to poison it permanently until a daemon restart:
    #
    # * a statement error (``QueryCanceled`` from a ``statement_timeout``,
    #   ``DiskFull`` …) left the transaction aborted with no ``rollback()``,
    #   so every later statement failed; and
    # * a dead socket (host suspend/resume, server restart) left ``closed``
    #   set, so every later ``cursor()`` raised ``the connection is closed``.
    #
    # Live measurement 2026-09-03: one slow walk took KG reads down for
    # every client on the host, three times in one day. The guards below
    # roll back after statement errors and reconnect once after
    # connection errors, so a bad query costs one query, not the service.

    def _connect(self) -> None:
        psycopg2 = _load_psycopg2()
        self._conn = psycopg2.connect(self._dsn)
        # KG writes need explicit commit semantics, not autocommit — keep
        # the same shape as the SQLite KG so the eventual unified write
        # API can be swapped underneath. The bootstrap commits its own
        # changes; subsequent write operations control their own
        # transactions.
        self._conn.autocommit = False
        self._age_loaded = False

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _ensure_conn(self) -> None:
        """Reconnect if the cached connection is gone (host slept, server bounced)."""
        conn = self._conn
        if conn is None or getattr(conn, "closed", 0):
            self._connect()

    @staticmethod
    def _is_connection_error(exc: BaseException) -> bool:
        psycopg2 = _load_psycopg2()
        conn_errors = tuple(
            getattr(psycopg2, name)
            for name in ("OperationalError", "InterfaceError")
            if hasattr(psycopg2, name)
        )
        if conn_errors and isinstance(exc, conn_errors):
            return True
        msg = str(exc)
        return "connection is closed" in msg or "server closed the connection" in msg

    def _rollback_quietly(self) -> None:
        """Leave the connection usable after a statement-level error.

        psycopg2 marks the transaction aborted after any error; without a
        rollback every subsequent statement fails with
        ``InFailedSqlTransaction`` — which is how one cancelled query used to
        take down every later KG read.
        """
        conn = self._conn
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception:
            self._drop_conn()

    def _with_conn_retry(self, fn):
        """Run ``fn()`` against a live connection; reconnect once on a dead one.

        Statement-level DB errors are rolled back and re-raised unchanged
        (the caller's query was wrong or too slow — the connection is fine).
        Connection-level errors drop the socket, reconnect, and retry once.
        """
        psycopg2 = _load_psycopg2()
        db_error = getattr(psycopg2, "Error", Exception)
        last_exc = None
        for attempt in (1, 2):
            self._ensure_conn()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 — classified below
                last_exc = exc
                if self._is_connection_error(exc):
                    self._drop_conn()
                    if attempt == 2:
                        raise
                    continue
                if isinstance(exc, db_error):
                    self._rollback_quietly()
                raise
        raise last_exc  # pragma: no cover — loop always returns or raises

    def _ensure_graph(self) -> None:
        """Idempotent: load AGE, set search_path, create the graph if absent.

        Both `LOAD 'age'` and the `search_path` setting are session-scoped
        — anything new that takes a fresh cursor on this connection must
        re-run them before issuing Cypher. The pattern in this module is
        to always wrap `LOAD 'age'; SET search_path = ag_catalog, "$user",
        public` around each cypher block; for the bootstrap that's done
        here, downstream methods will repeat as needed.

        Also installs a unique index on ``Drawer.properties->>'id'``. AGE
        ``MERGE (d:Drawer {id: X})`` has a read-then-create gap that is
        not atomic — two writers MERGE'ing the same id concurrently both
        miss and both CREATE, producing duplicate Drawer nodes. The index
        makes the second CREATE fail at the Postgres layer; callers must
        catch UniqueViolation and retry as a MATCH. Postgres allows a
        ``CREATE UNIQUE INDEX IF NOT EXISTS`` on an existing populated
        table only if no duplicates exist, so the migration sequence is:
        dedup first, then run setup.
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
        self._ensure_drawer_unique_index()

    def _ensure_drawer_unique_index(self) -> None:
        """Install the Drawer.id unique index if the table exists and is dedup'd.

        Skips silently if (a) the Drawer label hasn't been created yet
        (no writethrough or backfill has fired), or (b) the table still
        has duplicate ids (cleanup hasn't run). Logs at debug for both.
        Callers can re-invoke after dedup to install the index then.
        """
        graph = self.GRAPH_NAME
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'Drawer'",
                (graph,),
            )
            if cur.fetchone() is None:
                return
            # Already installed: nothing to check. The duplicate count below
            # is a COUNT(DISTINCT) over every Drawer row (1.2M rows, 401 MB
            # on familiar 2026-10-20), and it ran on every KG setup.
            cur.execute(
                "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = 'drawer_id_unique'",
                (graph,),
            )
            if cur.fetchone() is not None:
                return
            cur.execute(
                f"SELECT COUNT(*) - COUNT(DISTINCT (properties::text)::jsonb->>'id') "
                f'FROM "{graph}"."Drawer"'
            )
            dup_excess = cur.fetchone()[0]
            if dup_excess and dup_excess > 0:
                logger.debug(
                    "skipping Drawer unique index: %d duplicate rows exist; run dedup first",
                    dup_excess,
                )
                return
            cur.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS drawer_id_unique "
                f'ON "{graph}"."Drawer" (((properties::text)::jsonb->>\'id\'))'
            )
        self._conn.commit()

    # Edge-endpoint indexes the graph-walk retrieval paths join on. AGE only
    # btree-indexes each label table's own ``id`` (``_ag_label_edge_pkey``),
    # never the ``start_id`` / ``end_id`` graphid columns. Without these, the
    # per-entity Cypher in ``searcher._graph_expand_*`` and the daemon's
    # ``/search/age-fused`` lookup parallel-seq-scans the entire edge table
    # (MENTIONS is multi-million-row on a full palace) — ~5.8s cold for a hot
    # entity. The index turns that into a bounded bitmap index scan. See
    # palace-daemon scripts/age_graph_indexes.sql + the
    # 2026-05-30 hybrid-graph-walk-latency perf note (mempalace#335). Keep the
    # index names + columns in sync with that SQL file.
    _EDGE_ENDPOINT_INDEXES: tuple[tuple[str, str, str], ...] = (
        ("idx_mentions_end_id", "MENTIONS", "end_id"),
        ("idx_mentions_start_id", "MENTIONS", "start_id"),
        ("idx_relation_start_id", "RELATION", "start_id"),
        ("idx_relation_end_id", "RELATION", "end_id"),
    )

    def _ensure_edge_endpoint_indexes(self) -> None:
        """Install btree indexes on MENTIONS/RELATION (start_id, end_id).

        Mirrors :meth:`_ensure_drawer_unique_index`: skips silently for any
        edge label whose backing table doesn't exist yet (the label is only
        created on the first edge write, so a palace that has never had a
        MENTIONS or RELATION edge has no table to index). Idempotent —
        ``CREATE INDEX IF NOT EXISTS`` — so a later call after edges land
        installs the missing ones.

        Non-concurrent: intended to run at the end of a backfill (or any
        bootstrap) where a brief lock on the edge table is acceptable. The
        operator-online path (rebuild against a live daemon) uses
        ``CREATE INDEX CONCURRENTLY`` via palace-daemon's
        ``POST /backfill-age/indexes`` instead.
        """
        graph = self.GRAPH_NAME
        with self._conn.cursor() as cur:
            for index_name, label, column in self._EDGE_ENDPOINT_INDEXES:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    (graph, label),
                )
                if cur.fetchone() is None:
                    logger.debug(
                        "skipping %s: %s.%s edge table does not exist yet",
                        index_name,
                        graph,
                        label,
                    )
                    continue
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS {index_name} ON "{graph}"."{label}" ({column})'
                )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying Postgres connection."""
        if self._conn is not None and not getattr(self._conn, "closed", 0):
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
        with self._lock:
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
        context: Optional[str] = None,
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

        ``context`` is the SPOC fourth-axis: a free-form anchor naming
        where this fact was witnessed (e.g. ``drawer:abc123``,
        ``conversation:2026-05-28``). Distinct from ``source`` (which
        carries provenance for human-curated edits) in that ``context`` is
        meant to be set by the extraction pipeline on every auto-derived
        triple. Stored as an optional property and surfaced through every
        read path. See techempower-org/mempalace#161.
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
        if context is not None:
            context = sanitize_kg_value(context, "context")

        # Build the property map keys dynamically — a Cypher property map
        # rejects bare ``NULL`` as a value (``SyntaxError: a name constant
        # is expected``), so omit any key whose value is None. The omitted
        # property reads back as NULL via ``r.source``/``r.valid_from`` etc.,
        # which matches the open-interval semantics the rest of the API
        # already assumes (see ``query_triples`` as_of filter).
        prop_pairs = [
            "relation_type: $rt",
            "confidence: $conf",
        ]
        params = {
            "subj": subject,
            "obj": object_,
            "rt": relation_type,
            "conf": confidence,
        }
        if source is not None:
            prop_pairs.append("source: $src")
            params["src"] = source
        if valid_from is not None:
            prop_pairs.append("valid_from: $vf")
            params["vf"] = valid_from
        if valid_to is not None:
            prop_pairs.append("valid_to: $vt")
            params["vt"] = valid_to
        if context is not None:
            prop_pairs.append("context: $ctx")
            params["ctx"] = context

        cypher = f"""
            MERGE (s:Entity {{name: $subj}})
            MERGE (o:Entity {{name: $obj}})
            CREATE (s)-[r:RELATION {{
                {", ".join(prop_pairs)}
            }}]->(o)
        """
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
        confidence, context``. ``context`` is the SPOC anchor (see
        ``add_triple``); ``None`` for triples written before the slot was
        introduced.
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
                   r.confidence AS confidence, r.context AS context
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
                "context": self._unwrap_agtype(r[7]),
            }
            for r in rows
        ]

    def add_entity(
        self, name: str, entity_type: str = "unknown", properties: Optional[dict] = None
    ) -> str:
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

    def invalidate(
        self, subject: str, predicate: str, obj: str, ended: Optional[str] = None
    ) -> int:
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
                       r.confidence AS confidence, r.source AS source,
                       r.context AS context
                """,
                {"name": name, **temporal_params},
                fetch=True,
            )
            for r in rows:
                vt = self._unwrap_agtype(r[4])
                results.append(
                    {
                        "direction": "outgoing",
                        "subject": self._unwrap_agtype(r[0]),
                        "predicate": self._unwrap_agtype(r[1]),
                        "object": self._unwrap_agtype(r[2]),
                        "valid_from": self._unwrap_agtype(r[3]),
                        "valid_to": vt,
                        "confidence": self._unwrap_agtype(r[5]),
                        "source_closet": self._unwrap_agtype(r[6]),
                        "context": self._unwrap_agtype(r[7]),
                        "current": vt is None,
                    }
                )

        if direction in ("incoming", "both"):
            rows = self._run_cypher(
                f"""
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                WHERE o.name = $name {temporal_where}
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to,
                       r.confidence AS confidence, r.source AS source,
                       r.context AS context
                """,
                {"name": name, **temporal_params},
                fetch=True,
            )
            for r in rows:
                vt = self._unwrap_agtype(r[4])
                results.append(
                    {
                        "direction": "incoming",
                        "subject": self._unwrap_agtype(r[0]),
                        "predicate": self._unwrap_agtype(r[1]),
                        "object": self._unwrap_agtype(r[2]),
                        "valid_from": self._unwrap_agtype(r[3]),
                        "valid_to": vt,
                        "confidence": self._unwrap_agtype(r[5]),
                        "source_closet": self._unwrap_agtype(r[6]),
                        "context": self._unwrap_agtype(r[7]),
                        "current": vt is None,
                    }
                )

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
                   r.valid_from AS valid_from, r.valid_to AS valid_to,
                   r.context AS context
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
                "context": self._unwrap_agtype(r[5]),
                "current": self._unwrap_agtype(r[4]) is None,
            }
            for r in rows
        ]

    def timeline(
        self,
        entity_name: Optional[str] = None,
        limit: int = 100,
        as_of: Optional[str] = None,
    ) -> list:
        """Return triples in chronological order, optionally filtered by entity.

        Mirrors SQLite ``KnowledgeGraph.timeline``. Limit defaults to 100
        for parity. AGE ``ORDER BY ... LIMIT`` works inside cypher() so no
        workaround needed.

        ``as_of`` (P5 / #161) filters to triples whose temporal interval
        contains the given date; NULL ends are treated as open intervals
        (same semantics as ``query_triples``). Default (None) returns the
        full timeline including expired facts.
        """
        if as_of is not None:
            as_of = sanitize_iso_temporal(as_of, "as_of")
        temporal_where = ""
        temporal_params: dict = {}
        if as_of is not None:
            temporal_where = (
                " AND (r.valid_from IS NULL OR r.valid_from <= $as_of)"
                " AND (r.valid_to IS NULL OR r.valid_to >= $as_of)"
            )
            temporal_params["as_of"] = as_of

        if entity_name is not None:
            entity_name = sanitize_kg_value(entity_name, "entity_name")
            rows = self._run_cypher(
                f"""
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                WHERE (s.name = $name OR o.name = $name) {temporal_where}
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to,
                       r.context AS context
                ORDER BY r.valid_from
                LIMIT $limit
                """,
                {"name": entity_name, "limit": limit, **temporal_params},
                fetch=True,
            )
        else:
            where_clause = f"WHERE 1=1 {temporal_where}".rstrip() if temporal_where else ""
            rows = self._run_cypher(
                f"""
                MATCH (s:Entity)-[r:RELATION]->(o:Entity)
                {where_clause}
                RETURN s.name AS subject, r.relation_type AS predicate,
                       o.name AS object,
                       r.valid_from AS valid_from, r.valid_to AS valid_to,
                       r.context AS context
                ORDER BY r.valid_from
                LIMIT $limit
                """,
                {"limit": limit, **temporal_params},
                fetch=True,
            )
        return [
            {
                "subject": self._unwrap_agtype(r[0]),
                "predicate": self._unwrap_agtype(r[1]),
                "object": self._unwrap_agtype(r[2]),
                "valid_from": self._unwrap_agtype(r[3]),
                "valid_to": self._unwrap_agtype(r[4]),
                "context": self._unwrap_agtype(r[5]),
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
            {
                "did": drawer_id,
                "ename": entity_name,
                "count": count,
                "conf": confidence,
                "etype": entity_type,
            },
            commit=commit,
        )

    def commit(self) -> None:
        """Commit the pending KG-write transaction.

        Used by bulk-write callers (``backfill_age``) that pass
        ``commit=False`` to ``add_mention``/``_run_cypher`` to batch many
        statements into one transaction, then call ``kg.commit()`` once
        per batch. The single-statement default still commits per call.
        """
        with self._lock:
            self._conn.commit()

    def delete_drawer(self, drawer_id: str, *, commit: bool = True) -> int:
        """Remove a Drawer node and all its edges from the AGE graph.

        Returns the number of Drawer nodes removed (0 if the id wasn't
        present, ≥1 if dedup hasn't run and there are duplicates).
        Idempotent — calling on an already-deleted id is a no-op.

        Used by the delete-through hook so drawer deletes propagate to
        the graph; without it, orphan Drawer nodes accumulate over time
        (one per delete since 2026-05-22 AGE rollout).
        """
        drawer_id = sanitize_kg_value(drawer_id, "drawer_id")
        rows = self._run_cypher(
            """
            MATCH (d:Drawer {id: $did})
            DETACH DELETE d
            RETURN 1 AS deleted
            """,
            {"did": drawer_id},
            commit=commit,
        )
        return len(rows)

    def delete_drawers(self, drawer_ids: list, *, commit: bool = True) -> int:
        """Batched delete via UNWIND. Returns total Drawer nodes removed.

        Use when removing many drawers in one logical transaction — one
        Cypher round-trip instead of N — mirroring the UNWIND batching
        ``backfill_age`` uses for adds (commit ``4fbd8c6``).
        """
        if not drawer_ids:
            return 0
        ids = [sanitize_kg_value(d, "drawer_id") for d in drawer_ids]
        rows = self._run_cypher(
            """
            UNWIND $ids AS did
            MATCH (d:Drawer {id: did})
            DETACH DELETE d
            RETURN 1 AS deleted
            """,
            {"ids": ids},
            commit=commit,
        )
        return len(rows)

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

        Tries ``_stats_fast`` first (SQL against the AGE backing label
        tables — sub-ms on ≥1M edges because Postgres serves plain
        ``count(*)`` straight off the visibility map without unwrapping
        every value through agtype). Falls back to ``_stats_cypher`` when
        the backing tables aren't available yet (fresh palace, label not
        yet created, or non-standard AGE setup). The fallback runs three
        full graph walks and is the slow path the issue measures at ~9s
        on the production palace at ~1M entities / ~1.6M RELATION edges.

        Implemented to close techempower-org/mempalace#96 (envelope
        shape) and #266 (performance — replaces the ~9s slow path with
        sub-100ms).
        """
        # Lock the whole fast→fallback span: _stats_fast commits on this
        # shared connection and the except path rolls it back, both of which
        # would clobber a concurrent writer's transaction without the guard.
        with self._lock:
            try:
                return self._stats_fast()
            except Exception as exc:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                logger.debug("stats() fast path unavailable, falling back to Cypher: %s", exc)
                return self._stats_cypher()

    def _stats_fast(self) -> dict:
        """Backing-table fast path for ``stats()``.

        Ported from palace-daemon ``f56e238`` — AGE registers each label
        as a regular Postgres table under the graph's schema
        (``mempalace_kg."Entity"``, ``mempalace_kg."RELATION"``, ...),
        so a plain ``SELECT count(*)`` hits the visibility map without
        materializing agtype-wrapped rows. Identifier case is preserved
        by AGE so the quoted-identifier names are mandatory; lowercase
        names 404.

        ``current_facts`` and ``relationship_types`` use the agtype
        ``->>`` operator to read the RELATION properties column as JSON.
        Both are wrapped in savepoints so a missing-operator error (older
        AGE without agtype ``->>`` support) only loses the savepoint, not
        the outer transaction, and we transparently fall through to a
        single Cypher walk for that one field.

        Raises on backing-table miss (e.g. label not yet created on a
        fresh palace) so the outer ``stats()`` method can fall back to
        the full-Cypher path.
        """
        graph = self.GRAPH_NAME
        with self._conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{graph}"."Entity"')
            row = cur.fetchone()
            entities = int(row[0]) if row else 0

            cur.execute(f'SELECT count(*) FROM "{graph}"."RELATION"')
            row = cur.fetchone()
            triples = int(row[0]) if row else 0

            # current_facts: prefer agtype ->> cast; on operator-missing
            # error roll the savepoint back and fall through to Cypher
            # for this field only.
            if triples == 0:
                current = 0
            else:
                cur.execute("SAVEPOINT stats_current")
                try:
                    cur.execute(
                        f'SELECT count(*) FROM "{graph}"."RELATION" '
                        "WHERE (properties->>'valid_to') IS NULL"
                    )
                    row = cur.fetchone()
                    current = int(row[0]) if row else 0
                    cur.execute("RELEASE SAVEPOINT stats_current")
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT stats_current")
                    cur.execute("RELEASE SAVEPOINT stats_current")
                    logger.debug(
                        "agtype ->> unavailable for current_facts, falling back to Cypher: %s",
                        exc,
                    )
                    current = self._current_facts_cypher()

            # relationship_types: same savepoint pattern.
            if triples == 0:
                relationship_types: list[str] = []
            else:
                cur.execute("SAVEPOINT stats_types")
                try:
                    cur.execute(
                        f"SELECT DISTINCT (properties->>'relation_type') "
                        f'FROM "{graph}"."RELATION"'
                    )
                    rows = cur.fetchall()
                    relationship_types = sorted(r[0] for r in rows if isinstance(r[0], str))
                    cur.execute("RELEASE SAVEPOINT stats_types")
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT stats_types")
                    cur.execute("RELEASE SAVEPOINT stats_types")
                    logger.debug(
                        "agtype ->> unavailable for relation_type, falling back to Cypher: %s",
                        exc,
                    )
                    relationship_types = self._relationship_types_cypher()
        self._conn.commit()

        return {
            "entities": entities,
            "triples": triples,
            "current_facts": current,
            "expired_facts": triples - current,
            "relationship_types": relationship_types,
        }

    def _current_facts_cypher(self) -> int:
        """Cypher fallback for ``current_facts`` when the agtype ``->>``
        cast isn't supported. One graph walk instead of three — still the
        expensive operation but bounded to a single field."""
        rows = self._run_cypher(
            """
            MATCH ()-[r:RELATION]->()
            WHERE r.valid_to IS NULL
            RETURN count(r) AS cnt
            """,
            {},
            fetch=True,
        )
        return int(self._unwrap_agtype(rows[0][0])) if rows else 0

    def _relationship_types_cypher(self) -> list:
        """Cypher fallback for ``relationship_types`` when agtype ``->>``
        isn't supported. One graph walk, returns sorted distinct values
        with NULLs dropped (matches the SQL path semantics)."""
        rows = self._run_cypher(
            """
            MATCH ()-[r:RELATION]->()
            RETURN DISTINCT r.relation_type AS rt
            """,
            {},
            fetch=True,
        )
        return sorted(v for v in (self._unwrap_agtype(r[0]) for r in rows) if isinstance(v, str))

    def _stats_cypher(self) -> dict:
        """Original three-Cypher-walk stats path, retained as the
        outermost fallback when the backing tables aren't accessible
        (fresh palace where the label hasn't been created yet, or a
        non-standard AGE setup). Three separate round-trips because AGE
        1.6.0's parser is fussy about ``count(*) WHERE``-style aggregates
        inside subqueries."""
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

        relationship_types = self._relationship_types_cypher()

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

        # Hold the lock across the whole cursor → execute → commit span so a
        # concurrent thread sharing this connection cannot interleave its own
        # statements into our transaction (see __init__). Reentrant: public
        # callers already holding the lock pass straight through.
        def _execute() -> list:
            rows: list = []
            with self._conn.cursor() as cur:
                if not self._age_loaded:
                    cur.execute("LOAD 'age'")
                    cur.execute('SET search_path = ag_catalog, "$user", public')
                    self._age_loaded = True
                cur.execute(_compose_cypher_sql(self.GRAPH_NAME, cypher_inlined, cols_decl))
                if fetch:
                    rows = cur.fetchall()
            if commit:
                self._conn.commit()
            return rows

        with self._lock:
            return self._with_conn_retry(_execute)

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

        # Lock the cursor→execute→commit span (see _run_cypher / __init__).
        def _execute():
            with self._conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                cur.execute(_compose_cypher_sql(self.GRAPH_NAME, cypher_inlined, "v agtype"))
                fetched = cur.fetchone()
            if commit:
                self._conn.commit()
            return fetched

        with self._lock:
            row = self._with_conn_retry(_execute)
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
