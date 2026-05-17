"""KG write-through hooks for PostgresCollection drawer writes.

Phase 2 of the AGE-integration goal: every drawer write extracts entities
and adds them to the KG so retrieval can fuse vector + graph signals
without an offline backfill pass.

Hook contract (matches PostgresCollection.set_kg_writethrough):

    hook(drawer_id: str, document: str, metadata: dict) -> None

Hooks are called from inside ``_insert_rows`` *after* the drawer row
commits. They run synchronously on the writer's connection thread —
keep them fast or they slow down the write path. Exceptions are caught
upstream so a misbehaving extractor can't break ingest.

This module ships two hook factories:

- ``make_age_writethrough(kg, extractor)`` — extracts entities from each
  drawer and adds (drawer_filename → MENTIONS → entity_name) triples to
  the AGE KG via ``KnowledgeGraphAGE.add_triple``.
- ``make_null_writethrough()`` — no-op for tests / disabling.

The extractor is pluggable: pass any callable matching
``(text: str) -> list[Entity]`` where Entity has at least a ``.name``
attribute. The default is a regex-based extractor importable from the
SME repo (see sme/extractors/regex.py); production deployments can swap
in spaCy or LLM-backed extractors without touching the write-through
plumbing.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger("mempalace.kg_writethrough")


class _ExtractedEntity(Protocol):
    """Minimum surface a write-through extractor must produce per entity."""

    name: str


# Type alias for the extractor callable.
Extractor = Callable[[str], list[_ExtractedEntity]]


def make_age_writethrough(
    kg: Any,
    extractor: Extractor,
    *,
    relation_type: str = "mentions",
    confidence: float = 0.5,
    max_entities_per_drawer: int = 100,
):
    """Build a write-through hook that populates AGE from drawer writes.

    For each drawer, the hook:

    1. Runs ``extractor(document)`` to get a list of entities.
    2. For each entity (capped at ``max_entities_per_drawer``), calls
       ``kg.add_triple(subject=drawer_id, relation_type=relation_type,
       object_=entity.name, confidence=confidence)``.

    The triples land as ``(drawer_id) -[mentions]-> (entity_name)`` in
    AGE. ``drawer_id`` is typically the source filename (matches the
    ``expected_sources`` shape used in retrieval benchmarks), making the
    graph queryable as "which drawers mention X" via:

        MATCH (d:Entity)-[r:RELATION]->(e:Entity)
        WHERE e.name = $entity AND r.relation_type = 'mentions'
        RETURN d.name

    Capping at ``max_entities_per_drawer`` bounds the per-write cost;
    each add_triple is ~2-5ms on AGE (MERGE + CREATE round-trip), so a
    drawer with 100 entities adds ~250-500ms to its write. Tunable based
    on extractor verbosity vs latency budget.

    Args:
        kg: A ``KnowledgeGraphAGE`` (or any compatible KG with the same
            ``add_triple`` signature).
        extractor: Callable returning entities from text.
        relation_type: The Cypher edge label (default "mentions" matches
            the read-side fusion convention).
        confidence: Per-extraction confidence — 0.5 default reflects
            that regex extraction is high-recall but lower precision
            than e.g. LLM extraction.
        max_entities_per_drawer: Cap on entities per drawer write.

    Returns:
        A hook callable suitable for ``PostgresCollection.set_kg_writethrough``.
    """

    def hook(*, drawer_id: str, document: str, metadata: dict) -> None:
        if not document:
            return
        try:
            entities = extractor(document)
        except Exception as e:  # noqa: BLE001
            logger.warning("extractor failed for drawer %s: %s", drawer_id, e)
            return
        if not entities:
            return
        # Cap to bound per-drawer write cost.
        # Use add_mention (Drawer)-[:MENTIONS]->(Entity) rather than
        # add_triple (Entity-RELATION-Entity) so the drawer keeps its
        # :Drawer label and the palace-structure layer (Wing→Room→Drawer)
        # connects cleanly to the entity layer.
        for ent in entities[:max_entities_per_drawer]:
            try:
                kg.add_mention(
                    drawer_id=drawer_id,
                    entity_name=ent.name,
                    entity_type=getattr(ent, "type", "unknown"),
                    count=getattr(ent, "count", 1),
                    confidence=confidence,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "add_mention failed for (%s, %s): %s",
                    drawer_id, ent.name, e,
                )

    return hook


def make_null_writethrough():
    """A no-op hook. Useful for disabling KG writes in tests or rollouts
    without removing the ``set_kg_writethrough`` call from the writer
    setup path."""

    def hook(*, drawer_id: str, document: str, metadata: dict) -> None:
        return

    return hook


def make_writethrough_from_env(kg: Optional[Any] = None):
    """Build a hook based on environment variables.

    Env vars:
      MEMPALACE_KG_WRITETHROUGH=0|1     — master switch (default off)
      MEMPALACE_KG_EXTRACTOR=regex|spacy|llm|null  — choose extractor (default regex)

    Returns ``None`` if write-through is disabled. Returns a hook
    otherwise. ``kg`` is required when the master switch is on.

    Regex extractor needs an SME-repo import — kept optional so the
    mempalace package doesn't hard-require SME. If unavailable, falls
    back to a built-in tiny regex extractor (lower recall than the SME
    one but no cross-package dependency).
    """
    import os

    if os.environ.get("MEMPALACE_KG_WRITETHROUGH") not in ("1", "true", "yes"):
        return None
    if kg is None:
        raise ValueError("kg must be provided when MEMPALACE_KG_WRITETHROUGH is enabled")

    extractor_name = os.environ.get("MEMPALACE_KG_EXTRACTOR", "regex")
    if extractor_name == "regex":
        # Try SME's regex extractor first (richer two-pass impl); fall back
        # to a minimal built-in.
        try:
            from sme.extractors.regex import extract as sme_extract  # type: ignore
            extractor = sme_extract
        except ImportError:
            extractor = _builtin_regex_extractor
    elif extractor_name == "null":
        return make_null_writethrough()
    else:
        raise ValueError(
            f"unknown MEMPALACE_KG_EXTRACTOR={extractor_name!r}; "
            "supported: regex, null (spacy/llm pending)"
        )

    return make_age_writethrough(kg, extractor)


def _builtin_regex_extractor(text: str) -> list:
    """Fallback extractor when SME isn't importable.

    Catches capitalized words (proper nouns), hyphenated tech identifiers,
    and version strings. Lower recall than the SME two-pass extractor;
    sufficient as a default.
    """
    import re
    from collections import Counter
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _E:
        name: str
        type: str
        count: int

    counts: Counter[str] = Counter()
    types: dict[str, str] = {}
    # Capitalized single words, length 3+
    for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text):
        counts[w] += 1
        types.setdefault(w, "PROPER_NOUN")
    # Hyphenated lowercase identifiers
    for w in re.findall(r"\b[a-z][a-z0-9]*(?:[-_][a-z0-9]+){1,4}\b", text):
        counts[w] += 1
        types.setdefault(w, "TECH_IDENT")
    # Version strings
    for w in re.findall(r"\bv?\d+(?:\.\d+){1,3}\b", text):
        counts[w] += 1
        types.setdefault(w, "TECH_IDENT")
    return [_E(name=k.lower(), type=types[k], count=v) for k, v in counts.most_common()]
