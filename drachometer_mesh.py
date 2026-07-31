#!/usr/bin/env python3
"""Mesh replication for drachometer (phase 1).

Append-only, event-sourced replication for trusted LAN/VM networks. Every local
write to ``turns`` or ``tool_calls`` emits an immutable event into an ``oplog``
table, keyed by a *content hash* so applying the same event any number of times
is a no-op. Nodes gossip over plain stdlib HTTP using pull-based anti-entropy
(compare per-origin digests, fetch the events you are missing): no broker, no
third-party dependencies.

Scope is deliberately limited to LAN/VM networks. Most mesh endpoints require
callers to present the mesh identifier (``<name>-<8 hex>``), which acts only
as a lightweight shared-secret gate to prevent accidental or casual
cross-mesh merges on a shared LAN (coworkers, roommates). Discovery keeps one
anonymous, read-only probe so nodes can advertise which mesh they belong to.
This is **not** robust authentication and there is still no TLS, so do not
expose mesh ports to the public internet.

This file is both an importable library (the hook and the dashboard server import
it) and a CLI for setup and maintenance::

    python drachometer_mesh.py init  --name home [--port 9874] [--advertise HOST]
    python drachometer_mesh.py join  MESH_ID --peer HOST:PORT [--peer HOST:PORT ...]
    python drachometer_mesh.py import OTHER.db [--as LABEL]
    python drachometer_mesh.py status
    python drachometer_mesh.py disable

Because identity is content-addressed and each Claude Code session runs on a
single machine (so ``session_id`` partitions writes by node), merging the
histories of two independently-created clients is just the union of their
oplogs -- no conflicts, idempotent on replay. ``join`` merges over the network;
``import`` merges an offline database file.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

CLAUDE_DIR = Path.home() / ".claude"
DB_PATH = CLAUDE_DIR / "drachometer.db"
CONFIG_PATH = CLAUDE_DIR / "drachometer-mesh.json"
LOG_PATH = CLAUDE_DIR / "drachometer-mesh.log"

SCHEMA_VERSION = 1          # replication payload/protocol version (handshake-checked)
DEFAULT_PORT = 9874
DEFAULT_SYNC_INTERVAL = 15  # seconds between gossip rounds
FETCH_BATCH = 200           # max event ids fetched per request
DEFAULT_LOG_LEVEL = "info"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_RETENTION_DAYS = 0
DEFAULT_RETENTION_KEEP_PER_ORIGIN = 50
DEFAULT_COMPRESS_PAYLOADS = True

# Local mesh discovery / runtime dashboard-control defaults.
DISCOVERY_TIMEOUT = 0.35        # per-host probe timeout (seconds)
DISCOVERY_MAX_WORKERS = 128     # concurrent probes during a subnet scan
DISCOVERY_MAX_HOSTS = 4096      # safety cap on total hosts scanned per request
PROPAGATION_WINDOW = 15         # rolling window of records for mean propagation time
PEER_ACTIVE_TTL = 45            # seconds a peer stays "active" after last successful contact

# Serializes oplog application *within* this process; WAL + busy_timeout handle
# cross-process contention (the hook writes from a separate process).
_APPLY_LOCK = threading.Lock()
_METRICS_LOCK = threading.Lock()
_METRICS = {
    "dedupes": 0,
    "conflicts": 0,
    "failed_sync_attempts": 0,
    "last_sync_at": None,
    "last_sync_peer": None,
    "last_sync_applied": 0,
    "last_sync_latency_ms": 0.0,
}

_LOG_LEVEL_CACHE: tuple[int | None, int | None, str] | None = None


# --------------------------------------------------------------------------- #
# Runtime state (dashboard-controlled mesh: live status, uptime, propagation)
# --------------------------------------------------------------------------- #
_RUNTIME_LOCK = threading.RLock()
_RUNTIME: dict = {
    "server": None,             # the live _ThreadingHTTPServer, if any
    "stop_event": None,         # threading.Event used to stop the gossip daemon
    "app_version": "",
    "started_at": None,         # epoch seconds when the current mesh came up
    "syncing": False,           # True while a gossip round is in flight
    "last_sync_ok": None,       # bool | None -- outcome of the most recent round
    "last_sync_at": None,       # ISO timestamp of the most recent round
    "active_peers": {},         # advertise-addr -> {"last_seen": epoch, "node_id": str}
    "prop_seconds": deque(maxlen=PROPAGATION_WINDOW),  # recent propagation times
    "prop_recorded": set(),     # event_ids already counted toward propagation
    "last_scan": None,          # cached discover_meshes() result
}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def _config_log_level(cfg: dict | None) -> str:
    level = (cfg or {}).get("log_level") or DEFAULT_LOG_LEVEL
    return str(level).lower()


def _cached_log_level() -> str:
    global _LOG_LEVEL_CACHE
    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        return DEFAULT_LOG_LEVEL
    cache_key = (stat.st_mtime_ns, stat.st_size)
    if _LOG_LEVEL_CACHE is not None and _LOG_LEVEL_CACHE[0] == cache_key[0] and _LOG_LEVEL_CACHE[1] == cache_key[1]:
        return _LOG_LEVEL_CACHE[2]
    cfg = load_config()
    level = _config_log_level(cfg)
    _LOG_LEVEL_CACHE = (cache_key[0], cache_key[1], level)
    return level


def _should_log(level: str, cfg: dict | None = None) -> bool:
    order = {"debug": 0, "info": 1, "warning": 2, "error": 3}
    current = order.get(_config_log_level(cfg), 1) if cfg is not None else order.get(_cached_log_level(), 1)
    return order.get(level.lower(), 1) >= current


def log(message: str, level: str = "info", **fields) -> None:
    if not _should_log(level):
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.lower(),
        "message": message,
    }
    entry.update(fields)
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def normalize_config(cfg: dict | None) -> dict | None:
    if not isinstance(cfg, dict):
        return None
    normalized = dict(cfg)
    normalized.setdefault("enabled", False)
    normalized.setdefault("mesh_id", None)
    normalized.setdefault("node_id", None)
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized.setdefault("listen_host", "0.0.0.0")
    normalized.setdefault("listen_port", DEFAULT_PORT)
    normalized.setdefault("advertise_host", None)
    normalized.setdefault("advertise_port", DEFAULT_PORT)
    normalized.setdefault("peers", [])
    normalized.setdefault("sync_interval_seconds", DEFAULT_SYNC_INTERVAL)
    normalized.setdefault("log_level", DEFAULT_LOG_LEVEL)
    normalized.setdefault("max_retries", DEFAULT_MAX_RETRIES)
    normalized.setdefault("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS)
    normalized.setdefault("retention_days", DEFAULT_RETENTION_DAYS)
    normalized.setdefault("retention_keep_per_origin", DEFAULT_RETENTION_KEEP_PER_ORIGIN)
    normalized.setdefault("compress_payloads", DEFAULT_COMPRESS_PAYLOADS)
    if not isinstance(normalized.get("peers"), list):
        normalized["peers"] = []
    return normalized


def load_config() -> dict | None:
    try:
        return normalize_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    payload = normalize_config(cfg)
    if payload is None:
        return
    CONFIG_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def is_enabled() -> bool:
    cfg = load_config()
    return bool(cfg and cfg.get("enabled") and cfg.get("mesh_id") and cfg.get("node_id"))


def make_mesh_id(name: str) -> str:
    """``<sanitized-name>-<8 hex>`` -- a human label plus an 8-char GUID suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "mesh").strip().lower()).strip("-") or "mesh"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def detect_lan_ip() -> str:
    """Best-effort primary LAN IPv4 (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.1", 9))  # TEST-NET-3; routing lookup only
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def connect(db_path: Path | None = None) -> sqlite3.Connection:
    # Resolve DB_PATH at call time (not as a default arg) so the module global
    # can be redirected, e.g. by tests.
    conn = sqlite3.connect(db_path or DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db(db_path: Path | None = None):
    """Open a connection that is *actually closed* on exit.

    ``with sqlite3.connect(...) as conn`` only manages a transaction and leaves
    the connection open -- a leak (and a Windows file lock) in the long-lived
    server. This commits on success and always closes.
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


