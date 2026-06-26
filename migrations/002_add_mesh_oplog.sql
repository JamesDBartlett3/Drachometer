-- Adds the append-only mesh replication oplog and a global identity for
-- tool_calls. Additive and idempotent-friendly: preserves all existing rows.
--
-- The oplog is the source of truth for mesh replication. Every local write to
-- `turns` or `tool_calls` emits an immutable event here, keyed by a content
-- hash (event_id) so applying the same event twice is a no-op. `tool_calls`
-- gains a global `uid` because, unlike `turns` (which has the natural global
-- key session_id+turn_id), tool calls had no stable cross-node identity.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS oplog (
    event_id    TEXT    PRIMARY KEY,          -- sha256(entity || payload), content-addressed
    origin_node TEXT    NOT NULL,             -- node_id that first created the event
    lamport     INTEGER NOT NULL,             -- per-mesh logical clock for ordering
    created_at  TEXT    NOT NULL,             -- ISO-8601 UTC wall clock (informational)
    entity      TEXT    NOT NULL,             -- 'turn' | 'tool_call'
    op          TEXT    NOT NULL DEFAULT 'upsert',
    payload     TEXT    NOT NULL              -- canonical JSON of the row's logical fields
);

CREATE INDEX IF NOT EXISTS idx_oplog_origin_lamport ON oplog(origin_node, lamport);
CREATE INDEX IF NOT EXISTS idx_oplog_lamport        ON oplog(lamport);

-- Global identity for tool_calls. Add the column if it does not already exist,
-- then mint a random uid for every legacy row that lacks one.
ALTER TABLE tool_calls ADD COLUMN uid TEXT;
UPDATE tool_calls SET uid = lower(hex(randomblob(16))) WHERE uid IS NULL OR uid = '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_uid ON tool_calls(uid);

COMMIT;
PRAGMA foreign_keys = ON;
