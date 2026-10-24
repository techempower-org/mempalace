# `mempalace.dynamics`

Source: [`mempalace/dynamics.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dynamics.py)

dynamics.py — Living-connection math for halls + tunnels.

Hebbian potentiation (strength grows on co-access) and Ebbinghaus exponential
decay (strength fades with time since last activation), with the Cepeda
spacing effect: stability grows when reinforcement is spaced rather than
massed.

This module is pure. No I/O, no DB, no chromadb. It operates on plain
dicts (hall records, tunnel records); the math lives here in one place so
both connection kinds share identical semantics.

``hallways.py`` and ``palace_graph.py`` currently call only
``initialize_dynamics_fields``: hall and tunnel records get these fields
backfilled and preserved across recomputes, but nothing potentiates them on
access and nothing decays them over time. Wiring that up is a separate
question (which access counts as a co-access?); this docstring previously
said those modules invoked these functions, which was not accurate.

Read strength with ``effective_strength(connection, now=...)``. It is pure
and side-effect free, so a read cannot change a ranking.

Schema fields added to hall + tunnel records (all default-safe — existing
records without them work via ``initialize_dynamics_fields``):

    strength: float           — Hebbian connection weight, floored at STRENGTH_FLOOR,
                                capped at MAX_STRENGTH
    stability: float          — decay resistance; grows with spaced reinforcement
    last_activated: str       — ISO datetime; updates on potentiation
    access_count: int         — cumulative co-access events

Research grounding:
    - Hebb (1949): "neurons that fire together, wire together" → potentiation
    - Ebbinghaus (1885): exponential forgetting curve → apply_decay
    - Cepeda et al. (2006): spacing effect → stability growth on spaced reinforcement

## Functions

### `initialize_dynamics_fields`

```python
def initialize_dynamics_fields(connection: dict, *, now: Optional[datetime] = None) -> dict
```

Populate strength/stability/last_activated/access_count if missing.

Existing fields are NOT overwritten — this is a backfill helper for
records created before L7 dynamics shipped. Safe to call on any record;
a no-op when all fields are already present.

The ``now`` parameter is dependency injection for tests; defaults to
current UTC time. Same pattern as the rest of this module.

### `potentiate`

```python
def potentiate(connection: dict, *, increment: float = POTENTIATION_INCREMENT, now: Optional[datetime] = None) -> dict
```

Strengthen ``connection`` on a co-access event.

Updates ``strength`` (capped at ``MAX_STRENGTH``), ``last_activated``,
and ``access_count``. Grows ``stability`` by ``STABILITY_INCREMENT``
only if the gap since the prior activation is at least
``SPACED_INTERVAL_HOURS`` (the Cepeda spacing effect — rapid bursts
don't build durability; distributed practice does).

Mutates and returns the same dict for chaining. Pure aside from that
mutation — no I/O.

### `effective_strength`

```python
def effective_strength(connection: dict, *, now: Optional[datetime] = None) -> float
```

Return what ``connection``'s strength is worth at ``now``.

``strength * exp(-days_since_last_activated / stability)``, floored at
``STRENGTH_FLOOR``. Pure: the connection is not modified, so reading a
strength cannot change it.

Prefer this to ``apply_decay``. Because the stored ``strength`` stays the
value as of ``last_activated``, the answer depends only on elapsed time —
reading twice gives the same number as reading once, and there is no
maintenance pass to schedule, to forget, or to accidentally run twice.
See ``apply_decay``'s note for what happens otherwise.

``now`` is dependency injection for tests. A naive datetime is read as
UTC rather than raising: ``_parse_iso`` already forces awareness on the
stored timestamp, so without the same treatment here a caller passing
``datetime.now()`` gets a TypeError from the subtraction below.

### `apply_decay`

```python
def apply_decay(connection: dict, *, now: Optional[datetime] = None) -> dict
```

Apply Ebbinghaus exponential decay to ``connection``'s strength.

The decay model is ``new = old * exp(-days_since_last / stability)``,
floored at ``STRENGTH_FLOOR`` so connections never reach zero. Higher
stability = slower decay (the Cepeda principle: spaced reinforcement
builds durability).

Idempotent at the same instant — calling twice at the same ``now``
without a potentiation in between produces the same final strength.

NOT idempotent across different instants, and that is worth knowing
before wiring it into a maintenance pass. It multiplies the *stored*
strength while only ``potentiate`` resets ``last_activated``, so the
result depends on how many times it ran rather than on how much time
passed::

    strength 1.0, stability 5, last activated 10 days ago
    applied once at day 10        → 0.135335
    applied at day 5 and day 10   → 0.050000   (STRENGTH_FLOOR)

Two installations with identical usage would then rank memories
differently because one ran maintenance more often, and the
twice-decayed connection sits at the floor where it is
indistinguishable from one nobody has touched in a year.

``effective_strength`` computes the same curve at read time without
that hazard, and needs no maintenance pass at all.

Mutates and returns the same dict for chaining. Pure aside from that
mutation — no I/O.

``now`` is dependency injection for tests.