OPLOG_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS mesh_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the oplog, mesh metadata, and the tool_calls.uid identity if absent."""
    conn.executescript(OPLOG_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO mesh_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "UPDATE mesh_meta SET value = ? WHERE key = 'schema_version' AND value IS NULL",
        (str(SCHEMA_VERSION),),
    )
    has_tool_calls = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchone()
    if has_tool_calls:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")]
        if "uid" not in cols:
            conn.execute("ALTER TABLE tool_calls ADD COLUMN uid TEXT")
        conn.execute(
            "UPDATE tool_calls SET uid = lower(hex(randomblob(16))) "
            "WHERE uid IS NULL OR uid = ''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_uid ON tool_calls(uid)"
        )
    conn.commit()


def _record_metric(name: str, value: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] = int(_METRICS.get(name, 0)) + value


def reset_metrics() -> None:
    with _METRICS_LOCK:
        _METRICS.update({
            "dedupes": 0,
            "conflicts": 0,
            "failed_sync_attempts": 0,
            "last_sync_at": None,
            "last_sync_peer": None,
            "last_sync_applied": 0,
            "last_sync_latency_ms": 0.0,
        })


def collect_health_metrics(cfg: dict | None = None, db_path: Path | None = None) -> dict:
    cfg = normalize_config(cfg or load_config() or {}) or {}
    peers = cfg.get("peers") or []
    reachable = 0
    mesh_id = cfg.get("mesh_id")
    for peer in peers:
        if not mesh_id:
            continue
        try:
            hello = _get_json(peer, _mesh_path("/mesh/hello", mesh_id), timeout=1.0)
        except Exception:
            continue
        if not hello.get("ok"):
            continue
        reachable += 1
    with _db(db_path) as conn:
        ensure_schema(conn)
        counts = local_origin_counts(conn)
    with _METRICS_LOCK:
        metrics = dict(_METRICS)
    metrics["peer_reachability"] = {"reachable": reachable, "total": len(peers)}
    metrics["oplog_event_count"] = sum(counts.values())
    metrics["oplog_origin_count"] = len(counts)
    metrics["schema_version"] = cfg.get("schema_version", SCHEMA_VERSION)
    metrics["retention_days"] = cfg.get("retention_days", DEFAULT_RETENTION_DAYS)
    metrics["retention_keep_per_origin"] = cfg.get("retention_keep_per_origin", DEFAULT_RETENTION_KEEP_PER_ORIGIN)
    metrics["compression_enabled"] = bool(cfg.get("compress_payloads", DEFAULT_COMPRESS_PAYLOADS))
    if metrics.get("last_sync_at"):
        metrics["replication_lag_seconds"] = int(
            (datetime.now(timezone.utc) - datetime.fromisoformat(str(metrics["last_sync_at"]))).total_seconds()
        )
    else:
        metrics["replication_lag_seconds"] = None
    metrics["dedupe_rate"] = round(
        metrics["dedupes"] / max(1, metrics["oplog_event_count"]), 3
    ) if metrics.get("oplog_event_count") else 0.0
    metrics["conflict_rate"] = round(
        metrics["conflicts"] / max(1, metrics["oplog_event_count"]), 3
    ) if metrics.get("oplog_event_count") else 0.0
    metrics["alert"] = "ok"
    if metrics["failed_sync_attempts"]:
        metrics["alert"] = "sync failures detected"
    elif metrics["peer_reachability"]["total"] and metrics["peer_reachability"]["reachable"] < metrics["peer_reachability"]["total"]:
        metrics["alert"] = "unreachable peers"
    elif not peers:
        metrics["alert"] = "no peers configured"
    return metrics


# --------------------------------------------------------------------------- #
# Model dimension (kept self-contained; mirrors the hook/installer inference so
# replicated model_keys resolve to priced rows on every node).
# --------------------------------------------------------------------------- #
MODEL_TIER_PRICING = {
    "opus":   {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_create": 6.25},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
    "haiku":  {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_create": 1.25},
}


def _load_pricing_overrides() -> None:
    pricing_path = Path(__file__).resolve().parent / "drachometer-pricing.json"
    try:
        data = json.loads(pricing_path.read_text(encoding="utf-8"))
        tiers = data.get("tiers", data)
        if isinstance(tiers, dict):
            for tier, p in tiers.items():
                if isinstance(p, dict) and isinstance(p.get("input"), (int, float)):
                    MODEL_TIER_PRICING[tier] = {
                        "input": p.get("input"),
                        "output": p.get("output"),
                        "cache_read": p.get("cache_read"),
                        "cache_create": p.get("cache_create"),
                    }
    except (OSError, json.JSONDecodeError, ValueError):
        pass


_load_pricing_overrides()


def _infer_model_attributes(model_key: str) -> dict:
    lower = model_key.lower()
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
    parts = [p for p in model_key.split("-") if p]
    model_name = (
        " ".join(parts[:2]).title()
        if len(parts) >= 2 and parts[0].lower() == "claude"
        else (parts[0].title() if parts else None)
    )
    version_match = re.search(r"(\d+(?:[-.]\d+)*(?:-\d{8})?)", model_key)
    provider = "Anthropic" if lower.startswith("claude") else None
    pricing = MODEL_TIER_PRICING.get(tier, {})
    return {
        "model_name": model_name,
        "model_version": version_match.group(1) if version_match else None,
        "model_provider": provider,
        "input_price_per_mtok": pricing.get("input"),
        "output_price_per_mtok": pricing.get("output"),
        "cache_read_price_per_mtok": pricing.get("cache_read"),
        "cache_creation_price_per_mtok": pricing.get("cache_create"),
    }


def ensure_model_row(conn: sqlite3.Connection, model_key: str | None) -> int | None:
    key = (model_key or "").strip()
    if not key:
        return None
    row = conn.execute("SELECT id FROM models WHERE model_key = ?", (key,)).fetchone()
    if row:
        return row[0]
    attrs = _infer_model_attributes(key)
    cur = conn.execute(
        """INSERT INTO models (
               model_key, model_name, model_version, model_provider,
               input_price_per_mtok, output_price_per_mtok,
               cache_read_price_per_mtok, cache_creation_price_per_mtok
           ) VALUES (
               :model_key, :model_name, :model_version, :model_provider,
               :input_price_per_mtok, :output_price_per_mtok,
               :cache_read_price_per_mtok, :cache_creation_price_per_mtok
           )""",
        {"model_key": key, **attrs},
    )
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# Event identity and emission
# --------------------------------------------------------------------------- #
def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_id_for(entity: str, canonical_payload: str) -> str:
    digest = hashlib.sha256(f"{entity}\x00{canonical_payload}".encode("utf-8"))
    return digest.hexdigest()[:40]


def _next_lamport(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(lamport) FROM oplog").fetchone()
    return (row[0] or 0) + 1


def emit_event(conn: sqlite3.Connection, origin_node: str, entity: str, payload: dict) -> str:
    """Record a locally-originated change as an immutable oplog event.

    Content-addressed: re-emitting identical content is a harmless no-op, so
    callers never need to deduplicate. Does not commit (the caller owns the
    transaction so the base-table write and its event are atomic).
    """
    canonical = _canonical(payload)
    eid = event_id_for(entity, canonical)
    conn.execute(
        """INSERT OR IGNORE INTO oplog
               (event_id, origin_node, lamport, created_at, entity, op, payload)
           VALUES (?, ?, ?, ?, ?, 'upsert', ?)""",
        (eid, origin_node, _next_lamport(conn), datetime.now(timezone.utc).isoformat(),
         entity, canonical),
    )
    return eid


def turn_payload(row: dict) -> dict:
    """Logical, node-independent representation of a turn (model_key, not model_id)."""
    return {
        "session_id": row.get("session_id"),
        "turn_id": row.get("turn_id"),
        "recorded_at": row.get("recorded_at"),
        "stop_reason": row.get("stop_reason"),
        "input_tokens": row.get("input_tokens") or 0,
        "output_tokens": row.get("output_tokens") or 0,
        "cache_read_tokens": row.get("cache_read_tokens") or 0,
        "cache_creation_tokens": row.get("cache_creation_tokens") or 0,
        "cwd": row.get("cwd"),
        "git_branch": row.get("git_branch"),
        "model_key": row.get("model_key"),
    }


def tool_call_payload(row: dict) -> dict:
    return {
        "uid": row.get("uid"),
        "session_id": row.get("session_id"),
        "turn_id": row.get("turn_id"),
        "recorded_at": row.get("recorded_at"),
        "tool_name": row.get("tool_name"),
        "tool_input": row.get("tool_input"),
        "exit_code": row.get("exit_code"),
        "error": row.get("error"),
    }


# --------------------------------------------------------------------------- #
# Event application (idempotent projection into base tables)
# --------------------------------------------------------------------------- #
def _project_turn(conn: sqlite3.Connection, p: dict) -> bool:
    model_id = ensure_model_row(conn, p.get("model_key"))
    existing = conn.execute(
        "SELECT recorded_at FROM turns WHERE session_id = ? AND turn_id = ?",
        (p.get("session_id"), p.get("turn_id")),
    ).fetchone()
    if existing and existing[0] and p.get("recorded_at") and existing[0] > p.get("recorded_at"):
        _record_metric("conflicts")
        log(
           "turn conflict suppressed by deterministic LWW rule",
           level="warning",
           event="conflict",
           entity="turn",
           session_id=p.get("session_id"),
           turn_id=p.get("turn_id"),
        )
        return False
    # Last-writer-wins on recorded_at: only overwrite an existing turn when the
    # incoming event is at least as recent (sessions are single-node, so this
    # only matters for the rare re-log of the same (session_id, turn_id)).
    conn.execute(
        """INSERT INTO turns (
               session_id, turn_id, recorded_at, stop_reason,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
               cwd, git_branch, model_id
           ) VALUES (
               :session_id, :turn_id, :recorded_at, :stop_reason,
               :input_tokens, :output_tokens, :cache_read_tokens, :cache_creation_tokens,
               :cwd, :git_branch, :model_id
           )
           ON CONFLICT(session_id, turn_id) DO UPDATE SET
               recorded_at           = excluded.recorded_at,
               stop_reason           = excluded.stop_reason,
               input_tokens          = excluded.input_tokens,
               output_tokens         = excluded.output_tokens,
               cache_read_tokens     = excluded.cache_read_tokens,
               cache_creation_tokens = excluded.cache_creation_tokens,
               cwd                   = excluded.cwd,
               git_branch            = excluded.git_branch,
               model_id              = excluded.model_id
           WHERE excluded.recorded_at >= turns.recorded_at""",
        {**p, "model_id": model_id},
    )
    return True


def _project_tool_call(conn: sqlite3.Connection, p: dict) -> bool:
    # Resolve the local turn primary key from the global natural key. May be
    # NULL if the parent turn has not replicated yet; queries can still join on
    # (session_id, turn_id), which is carried on every tool_call row.
    turn_pk = conn.execute(
        "SELECT id FROM turns WHERE session_id = ? AND turn_id = ?",
        (p.get("session_id"), p.get("turn_id")),
    ).fetchone()
    uid = p.get("uid")
    if uid:
        existing = conn.execute("SELECT 1 FROM tool_calls WHERE uid = ?", (uid,)).fetchone()
        if existing:
           _record_metric("dedupes")
           log("tool_call duplicate suppressed", level="debug", event="dedupe", entity="tool_call", uid=uid)
           return False
    cur = conn.execute(
        """INSERT INTO tool_calls (
               uid, turn_pk, session_id, turn_id, recorded_at,
               tool_name, tool_input, exit_code, error
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(uid) DO NOTHING""",
        (
           p.get("uid"), turn_pk[0] if turn_pk else None,
           p.get("session_id"), p.get("turn_id"), p.get("recorded_at"),
           p.get("tool_name"), p.get("tool_input"), p.get("exit_code"), p.get("error"),
        ),
    )
    return cur.rowcount > 0


def apply_event(conn: sqlite3.Connection, ev: dict) -> bool:
    """Store a (possibly remote) event and project it. Returns True if new.

    Idempotent: the oplog primary key drops duplicates, so a replayed or
    re-gossiped event is silently ignored.
    """
    payload = ev["payload"] if isinstance(ev["payload"], dict) else json.loads(ev["payload"])
    canonical = _canonical(payload)
    cur = conn.execute(
        """INSERT OR IGNORE INTO oplog
               (event_id, origin_node, lamport, created_at, entity, op, payload)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ev["event_id"], ev["origin_node"], int(ev["lamport"]), ev["created_at"],
         ev["entity"], ev.get("op", "upsert"), canonical),
    )
    if cur.rowcount == 0:
        _record_metric("dedupes")
        log(
            "event duplicate suppressed",
            level="debug",
            event="dedupe",
            entity=ev.get("entity"),
            event_id=ev.get("event_id"),
        )
        return False
    projected = True
    if ev["entity"] == "turn":
        projected = _project_turn(conn, payload)
    elif ev["entity"] == "tool_call":
        projected = _project_tool_call(conn, payload)
    if not projected:
        log(
            "event applied without projection change",
            level="debug",
            event="projection_skip",
            entity=ev.get("entity"),
            event_id=ev.get("event_id"),
        )
    return projected


