-- Soft-warn canonical rooms — drop the FK on mempalace_drawers.room
-- Issue: techempower-org/mempalace#86
--
-- Background
-- ----------
-- The 2026-05-14 hybrid-search/taxonomy work added a foreign-key
-- constraint ``mempalace_drawers_room_fk`` on ``mempalace_drawers.room``
-- referencing ``mempalace_canonical_rooms.name``. This enforced the
-- canonical 7-room taxonomy at the postgres layer.
--
-- That rigid enforcement bit us twice: ``tool_diary_write`` and
-- ``palace-daemon`` hooks both hardcoded ``room="diary"`` and started
-- silently failing post-migration. Per #86 we are switching from a
-- hard FK rejection to a soft warning emitted in the write-path
-- response.
--
-- Effect
-- ------
-- After this migration, any string is accepted as a room name on
-- insert. The ``mempalace_canonical_rooms`` table is retained as a
-- registry (driving the ``mempalace rooms list/add/rename/delete``
-- CLI) but is no longer authoritative.
--
-- The Python write path (mempalace.miner.add_drawer/add_drawers and
-- mempalace.mcp_server.tool_add_drawer/tool_diary_write/
-- tool_update_drawer) now returns a ``warnings`` list when a non-
-- canonical room is written. See mempalace/room_taxonomy.py.
--
-- Apply
-- -----
--   psql "$POSTGRES_DSN" -f docs/operators/2026-05-15-drop-canonical-room-fk.sql
--
-- Idempotent: ``DROP CONSTRAINT IF EXISTS`` is a no-op when the FK is
-- already gone (e.g. on a fresh install that never had it).

BEGIN;

ALTER TABLE mempalace_drawers
    DROP CONSTRAINT IF EXISTS mempalace_drawers_room_fk;

-- Also drop the legacy constraint names that may exist on older
-- installations from the Phase 1D rollout — names varied between
-- the spec and the actual DDL. All cover the same room→canonical link.
ALTER TABLE mempalace_drawers
    DROP CONSTRAINT IF EXISTS fk_mempalace_drawers_room;
ALTER TABLE mempalace_drawers
    DROP CONSTRAINT IF EXISTS mempalace_drawers_room_canonical_fk;

COMMIT;

-- Verify: this query should return zero rows.
--
--   SELECT conname FROM pg_constraint
--   WHERE conrelid = 'mempalace_drawers'::regclass
--     AND contype = 'f'
--     AND conname ILIKE '%room%';
