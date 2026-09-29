#!/usr/bin/env python3
"""Shared helpers for drachometer components (hook, mesh, installer, server).

Single source of truth for the pieces that used to be copy-pasted (and had
already drifted) across the hook, the mesh module, and the installer:

- the offline fallback model-tier pricing and the drachometer-pricing.json
  overlay kept fresh by the update-pricing workflow,
- model attribute inference (tier, display name, version, provider, prices),
- the base SQLite schema (models / turns / tool_calls / mesh oplog).

Every component imports this module from the install directory (the installer
copies all files into one place), so the copies can never diverge again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

# Offline fallback pricing (USD per 1M tokens), kept in sync with
# drachometer-pricing.json. Overlaid at import by the values from the pricing
# file installed alongside this module so newly-logged models are priced from
# the latest published rates.
MODEL_TIER_PRICING: dict[str, dict[str, float]] = {
    "fable":  {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_create": 12.5},
    "opus":   {"input": 4.0,  "output": 20.0, "cache_read": 0.4, "cache_create": 5.0},
    "sonnet": {"input": 2.0,  "output": 10.0, "cache_read": 0.2, "cache_create": 2.5},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.1, "cache_create": 1.25},
}

PRICING_KEYS = ("input", "output", "cache_read", "cache_create")

# Bumped whenever ensure_base_schema() learns something new; the hook checks
# PRAGMA user_version first so it can skip the (much larger) DDL path on every
# invocation of a database this version of the schema has already prepared.
SCHEMA_USER_VERSION = 1

MODEL_INSERT_SQL = """
INSERT INTO models (
    model_key, model_name, model_version, model_provider,
    input_price_per_mtok, output_price_per_mtok,
    cache_read_price_per_mtok, cache_creation_price_per_mtok
) VALUES (
    :model_key, :model_name, :model_version, :model_provider,
    :input_price_per_mtok, :output_price_per_mtok,
    :cache_read_price_per_mtok, :cache_creation_price_per_mtok
)
"""

# Full base schema: the usage tables plus the mesh oplog (empty and harmless
# when mesh is disabled).
BASE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS models (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key                    TEXT    NOT NULL UNIQUE,
    model_name                   TEXT,
    model_version                TEXT,
    model_provider               TEXT,
    input_price_per_mtok         REAL,
    output_price_per_mtok        REAL,
    cache_read_price_per_mtok    REAL,
    cache_creation_price_per_mtok REAL
);

CREATE TABLE IF NOT EXISTS turns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT    NOT NULL,
    turn_id               TEXT    NOT NULL,
    recorded_at           TEXT    NOT NULL,
    stop_reason           TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    model_id              INTEGER REFERENCES models(id),
    UNIQUE(session_id, turn_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_pk     INTEGER REFERENCES turns(id) ON DELETE CASCADE,
    session_id  TEXT    NOT NULL,
    turn_id     TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL,
    tool_name   TEXT,
    tool_input  TEXT,
    exit_code   INTEGER,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_turns_recorded_at ON turns(recorded_at);
CREATE INDEX IF NOT EXISTS idx_calls_turn_pk ON tool_calls(turn_pk);
CREATE INDEX IF NOT EXISTS idx_calls_session ON tool_calls(session_id, turn_id);

CREATE TABLE IF NOT EXISTS oplog (
    event_id    TEXT    PRIMARY KEY,
    origin_node TEXT    NOT NULL,
    lamport     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    entity      TEXT    NOT NULL,
    op          TEXT    NOT NULL DEFAULT 'upsert',
    payload     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oplog_origin_lamport ON oplog(origin_node, lamport);
CREATE INDEX IF NOT EXISTS idx_oplog_lamport        ON oplog(lamport);
"""

# Columns added after the schema above first shipped; ALTER ... ADD COLUMN is
# idempotent-by-exception because SQLite has no IF NOT EXISTS for columns.
TURNS_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("cwd", "TEXT"),
    ("git_branch", "TEXT"),
    ("model", "TEXT"),
    ("model_id", "INTEGER REFERENCES models(id)"),
]