def local_origin_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        origin: count
        for origin, count in conn.execute(
            "SELECT origin_node, COUNT(*) FROM oplog GROUP BY origin_node"
        )
    }


# --------------------------------------------------------------------------- #
# Backfill / import -- give pre-mesh history (or a foreign database) an oplog
# representation so it can replicate.
# --------------------------------------------------------------------------- #
def backfill(conn: sqlite3.Connection, origin_node: str) -> int:
    """Synthesize events for every existing local turn/tool_call. Idempotent."""
    ensure_schema(conn)
    emitted = 0
    for row in conn.execute(
        """SELECT t.session_id, t.turn_id, t.recorded_at, t.stop_reason,
                  t.input_tokens, t.output_tokens, t.cache_read_tokens,
                  t.cache_creation_tokens, t.cwd, t.git_branch, m.model_key
           FROM turns t LEFT JOIN models m ON t.model_id = m.id"""
    ).fetchall():
        keys = ["session_id", "turn_id", "recorded_at", "stop_reason",
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens", "cwd", "git_branch", "model_key"]
        emit_event(conn, origin_node, "turn", turn_payload(dict(zip(keys, row))))
        emitted += 1
    conn.execute(
        "UPDATE tool_calls SET uid = lower(hex(randomblob(16))) WHERE uid IS NULL OR uid = ''"
    )
    for row in conn.execute(
        """SELECT uid, session_id, turn_id, recorded_at, tool_name, tool_input,
                  exit_code, error FROM tool_calls"""
    ).fetchall():
        keys = ["uid", "session_id", "turn_id", "recorded_at", "tool_name",
                "tool_input", "exit_code", "error"]
        emit_event(conn, origin_node, "tool_call", tool_call_payload(dict(zip(keys, row))))
        emitted += 1
    conn.commit()
    return emitted


def import_database(conn: sqlite3.Connection, other_db: Path, label: str | None) -> int:
    """Merge records from an independently-created database into this one.

    If the foreign database already has an oplog (it was mesh-enabled), its
    events are applied verbatim, preserving their original origins. Otherwise we
    synthesize events from its turns/tool_calls under a stable synthetic origin
    so re-importing the same file stays idempotent.
    """
    ensure_schema(conn)
    src = sqlite3.connect(f"file:{other_db}?mode=ro", uri=True)
    try:
        applied = 0
        has_oplog = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oplog'"
        ).fetchone()
        if has_oplog:
            cols = "event_id, origin_node, lamport, created_at, entity, op, payload"
            with _APPLY_LOCK:
                for row in src.execute(f"SELECT {cols} FROM oplog").fetchall():
                    ev = dict(zip(cols.replace(" ", "").split(","), row))
                    if apply_event(conn, ev):
                        applied += 1
                conn.commit()
            return applied

        # No oplog: derive a stable synthetic origin for the foreign rows.
        origin = label or f"import-{hashlib.sha256(str(other_db.resolve()).encode()).hexdigest()[:8]}"
        src_model = {
            mid: key for mid, key in src.execute("SELECT id, model_key FROM models")
        } if src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='models'"
        ).fetchone() else {}
        with _APPLY_LOCK:
            for row in src.execute(
                """SELECT session_id, turn_id, recorded_at, stop_reason, input_tokens,
                          output_tokens, cache_read_tokens, cache_creation_tokens,
                          cwd, git_branch, model_id FROM turns"""
            ).fetchall():
                keys = ["session_id", "turn_id", "recorded_at", "stop_reason",
                        "input_tokens", "output_tokens", "cache_read_tokens",
                        "cache_creation_tokens", "cwd", "git_branch", "model_id"]
                d = dict(zip(keys, row))
                d["model_key"] = src_model.get(d.pop("model_id"))
                p = turn_payload(d)
                before = conn.total_changes
                emit_event(conn, origin, "turn", p)  # INSERT OR IGNORE
                if conn.total_changes > before:      # count only new events
                    applied += 1
                _project_turn(conn, p)
            tc_cols = [r[1] for r in src.execute("PRAGMA table_info(tool_calls)")]
            uid_expr = "uid" if "uid" in tc_cols else "lower(hex(randomblob(16)))"
            for row in src.execute(
                f"""SELECT {uid_expr}, session_id, turn_id, recorded_at, tool_name,
                           tool_input, exit_code, error FROM tool_calls"""
            ).fetchall():
                keys = ["uid", "session_id", "turn_id", "recorded_at", "tool_name",
                        "tool_input", "exit_code", "error"]
                p = tool_call_payload(dict(zip(keys, row)))
                if not p["uid"]:
                    p["uid"] = uuid.uuid4().hex
                before = conn.total_changes
                emit_event(conn, origin, "tool_call", p)  # INSERT OR IGNORE
                if conn.total_changes > before:           # count only new events
                    applied += 1
                _project_tool_call(conn, p)
            conn.commit()
        return applied
    finally:
        src.close()


