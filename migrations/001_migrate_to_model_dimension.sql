-- Migrates a legacy token_usage.db schema to the model-dimension schema.
-- Intended source schema: turns has a text `model` column and no `models` table.
-- This migration is additive and preserves all existing turn/tool_call rows.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS models (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key                     TEXT    NOT NULL UNIQUE,
    model_name                    TEXT,
    model_version                 TEXT,
    model_provider                TEXT,
    input_price_per_mtok          REAL,
    output_price_per_mtok         REAL,
    cache_read_price_per_mtok     REAL,
    cache_creation_price_per_mtok REAL
);

ALTER TABLE turns ADD COLUMN model_id INTEGER REFERENCES models(id);

INSERT INTO models (model_key)
SELECT DISTINCT TRIM(model) AS model_key
FROM turns
WHERE model IS NOT NULL
  AND TRIM(model) <> ''
  AND NOT EXISTS (
      SELECT 1
      FROM models m
      WHERE m.model_key = TRIM(turns.model)
  );

UPDATE turns
SET model_id = (
    SELECT m.id
    FROM models m
    WHERE m.model_key = TRIM(turns.model)
)
WHERE model_id IS NULL
  AND model IS NOT NULL
  AND TRIM(model) <> '';

CREATE INDEX IF NOT EXISTS idx_turns_model_id ON turns(model_id);

COMMIT;
PRAGMA foreign_keys = ON;