def load_pricing_overrides(path: Path | None = None) -> None:
    """Overlay MODEL_TIER_PRICING with drachometer-pricing.json.

    A tier only overrides its fallback when all four price keys are present and
    numeric; a partially-corrupt or half-updated file must not be able to price
    some dimensions and silently leave others as NULL.
    """
    pricing_path = path or Path(__file__).resolve().parent / "drachometer-pricing.json"
    try:
        data = json.loads(pricing_path.read_text(encoding="utf-8"))
        tiers = data.get("tiers", data)
        if not isinstance(tiers, dict):
            return
        for tier, prices in tiers.items():
            if isinstance(prices, dict) and all(
                isinstance(prices.get(key), (int, float)) for key in PRICING_KEYS
            ):
                MODEL_TIER_PRICING[tier] = {key: float(prices[key]) for key in PRICING_KEYS}
    except (OSError, json.JSONDecodeError, ValueError):
        pass


load_pricing_overrides()


def infer_model_attributes(model_key: str | None) -> dict:
    """Derive display/pricing attributes for a model key (e.g. 'claude-opus-5-5')."""
    key = (model_key or "").strip()
    lower = key.lower()
    if not key:
        return {
            "model_name": None,
            "model_version": None,
            "model_provider": None,
            "input_price_per_mtok": None,
            "output_price_per_mtok": None,
            "cache_read_price_per_mtok": None,
            "cache_creation_price_per_mtok": None,
        }

    if "fable" in lower:
        tier = "fable"
    elif "opus" in lower:
        tier = "opus"
    elif "sonnet" in lower:
        tier = "sonnet"
    elif "haiku" in lower:
        tier = "haiku"
    else:
        tier = None

    parts = [p for p in key.split("-") if p]
    model_name = (
        " ".join(parts[:2]).title()
        if len(parts) >= 2 and parts[0].lower() == "claude"
        else (parts[0].title() if parts else None)
    )
    version_match = re.search(r"(\d+(?:[-.]\d+)*(?:-\d{8})?)", key)
    pricing = MODEL_TIER_PRICING.get(tier or "", {})
    return {
        "model_name": model_name,
        "model_version": version_match.group(1) if version_match else None,
        "model_provider": "Anthropic" if lower.startswith("claude") else None,
        "input_price_per_mtok": pricing.get("input"),
        "output_price_per_mtok": pricing.get("output"),
        "cache_read_price_per_mtok": pricing.get("cache_read"),
        "cache_creation_price_per_mtok": pricing.get("cache_create"),
    }


def insert_model_row(conn: sqlite3.Connection, model_key: str, attrs: dict) -> int:
    cur = conn.execute(MODEL_INSERT_SQL, {"model_key": model_key, **attrs})
    return cur.lastrowid


def ensure_model_row(conn: sqlite3.Connection, model_key: str | None) -> int | None:
    """Return the models.id for ``model_key``, creating a priced row if needed."""
    key = (model_key or "").strip()
    if not key:
        return None
    row = conn.execute("SELECT id FROM models WHERE model_key = ?", (key,)).fetchone()
    if row:
        return row[0]
    return insert_model_row(conn, key, infer_model_attributes(key))


def ensure_base_schema(conn: sqlite3.Connection, backfill_model_ids: bool = True) -> None:
    """Create/upgrade the base schema and stamp PRAGMA user_version.

    Idempotent and safe to run on an empty database, a legacy pre-mesh
    database, or a fully current one. Pass ``backfill_model_ids=False`` to
    skip linking legacy inline model names (the installer does that itself so
    it can prompt the user for prices of unknown models).
    """
    conn.executescript(BASE_SCHEMA_DDL)
    for col, typedef in TURNS_EXTRA_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE tool_calls ADD COLUMN uid TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_model_id ON turns(model_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_uid ON tool_calls(uid)")
    # Legacy databases logged model names inline on turns; give them the model
    # dimension so per-model pricing works everywhere.
    if backfill_model_ids:
        rows = conn.execute(
            "SELECT id, model FROM turns WHERE model_id IS NULL AND model IS NOT NULL AND TRIM(model) <> ''"
        ).fetchall()
        for turn_pk, model_key in rows:
            model_id = ensure_model_row(conn, model_key)
            if model_id is not None:
                conn.execute("UPDATE turns SET model_id = ? WHERE id = ?", (model_id, turn_pk))
    conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
    conn.commit()