# --------------------------------------------------------------------------- #
# HTTP transport -- mesh endpoints (separate from the loopback dashboard server).
# --------------------------------------------------------------------------- #
class _PeerRegistry:
    """Configured seeds plus peers learned via startup registration."""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._peers = set(cfg.get("peers") or [])
        self._lock = threading.Lock()

    def all(self) -> list[str]:
        with self._lock:
            return sorted(self._peers)

    def add(self, peer: str) -> None:
        if not peer:
            return
        with self._lock:
            if peer in self._peers:
                return
            self._peers.add(peer)
            self._cfg["peers"] = sorted(self._peers)
        try:
            save_config(self._cfg)
        except OSError:
            pass


def _make_mesh_handler(cfg: dict, registry: _PeerRegistry, app_version: str,
                       db_path: Path | None = None):
    db_path = db_path or DB_PATH
    class MeshHandler(BaseHTTPRequestHandler):
        def _send_json(self, obj, status=200, compress=None):
            raw_body = json.dumps(obj).encode("utf-8")
            accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            use_gzip = bool(compress if compress is not None else (accepts_gzip and cfg.get("compress_payloads", DEFAULT_COMPRESS_PAYLOADS)))
            body = gzip.compress(raw_body) if use_gzip and accepts_gzip else raw_body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if use_gzip and accepts_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _mesh_authorized(self, presented_mesh_id: str | None) -> bool:
            return bool(presented_mesh_id) and presented_mesh_id == cfg.get("mesh_id")

        def _require_mesh_id(self, presented_mesh_id: str | None) -> bool:
            if self._mesh_authorized(presented_mesh_id):
                return True
            self._send_json({"error": "mesh mismatch"}, status=403)
            return False

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
            if path == "/mesh/discover":
                self._send_json({
                    "ok": bool(cfg.get("enabled") and cfg.get("mesh_id") and cfg.get("node_id")),
                    "mesh_id": cfg.get("mesh_id"),
                    "node_id": cfg.get("node_id"),
                    "schema_version": int(cfg.get("schema_version", SCHEMA_VERSION)),
                })
            elif path == "/mesh/hello":
                if not self._mesh_authorized(params.get("mesh_id")):
                    self._send_json({"ok": False, "mesh_id_match": False})
                    return
                self._send_json({
                    "ok": True,
                    "mesh_id_match": True,
                    "node_id": cfg["node_id"],
                    "schema_version": int(cfg.get("schema_version", SCHEMA_VERSION)),
                    "app_version": app_version,
                    "migration_note": "upgrade both nodes to the same release and re-run 'python drachometer_mesh.py migrate' if schema versions differ",
                })
            elif path == "/mesh/digest":
                if not self._require_mesh_id(params.get("mesh_id")):
                    return
                with _db(db_path) as conn:
                    origins = local_origin_counts(conn)
                self._send_json({
                    "schema_version": SCHEMA_VERSION,
                    "origins": origins,
                })
            elif path == "/mesh/event-ids":
                if not self._require_mesh_id(params.get("mesh_id")):
                    return
                origin = params.get("origin", "")
                with _db(db_path) as conn:
                    ids = [r[0] for r in conn.execute(
                        "SELECT event_id FROM oplog WHERE origin_node = ? ORDER BY lamport",
                        (origin,),
                    )]
                self._send_json({"origin": origin, "ids": ids})
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self):  # noqa: N802
            path = self.path.partition("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "bad json"}, status=400)
                return
            if path == "/mesh/events":
                if not self._require_mesh_id(body.get("mesh_id")):
                    return
                ids = body.get("ids") or []
                qmarks = ",".join("?" * len(ids))
                events = []
                if ids:
                    with _db(db_path) as conn:
                        for row in conn.execute(
                            f"""SELECT event_id, origin_node, lamport, created_at,
                                       entity, op, payload
                                FROM oplog WHERE event_id IN ({qmarks})""",
                            ids,
                        ):
                            events.append({
                                "event_id": row[0], "origin_node": row[1],
                                "lamport": row[2], "created_at": row[3],
                                "entity": row[4], "op": row[5], "payload": row[6],
                            })
                self._send_json({"events": events})
            elif path == "/mesh/announce":
                if not self._require_mesh_id(body.get("mesh_id")):
                    return
                # Startup registration: a peer tells us how to reach it.
                advertise = body.get("advertise")
                if advertise:
                    registry.add(advertise)
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, status=404)

        def log_message(self, *args):  # silence default stderr logging
            pass

    return MeshHandler


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True, inherited_socket: int | None = None):
        self._inherited_socket = inherited_socket is not None
        if self._inherited_socket:
            super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)
            self.socket = socket.socket(fileno=inherited_socket)
            self.server_address = self.socket.getsockname()
            return
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=bind_and_activate)

    def server_activate(self) -> None:
        if self._inherited_socket:
            return
        super().server_activate()


# --------------------------------------------------------------------------- #
# HTTP client + gossip
# --------------------------------------------------------------------------- #
def _decode_response(resp) -> bytes:
    payload = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        return gzip.decompress(payload)
    return payload


def _get_json(peer: str, path: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        f"http://{peer}{path}", headers={"Accept-Encoding": "gzip"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(_decode_response(resp).decode("utf-8"))


def _post_json(peer: str, path: str, body: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://{peer}{path}", data=data,
        headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(_decode_response(resp).decode("utf-8"))


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _mesh_path(path: str, mesh_id: str, **params) -> str:
    query = {"mesh_id": mesh_id}
    query.update({key: value for key, value in params.items() if value is not None})
    return f"{path}?{urllib.parse.urlencode(query)}"


def sync_with_peer(cfg: dict, peer: str) -> int:
    """Pull every event this node is missing from one peer. Returns count applied."""
    cfg = normalize_config(cfg) or {}
    expected_schema = int(cfg.get("schema_version", SCHEMA_VERSION))
    attempts = int(cfg.get("max_retries", DEFAULT_MAX_RETRIES)) + 1
    backoff = float(cfg.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS))
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            hello = _get_json(peer, _mesh_path("/mesh/hello", cfg["mesh_id"]))
            if not hello.get("ok"):
                log(
                    "skip peer due to mesh id mismatch",
                    level="warning",
                    event="mesh_mismatch",
                    peer=peer,
                    local_mesh_id=cfg.get("mesh_id"),
                )
                return 0
            remote_schema = int(hello.get("schema_version", 0))
            if remote_schema != expected_schema:
                log(
                    "skip peer due to schema version mismatch",
                    level="warning",
                    event="schema_mismatch",
                    peer=peer,
                    remote_schema=remote_schema,
                    local_schema=expected_schema,
                    migration_note="upgrade both nodes to the same release and re-run 'python drachometer_mesh.py migrate'",
                )
                return 0

            remote = _get_json(peer, _mesh_path("/mesh/digest", cfg["mesh_id"]))
            with _db() as conn:
                local_counts = local_origin_counts(conn)
            applied = 0
            for origin, remote_count in (remote.get("origins") or {}).items():
                if local_counts.get(origin, 0) >= remote_count:
                    continue
                remote_ids = _get_json(
                    peer, _mesh_path("/mesh/event-ids", cfg["mesh_id"], origin=origin)
                ).get("ids", [])
                with _db() as conn:
                    have = {
                        r[0] for r in conn.execute(
                            "SELECT event_id FROM oplog WHERE origin_node = ?", (origin,)
                        )
                    }
                missing = [i for i in remote_ids if i not in have]
                for batch in _chunks(missing, int(cfg.get("fetch_batch_size", FETCH_BATCH))):
                    events = _post_json(
                        peer, "/mesh/events", {"mesh_id": cfg["mesh_id"], "ids": batch}
                    ).get("events", [])
                    log(
                        "pulling sync batch",
                        level="debug",
                        event="sync_batch",
                        peer=peer,
                        origin=origin,
                        batch_size=len(batch),
                    )
                    with _APPLY_LOCK, connect() as conn:
                        for ev in events:
                            if apply_event(conn, ev):
                                applied += 1
                        conn.commit()
            with _METRICS_LOCK:
                _METRICS["last_sync_at"] = datetime.now(timezone.utc).isoformat()
                _METRICS["last_sync_peer"] = peer
                _METRICS["last_sync_applied"] = applied
                _METRICS["last_sync_latency_ms"] = round((time.monotonic() - started) * 1000, 2)
            log(
                "sync completed",
                level="info",
                event="sync_complete",
                peer=peer,
                applied=applied,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
            )
            return applied
        except Exception as exc:
            _record_metric("failed_sync_attempts")
            if attempt >= attempts:
                log(
                    "sync failed",
                    level="error",
                    event="sync_error",
                    peer=peer,
                    attempt=attempt,
                    error=str(exc),
                )
                return 0
            log(
                "sync retrying",
                level="warning",
                event="sync_retry",
                peer=peer,
                attempt=attempt,
                backoff_seconds=round(backoff * attempt, 2),
                error=str(exc),
            )
            time.sleep(backoff * attempt)
    return 0


def sync_round(cfg: dict, registry: _PeerRegistry) -> int:
    total = 0
    with _RUNTIME_LOCK:
        _RUNTIME["syncing"] = True
    ok = True
    try:
        for peer in registry.all():
            try:
                total += sync_with_peer(cfg, peer)
            except Exception as exc:  # network/peer errors are expected and non-fatal
                ok = False
                log(f"sync error {peer}: {exc}")
        try:
            _update_liveness_and_propagation(cfg, registry)
        except Exception as exc:  # liveness probing is best-effort
            log(f"liveness error: {exc}", level="warning")
    finally:
        with _RUNTIME_LOCK:
            _RUNTIME["syncing"] = False
            _RUNTIME["last_sync_ok"] = ok
            _RUNTIME["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    return total


def _announce(cfg: dict, registry: _PeerRegistry) -> None:
    advertise = f"{cfg.get('advertise_host')}:{cfg.get('advertise_port', DEFAULT_PORT)}"
    for peer in registry.all():
        try:
            _post_json(peer, "/mesh/announce",
                       {"mesh_id": cfg["mesh_id"], "node_id": cfg["node_id"],
                        "advertise": advertise}, timeout=5.0)
        except Exception as exc:
            log(f"announce error {peer}: {exc}")


def start_mesh(app_version: str = "", db_path: Path | None = None, inherited_socket: int | None = None) -> bool:
    """Start the mesh HTTP server and gossip daemon if mesh is enabled.

    Returns True if mesh was started. Safe to call from the dashboard server; all
    work happens on daemon threads. Any previously-running mesh (started in this
    process) is stopped first so create/join/leave can reconfigure at runtime.
    """
    cfg = load_config()
    if not (cfg and cfg.get("enabled") and cfg.get("mesh_id") and cfg.get("node_id")):
        return False

    stop_mesh()  # tear down any prior in-process mesh before rebinding

    with _db(db_path) as conn:
        ensure_schema(conn)

    registry = _PeerRegistry(cfg)
    host = cfg.get("listen_host", "0.0.0.0")
    port = int(cfg.get("listen_port", DEFAULT_PORT))
    handler = _make_mesh_handler(cfg, registry, app_version, db_path)
    try:
        server = _ThreadingHTTPServer((host, port), handler, inherited_socket=inherited_socket)
    except OSError as exc:
        log(f"mesh server bind failed on {host}:{port}: {exc}")
        return False

    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"mesh server listening on {host}:{port} (mesh_id={cfg['mesh_id']})")

    stop_event = threading.Event()
    with _RUNTIME_LOCK:
        _RUNTIME["server"] = server
        _RUNTIME["stop_event"] = stop_event
        _RUNTIME["app_version"] = app_version
        _RUNTIME["started_at"] = time.time()
        _RUNTIME["active_peers"] = {}
        _RUNTIME["prop_seconds"] = deque(maxlen=PROPAGATION_WINDOW)
        _RUNTIME["prop_recorded"] = set()

    def _daemon():
        _announce(cfg, registry)  # startup registration
        interval = int(cfg.get("sync_interval_seconds", DEFAULT_SYNC_INTERVAL))
        while not stop_event.is_set():
            sync_round(cfg, registry)
            stop_event.wait(interval)

    threading.Thread(target=_daemon, daemon=True).start()
    return True


def stop_mesh() -> bool:
    """Stop the in-process mesh server and gossip daemon, if running.

    Returns True if something was stopped. Replication history is untouched.
    """
    with _RUNTIME_LOCK:
        server = _RUNTIME.get("server")
        stop_event = _RUNTIME.get("stop_event")
        _RUNTIME["server"] = None
        _RUNTIME["stop_event"] = None
        _RUNTIME["started_at"] = None
        _RUNTIME["syncing"] = False
        _RUNTIME["active_peers"] = {}
    if stop_event is not None:
        stop_event.set()
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        log("mesh server stopped")
        return True
    return False


# --------------------------------------------------------------------------- #
# Local subnet discovery + live status (dashboard-driven configuration)
# --------------------------------------------------------------------------- #
def list_local_ipv4s() -> list[str]:
    """Best-effort set of this host's non-loopback IPv4 addresses across NICs.

    Used only as a fallback when the OS network tools cannot be queried for the
    real per-interface netmask (see :func:`list_local_interfaces`).
    """
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr:
                ips.add(addr)
    except OSError:
        pass
    primary = detect_lan_ip()
    if primary:
        ips.add(primary)
    return sorted(ip for ip in ips if not ipaddress.ip_address(ip).is_loopback)


def _netmask_to_prefix(mask: str) -> int | None:
    """Convert a netmask to a prefix length.

    Accepts a dotted-quad (``255.255.255.0``), a hex mask (``0xffffff00``) as
    printed by BSD/macOS ``ifconfig``, or a bare prefix (``24``).
    """
    mask = (mask or "").strip()
    if not mask:
        return None
    try:
        if mask.lower().startswith("0x"):
            dotted = str(ipaddress.IPv4Address(int(mask, 16)))
        elif "." in mask:
            dotted = mask
        else:
            prefix = int(mask)
            return prefix if 0 <= prefix <= 32 else None
        return ipaddress.IPv4Network(f"0.0.0.0/{dotted}").prefixlen
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        return None


def _parse_ip_addr_output(text: str) -> list[tuple[str, int]]:
    """Parse ``ip -o -f inet addr show`` (Linux iproute2) output.

    Each address already carries its prefix, e.g. ``inet 192.168.1.50/24``.
    """
    out: list[tuple[str, int]] = []
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", text):
        out.append((match.group(1), int(match.group(2))))
    return out


def _parse_ifconfig_output(text: str) -> list[tuple[str, int]]:
    """Parse ``ifconfig`` output (BSD/macOS hex masks or Linux dotted masks)."""
    out: list[tuple[str, int]] = []
    pattern = re.compile(
        r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+).*?"
        r"(?:netmask|Mask:?)\s*(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        prefix = _netmask_to_prefix(match.group(2))
        if prefix is not None:
            out.append((match.group(1), prefix))
    return out


def _parse_windows_ipconfig_output(text: str) -> list[tuple[str, int]]:
    """Parse Windows ``ipconfig`` output, pairing each IPv4 with its subnet mask."""
    out: list[tuple[str, int]] = []
    pending_ip: str | None = None
    for line in text.splitlines():
        ipv4 = re.search(r"IPv4 Address.*?:\s*([\d.]+)", line)
        if ipv4:
            pending_ip = ipv4.group(1).strip()
            continue
        mask = re.search(r"Subnet Mask.*?:\s*([\d.]+)", line)
        if mask and pending_ip:
            prefix = _netmask_to_prefix(mask.group(1).strip())
            if prefix is not None:
                out.append((pending_ip, prefix))
            pending_ip = None
    return out


def list_local_interfaces() -> list[tuple[str, int]]:
    """Enumerate active NICs as ``(ipv4, prefix_length)`` from the OS.

    Subnets are read from the machine's real interface configuration rather than
    assumed, so discovery scans exactly the networks this host is attached to.
    Loopback, link-local and unspecified addresses are skipped. Returns an empty
    list if no network tool could be queried (callers then fall back).
    """
    if platform.system() == "Windows":
        commands: list[tuple[list[str], object]] = [
            (["ipconfig"], _parse_windows_ipconfig_output),
        ]
    else:
        commands = [
            (["ip", "-o", "-f", "inet", "addr", "show"], _parse_ip_addr_output),
            (["ifconfig", "-a"], _parse_ifconfig_output),
            (["ifconfig"], _parse_ifconfig_output),
        ]
    for argv, parser in commands:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if not proc.stdout:
            continue
        result: list[tuple[str, int]] = []
        for ip, prefix in parser(proc.stdout):  # type: ignore[operator]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
                continue
            if not (0 <= prefix <= 32):
                continue
            result.append((ip, prefix))
        if result:
            return result
    return []


def subnets_from_ips(ips: list[str], prefix: int = 24) -> list[str]:
    """Derive unique CIDR subnets (default /24) covering the given IPv4 addresses.

    Fallback only, used when real NIC netmasks cannot be read from the OS.
    """
    nets: list[str] = []
    seen: set[str] = set()
    for ip in ips:
        try:
            net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        except ValueError:
            continue
        key = str(net)
        if key not in seen:
            seen.add(key)
            nets.append(key)
    return nets


def list_local_subnets(fallback_prefix: int = 24) -> list[str]:
    """All local IPv4 subnets, taken from the active NIC(s) real netmasks.

    Falls back to approximating each detected address as a /``fallback_prefix``
    network only when the OS interface list cannot be obtained.
    """
    subnets: list[str] = []
    seen: set[str] = set()
    for ip, prefix in list_local_interfaces():
        try:
            net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        except ValueError:
            continue
        key = str(net)
        if key not in seen:
            seen.add(key)
            subnets.append(key)
    if subnets:
        return subnets
    return subnets_from_ips(list_local_ipv4s(), fallback_prefix)


def _hosts_for_subnets(subnets: list[str], cap: int = DISCOVERY_MAX_HOSTS) -> list[str]:
    hosts: list[str] = []
    for cidr in subnets:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        for host in net.hosts():
            hosts.append(str(host))
            if len(hosts) >= cap:
                return hosts
    return hosts


def probe_discovery(host: str, port: int = DEFAULT_PORT,
                    timeout: float = DISCOVERY_TIMEOUT) -> dict | None:
    """Return anonymous mesh discovery metadata for a reachable node."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((host, port)) != 0:
                return None
        except OSError:
            return None
    try:
        hello = _get_json(f"{host}:{port}", "/mesh/discover", timeout=timeout)
    except Exception:
        return None
    if not isinstance(hello, dict) or not hello.get("ok") or not hello.get("mesh_id"):
        return None
    hello["advertise"] = f"{host}:{port}"
    return hello


def probe_node(host: str, port: int = DEFAULT_PORT, timeout: float = DISCOVERY_TIMEOUT,
               mesh_id: str | None = None) -> dict | None:
    """Return a peer's hello payload only when it proves membership in ``mesh_id``."""
    if not mesh_id:
        return None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((host, port)) != 0:
                return None
        except OSError:
            return None
    try:
        hello = _get_json(f"{host}:{port}", _mesh_path("/mesh/hello", mesh_id), timeout=timeout)
    except Exception:
        return None
    if not isinstance(hello, dict) or not hello.get("ok"):
        return None
    hello["mesh_id"] = mesh_id
    hello["advertise"] = f"{host}:{port}"
    return hello


def _group_meshes(hellos: list[dict], current_mesh_id: str | None) -> list[dict]:
    """Group ``/mesh/hello`` responses into per-mesh summaries."""
    grouped: dict[str, dict] = {}
    for hello in hellos:
        mesh_id = hello.get("mesh_id")
        if not mesh_id:
            continue
        name, _, suffix = str(mesh_id).rpartition("-")
        if not name:  # no dash -> treat whole id as the name
            name, suffix = str(mesh_id), ""
        entry = grouped.setdefault(mesh_id, {
            "mesh_id": mesh_id,
            "name": name,
            "suffix": suffix,
            "nodes": [],
            "is_current": bool(current_mesh_id) and mesh_id == current_mesh_id,
        })
        node = {"advertise": hello.get("advertise"), "node_id": hello.get("node_id")}
        if node not in entry["nodes"]:
            entry["nodes"].append(node)
    result = []
    for entry in grouped.values():
        entry["node_count"] = len(entry["nodes"])
        result.append(entry)
    result.sort(key=lambda e: (not e["is_current"], e["name"], e["suffix"]))
    return result


def discover_meshes(port: int = DEFAULT_PORT, timeout: float = DISCOVERY_TIMEOUT,
                    subnets: list[str] | None = None,
                    max_workers: int = DISCOVERY_MAX_WORKERS) -> dict:
    """Scan local subnets on all enabled NICs for reachable mesh nodes.

    Groups the responses by mesh id and marks the mesh this node currently
    belongs to. The result is cached for the live-status endpoint so the header
    indicator can list adjacent meshes without re-scanning every poll.
    """
    cfg = load_config() or {}
    current = cfg.get("mesh_id") if cfg.get("enabled") else None
    subnets = subnets if subnets is not None else list_local_subnets()
    hosts = _hosts_for_subnets(subnets)
    hellos: list[dict] = []
    if hosts:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(hosts))) as pool:
            for hello in pool.map(lambda h: probe_discovery(h, port, timeout), hosts):
                if hello:
                    hellos.append(hello)
    meshes = _group_meshes(hellos, current)
    result = {
        "subnets": subnets,
        "scanned_hosts": len(hosts),
        "meshes": meshes,
        "current_mesh_id": current,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    with _RUNTIME_LOCK:
        _RUNTIME["last_scan"] = result
    return result


def _update_liveness_and_propagation(cfg: dict, registry: _PeerRegistry) -> None:
    """Probe configured peers for liveness and update propagation timings.

    A peer counts as *active* when it responds to ``/mesh/hello`` with our mesh
    id. For every locally-originated event, the time until it is present on all
    active peers is recorded (rolling window) as the propagation latency.
    """
    node_id = cfg.get("node_id")
    mesh_id = cfg.get("mesh_id")
    now = time.time()
    active: dict[str, dict] = {}
    peer_has: dict[str, set[str]] = {}
    for peer in registry.all():
        try:
            hello = _get_json(peer, _mesh_path("/mesh/hello", mesh_id), timeout=2.0)
        except Exception:
            continue
        if not hello.get("ok"):
            continue
        active[peer] = {"last_seen": now, "node_id": hello.get("node_id")}
        try:
            ids = _get_json(
                peer, _mesh_path("/mesh/event-ids", mesh_id, origin=node_id), timeout=3.0
            ).get("ids", [])
            peer_has[peer] = set(ids)
        except Exception:
            peer_has[peer] = set()
    with _RUNTIME_LOCK:
        _RUNTIME["active_peers"] = active
    if not active:
        return  # nothing to propagate to; propagation time is undefined
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT event_id, created_at FROM oplog WHERE origin_node = ? "
                "ORDER BY lamport DESC LIMIT ?",
                (node_id, PROPAGATION_WINDOW * 4),
            ).fetchall()
    except sqlite3.Error:
        return
    _record_propagations(rows, peer_has, now)


def _record_propagations(rows, peer_has: dict[str, set[str]], now: float) -> int:
    """Record propagation latency for events now present on every active peer.

    ``rows`` is an iterable of ``(event_id, created_at_iso)``. Returns the number
    of newly-recorded propagation samples. Pure enough to unit-test directly.
    """
    if not peer_has:
        return 0
    recorded = 0
    with _RUNTIME_LOCK:
        already = _RUNTIME["prop_recorded"]
        window = _RUNTIME["prop_seconds"]
        for event_id, created_at in rows:
            if event_id in already:
                continue
            if not all(event_id in have for have in peer_has.values()):
                continue
            try:
                created = datetime.fromisoformat(str(created_at))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            seconds = max(0.0, now - created.timestamp())
            window.append(seconds)
            already.add(event_id)
            recorded += 1
    return recorded


def mesh_uptime_seconds() -> float | None:
    with _RUNTIME_LOCK:
        started = _RUNTIME.get("started_at")
    if not started:
        return None
    return max(0.0, time.time() - started)


def active_peers() -> list[dict]:
    now = time.time()
    with _RUNTIME_LOCK:
        peers = dict(_RUNTIME.get("active_peers") or {})
    out = []
    for advertise, info in peers.items():
        if now - info.get("last_seen", 0) <= PEER_ACTIVE_TTL:
            out.append({"advertise": advertise, "node_id": info.get("node_id")})
    out.sort(key=lambda p: p["advertise"] or "")
    return out


def mean_propagation_seconds() -> float | None:
    with _RUNTIME_LOCK:
        window = list(_RUNTIME.get("prop_seconds") or [])
    if not window:
        return None
    return round(sum(window) / len(window), 3)


def runtime_status() -> dict:
    """Aggregate live mesh status for the dashboard header/indicator and modal."""
    cfg = load_config() or {}
    enabled = bool(cfg.get("enabled") and cfg.get("mesh_id") and cfg.get("node_id"))
    peers = active_peers()
    with _RUNTIME_LOCK:
        syncing = bool(_RUNTIME.get("syncing"))
        last_sync_ok = _RUNTIME.get("last_sync_ok")
        last_sync_at = _RUNTIME.get("last_sync_at")
        running = _RUNTIME.get("server") is not None
        last_scan = _RUNTIME.get("last_scan")
    adjacent = []
    if last_scan:
        adjacent = [m for m in last_scan.get("meshes", [])
                    if not m.get("is_current") and m.get("node_count", 0) >= 1]
    return {
        "available": True,
        "enabled": enabled,
        "running": running,
        "mesh_id": cfg.get("mesh_id") if enabled else None,
        "node_id": cfg.get("node_id") if enabled else None,
        "mesh_name": (cfg.get("mesh_id") or "").rpartition("-")[0] if enabled else None,
        "connected": enabled and len(peers) >= 1,
        "peer_count": len(peers),
        "active_peers": peers,
        "syncing": syncing,
        "last_sync_ok": last_sync_ok,
        "last_sync_at": last_sync_at,
        "uptime_seconds": mesh_uptime_seconds() if enabled else None,
        "mean_propagation_seconds": mean_propagation_seconds() if enabled else None,
        "adjacent_meshes": adjacent,
        "listen_port": int(cfg.get("listen_port", DEFAULT_PORT)),
    }


# --------------------------------------------------------------------------- #
# Runtime create / join / leave (invoked from the dashboard control API)
# --------------------------------------------------------------------------- #
def leave_mesh() -> dict:
    """Leave the current mesh: stop replicating and clear the mesh identity.

    History (oplog + base tables) is preserved. The stable ``node_id`` is kept so
    re-joining later reuses the same identity. Only one mesh can be joined at a
    time, so leaving fully clears ``mesh_id`` and configured peers.
    """
    stop_mesh()
    cfg = load_config() or {}
    left = cfg.get("mesh_id")
    cfg["enabled"] = False
    cfg["mesh_id"] = None
    cfg["peers"] = []
    save_config(cfg)
    with _RUNTIME_LOCK:
        _RUNTIME["prop_seconds"] = deque(maxlen=PROPAGATION_WINDOW)
        _RUNTIME["prop_recorded"] = set()
        _RUNTIME["active_peers"] = {}
    log(f"left mesh {left}")
    return {"left": left}


def _current_app_version() -> str:
    with _RUNTIME_LOCK:
        return _RUNTIME.get("app_version") or ""


def create_mesh_runtime(name: str | None = None, port: int = DEFAULT_PORT,
                        advertise: str | None = None,
                        peers: list[str] | None = None, restart: bool = True) -> dict:
    """Create a new mesh on this node and (optionally) bring it up immediately."""
    cfg, emitted = enable_new_mesh(name, port, advertise, peers or [])
    started = start_mesh(_current_app_version()) if restart else False
    return {"mesh_id": cfg["mesh_id"], "node_id": cfg["node_id"],
            "backfilled": emitted, "started": started}


def join_mesh_runtime(mesh_id: str, port: int = DEFAULT_PORT, advertise: str | None = None,
                      peers: list[str] | None = None, restart: bool = True) -> dict:
    """Join an existing mesh by id and (optionally) bring it up immediately.

    Leaving any current mesh first enforces the single-mesh invariant.
    """
    if load_config() and (load_config() or {}).get("mesh_id"):
        leave_mesh()
    cfg, emitted = join_mesh(mesh_id, port, advertise, peers or [])
    started = start_mesh(_current_app_version()) if restart else False
    return {"mesh_id": cfg["mesh_id"], "node_id": cfg["node_id"],
            "backfilled": emitted, "started": started}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _new_node_config(mesh_id: str, port: int, advertise: str | None,
                     peers: list[str], existing: dict | None) -> dict:
    cfg = dict(existing or {})
    cfg["enabled"] = True
    cfg["node_id"] = cfg.get("node_id") or uuid.uuid4().hex   # stable across re-runs
    cfg["mesh_id"] = mesh_id
    cfg["schema_version"] = SCHEMA_VERSION
    cfg.setdefault("listen_host", "0.0.0.0")
    cfg["listen_port"] = port
    cfg["advertise_host"] = advertise or cfg.get("advertise_host") or detect_lan_ip()
    cfg["advertise_port"] = port
    merged = sorted(set(cfg.get("peers") or []) | set(peers))
    cfg["peers"] = merged
    cfg.setdefault("sync_interval_seconds", DEFAULT_SYNC_INTERVAL)
    cfg.setdefault("log_level", DEFAULT_LOG_LEVEL)
    cfg.setdefault("max_retries", DEFAULT_MAX_RETRIES)
    cfg.setdefault("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS)
    cfg.setdefault("retention_days", DEFAULT_RETENTION_DAYS)
    cfg.setdefault("retention_keep_per_origin", DEFAULT_RETENTION_KEEP_PER_ORIGIN)
    cfg.setdefault("compress_payloads", DEFAULT_COMPRESS_PAYLOADS)
    return cfg


def enable_new_mesh(name: str | None = None, port: int = DEFAULT_PORT,
                    advertise: str | None = None,
                    peers: list[str] | None = None) -> tuple[dict, int]:
    """Create (or re-affirm) a mesh on this node and backfill local history.

    Public entry point used by the installer and the ``init`` CLI command.
    Reuses the existing mesh id when no new name is supplied.
    """
    existing = load_config()
    if name or not (existing and existing.get("mesh_id")):
        mesh_id = make_mesh_id(name or "mesh")
    else:
        mesh_id = existing["mesh_id"]
    cfg = _new_node_config(mesh_id, port, advertise, peers or [], existing)
    save_config(cfg)
    with _db() as conn:
        ensure_schema(conn)
        emitted = backfill(conn, cfg["node_id"])
    return cfg, emitted


def join_mesh(mesh_id: str, port: int = DEFAULT_PORT, advertise: str | None = None,
              peers: list[str] | None = None) -> tuple[dict, int]:
    """Join an existing mesh by id, preserving node identity and local history."""
    cfg = _new_node_config(mesh_id, port, advertise, peers or [], load_config())
    save_config(cfg)
    with _db() as conn:
        ensure_schema(conn)
        emitted = backfill(conn, cfg["node_id"])
    return cfg, emitted


def compact_oplog(cfg: dict | None = None, db_path: Path | None = None, dry_run: bool = False) -> dict:
    cfg = normalize_config(cfg or load_config() or {}) or {}
    retention_days = int(cfg.get("retention_days", DEFAULT_RETENTION_DAYS) or 0)
    keep_per_origin = int(cfg.get("retention_keep_per_origin", DEFAULT_RETENTION_KEEP_PER_ORIGIN) or 0)
    if retention_days <= 0:
        return {"deleted": 0, "kept": 0, "dry_run": dry_run, "retention_days": retention_days}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with _db(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT event_id, origin_node, created_at, lamport FROM oplog ORDER BY origin_node, lamport"
        ).fetchall()
        by_origin: dict[str, list[tuple[str, str, int]]] = {}
        for event_id, origin, created_at, lamport in rows:
            by_origin.setdefault(origin, []).append((event_id, created_at, lamport))
        delete_ids: list[str] = []
        for origin, events in by_origin.items():
            newer_count = sum(1 for _, created_at, _ in events if created_at >= cutoff)
            if newer_count == 0:
                continue
            old_events = [event for event in events if event[1] < cutoff]
            if not old_events:
                continue
            if keep_per_origin <= 0:
                delete_ids.extend(eid for eid, _, _ in old_events)
                continue
            keep_count = max(0, keep_per_origin - newer_count)
            if keep_count <= 0:
                delete_ids.extend(eid for eid, _, _ in old_events)
                continue
            if len(old_events) > keep_count:
                delete_ids.extend(eid for eid, _, _ in old_events[:-keep_count])
        if dry_run:
            return {"deleted": len(delete_ids), "kept": len(rows) - len(delete_ids), "dry_run": True, "retention_days": retention_days}
        if delete_ids:
            placeholders = ",".join("?" for _ in delete_ids)
            conn.execute(f"DELETE FROM oplog WHERE event_id IN ({placeholders})", delete_ids)
            conn.commit()
        return {"deleted": len(delete_ids), "kept": len(rows) - len(delete_ids), "dry_run": False, "retention_days": retention_days}


def migrate_mesh_schema(cfg: dict | None = None, db_path: Path | None = None) -> dict:
    cfg = normalize_config(cfg or load_config() or {}) or {}
    cfg["schema_version"] = SCHEMA_VERSION
    save_config(cfg)
    with _db(db_path) as conn:
        ensure_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO mesh_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    return {"schema_version": SCHEMA_VERSION, "migrated": True}


def cmd_init(args) -> int:
    cfg, emitted = enable_new_mesh(args.name, args.port, args.advertise, args.peer or [])
    print(f"Mesh initialized.\n  mesh id:   {cfg['mesh_id']}\n  node id:   {cfg['node_id']}")
    print(f"  listen:    {cfg['listen_host']}:{cfg['listen_port']}")
    print(f"  advertise: {cfg['advertise_host']}:{cfg['advertise_port']}")
    print(f"  backfilled {emitted} event(s) from existing history.")
    print("\nShare this mesh id with other nodes so they can join:")
    print(f"  python drachometer_mesh.py join {cfg['mesh_id']} "
          f"--peer {cfg['advertise_host']}:{cfg['advertise_port']}")
    return 0


def cmd_join(args) -> int:
    cfg, emitted = join_mesh(args.mesh_id, args.port, args.advertise, args.peer or [])
    print(f"Joined mesh {cfg['mesh_id']} as node {cfg['node_id']}.")
    print(f"  peers: {', '.join(cfg['peers']) or '(none yet)'}")
    print(f"  backfilled {emitted} local event(s); they will replicate to peers.")
    return 0


def cmd_import(args) -> int:
    other = Path(args.database)
    if not other.exists():
        print(f"ERROR: {other} does not exist.", file=sys.stderr)
        return 1
    with _db() as conn:
        ensure_schema(conn)
        applied = import_database(conn, other, args.as_label)
    print(f"Imported {applied} new event(s) from {other}.")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    if not cfg:
        print("Mesh is not configured. Run 'init' or 'join' to enable it.")
        return 0
    print(f"enabled:   {bool(cfg.get('enabled'))}")
    print(f"mesh id:   {cfg.get('mesh_id')}")
    print(f"node id:   {cfg.get('node_id')}")
    print(f"listen:    {cfg.get('listen_host')}:{cfg.get('listen_port')}")
    print(f"advertise: {cfg.get('advertise_host')}:{cfg.get('advertise_port')}")
    print(f"schema:    {cfg.get('schema_version', SCHEMA_VERSION)}")
    try:
        with _db() as conn:
            ensure_schema(conn)
            counts = local_origin_counts(conn)
        print(f"oplog:     {sum(counts.values())} event(s) across {len(counts)} origin(s)")
        for origin, count in sorted(counts.items()):
            mine = " (this node)" if origin == cfg.get("node_id") else ""
            print(f"             {origin}: {count}{mine}")
    except sqlite3.Error as exc:
        print(f"oplog:     unavailable ({exc})")
    peers = cfg.get("peers") or []
    print(f"peers:     {len(peers)}")
    for peer in peers:
        try:
            hello = _get_json(peer, _mesh_path("/mesh/hello", cfg.get("mesh_id")), timeout=3.0)
            ok = "reachable" if hello.get("ok") else "MESH MISMATCH"
            print(f"             {peer}: {ok}")
        except Exception as exc:
            print(f"             {peer}: unreachable ({exc})")
    metrics = collect_health_metrics(cfg)
    print("health:")
    print(f"  alert: {metrics['alert']}")
    print(f"  peer reachability: {metrics['peer_reachability']['reachable']}/{metrics['peer_reachability']['total']}")
    print(f"  replication lag: {metrics['replication_lag_seconds']}s")
    print(f"  dedupe rate: {metrics['dedupe_rate']}")
    print(f"  conflict rate: {metrics['conflict_rate']}")
    print(f"  failed sync attempts: {metrics['failed_sync_attempts']}")
    return 0


def cmd_compact(args) -> int:
    cfg = load_config()
    if not cfg:
        print("Mesh is not configured.")
        return 0
    summary = compact_oplog(cfg, dry_run=args.dry_run)
    action = "Would delete" if args.dry_run else "Deleted"
    print(f"{action} {summary['deleted']} oplog event(s); {summary['kept']} retained.")
    return 0


def cmd_migrate(args) -> int:
    cfg = load_config()
    if not cfg:
        print("Mesh is not configured.")
        return 0
    result = migrate_mesh_schema(cfg)
    print(f"Mesh schema metadata synchronized to version {result['schema_version']}.")
    return 0


def cmd_disable(args) -> int:
    cfg = load_config()
    if not cfg:
        print("Mesh is not configured.")
        return 0
    cfg["enabled"] = False
    save_config(cfg)
    print("Mesh disabled. History is preserved; re-enable with 'init' or 'join'.")
    return 0


def cmd_leave(args) -> int:
    cfg = load_config()
    if not (cfg and cfg.get("mesh_id")):
        print("Not a member of any mesh.")
        return 0
    result = leave_mesh()
    print(f"Left mesh {result['left']}. History is preserved; join another with 'join'.")
    return 0


def cmd_discover(args) -> int:
    result = discover_meshes(port=args.port)
    print(f"Scanned {result['scanned_hosts']} host(s) across {len(result['subnets'])} subnet(s): "
          f"{', '.join(result['subnets']) or '(none)'}")
    if not result["meshes"]:
        print("No mesh networks found.")
        return 0
    for m in result["meshes"]:
        marker = " (current)" if m["is_current"] else ""
        print(f"  {m['name']}-{m['suffix']}{marker}: {m['node_count']} node(s)")
        for node in m["nodes"]:
            print(f"      {node['advertise']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drachometer_mesh.py",
        description="Mesh replication for drachometer (LAN/VM only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create a new mesh on this node.")
    p_init.add_argument("--name", help="Human label for the mesh (e.g. 'home').")
    p_init.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_init.add_argument("--advertise", help="Advertise host/IP peers use to reach this node.")
    p_init.add_argument("--peer", action="append", help="Seed peer HOST:PORT (repeatable).")
    p_init.set_defaults(func=cmd_init)

    p_join = sub.add_parser("join", help="Join an existing mesh by its id.")
    p_join.add_argument("mesh_id", help="The mesh id to join (e.g. 'home-a1b2c3d4').")
    p_join.add_argument("--peer", action="append", help="Seed peer HOST:PORT (repeatable).")
    p_join.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_join.add_argument("--advertise", help="Advertise host/IP peers use to reach this node.")
    p_join.set_defaults(func=cmd_join)

    p_import = sub.add_parser("import", help="Merge records from another database file.")
    p_import.add_argument("database", help="Path to another drachometer.db to merge in.")
    p_import.add_argument("--as", dest="as_label", help="Synthetic origin label for the import.")
    p_import.set_defaults(func=cmd_import)

    p_status = sub.add_parser("status", help="Show mesh configuration and peer reachability.")
    p_status.set_defaults(func=cmd_status)

    p_compact = sub.add_parser("compact", help="Compact old oplog history using the retention policy.")
    p_compact.add_argument("--dry-run", action="store_true", help="Report what would be deleted without modifying the database.")
    p_compact.set_defaults(func=cmd_compact)

    p_migrate = sub.add_parser("migrate", help="Refresh mesh schema metadata and rejoin healthy state.")
    p_migrate.set_defaults(func=cmd_migrate)

    sub.add_parser("disable", help="Disable mesh replication (history preserved)." ).set_defaults(func=cmd_disable)

    sub.add_parser("leave", help="Leave the current mesh (history preserved)." ).set_defaults(func=cmd_leave)

    p_discover = sub.add_parser("discover", help="Scan local subnets for reachable mesh networks.")
    p_discover.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_discover.set_defaults(func=cmd_discover)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
