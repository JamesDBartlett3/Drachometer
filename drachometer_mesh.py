#!/usr/bin/env python3
"""Mesh replication for drachometer (phase 1).

Append-only, event-sourced replication for trusted LAN/VM networks. Every local
write to ``turns`` or ``tool_calls`` emits an immutable event into an ``oplog``
table, keyed by a *content hash* so applying the same event any number of times
is a no-op. Nodes gossip over plain stdlib HTTP using pull-based anti-entropy
(compare per-origin digests, fetch the events you are missing): no broker, no
third-party dependencies.

Scope is deliberately limited to LAN/VM networks. The mesh identifier
(``<name>-<8 hex>``) prevents *accidental* cross-merges between unrelated meshes
that happen to share a LAN (coworkers, roommates); it is **not** a security
boundary -- there is no authentication and no TLS. Do not expose mesh ports to
the public internet.

This file is both an importable library (the hook and the report server import
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
import hashlib
import json
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
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

# Serializes oplog application *within* this process; WAL + busy_timeout handle
# cross-process contention (the hook writes from a separate process).
_APPLY_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Logging (intentionally minimal for phase 1; richer logging is phase 2).
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config() -> dict | None:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
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
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the oplog and the tool_calls.uid identity if absent. Idempotent."""
    conn.executescript(OPLOG_SCHEMA)
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
def _project_turn(conn: sqlite3.Connection, p: dict) -> None:
    model_id = ensure_model_row(conn, p.get("model_key"))
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


def _project_tool_call(conn: sqlite3.Connection, p: dict) -> None:
    # Resolve the local turn primary key from the global natural key. May be
    # NULL if the parent turn has not replicated yet; queries can still join on
    # (session_id, turn_id), which is carried on every tool_call row.
    turn_pk = conn.execute(
        "SELECT id FROM turns WHERE session_id = ? AND turn_id = ?",
        (p.get("session_id"), p.get("turn_id")),
    ).fetchone()
    conn.execute(
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
        return False
    if ev["entity"] == "turn":
        _project_turn(conn, payload)
    elif ev["entity"] == "tool_call":
        _project_tool_call(conn, payload)
    return True


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
# HTTP transport -- mesh endpoints (separate from the loopback report server).
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
        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if path == "/mesh/hello":
                self._send_json({
                    "mesh_id": cfg["mesh_id"],
                    "node_id": cfg["node_id"],
                    "schema_version": SCHEMA_VERSION,
                    "app_version": app_version,
                })
            elif path == "/mesh/digest":
                with _db(db_path) as conn:
                    origins = local_origin_counts(conn)
                self._send_json({
                    "mesh_id": cfg["mesh_id"],
                    "schema_version": SCHEMA_VERSION,
                    "origins": origins,
                })
            elif path == "/mesh/event-ids":
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
                # Startup registration: a peer tells us how to reach it.
                advertise = body.get("advertise")
                if body.get("mesh_id") == cfg["mesh_id"] and advertise:
                    registry.add(advertise)
                self._send_json({"ok": True, "mesh_id": cfg["mesh_id"]})
            else:
                self._send_json({"error": "not found"}, status=404)

        def log_message(self, *args):  # silence default stderr logging
            pass

    return MeshHandler


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# HTTP client + gossip
# --------------------------------------------------------------------------- #
def _get_json(peer: str, path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"http://{peer}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(peer: str, path: str, body: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://{peer}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def sync_with_peer(cfg: dict, peer: str) -> int:
    """Pull every event this node is missing from one peer. Returns count applied."""
    hello = _get_json(peer, "/mesh/hello")
    if hello.get("mesh_id") != cfg["mesh_id"]:
        log(f"skip {peer}: mesh id mismatch ({hello.get('mesh_id')!r} != {cfg['mesh_id']!r})")
        return 0
    if hello.get("schema_version") != SCHEMA_VERSION:
        log(f"skip {peer}: schema version {hello.get('schema_version')} != {SCHEMA_VERSION}")
        return 0

    remote = _get_json(peer, "/mesh/digest")
    with _db() as conn:
        local_counts = local_origin_counts(conn)
    applied = 0
    for origin, remote_count in (remote.get("origins") or {}).items():
        if local_counts.get(origin, 0) >= remote_count:
            continue
        remote_ids = _get_json(peer, f"/mesh/event-ids?origin={origin}").get("ids", [])
        with _db() as conn:
            have = {
                r[0] for r in conn.execute(
                    "SELECT event_id FROM oplog WHERE origin_node = ?", (origin,)
                )
            }
        missing = [i for i in remote_ids if i not in have]
        for batch in _chunks(missing, FETCH_BATCH):
            events = _post_json(peer, "/mesh/events", {"ids": batch}).get("events", [])
            with _APPLY_LOCK, connect() as conn:
                for ev in events:
                    if apply_event(conn, ev):
                        applied += 1
                conn.commit()
    if applied:
        log(f"sync {peer}: applied {applied} event(s)")
    return applied


def sync_round(cfg: dict, registry: _PeerRegistry) -> int:
    total = 0
    for peer in registry.all():
        try:
            total += sync_with_peer(cfg, peer)
        except Exception as exc:  # network/peer errors are expected and non-fatal
            log(f"sync error {peer}: {exc}")
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


def start_mesh(app_version: str = "", db_path: Path | None = None) -> bool:
    """Start the mesh HTTP server and gossip daemon if mesh is enabled.

    Returns True if mesh was started. Safe to call from the report server; all
    work happens on daemon threads.
    """
    cfg = load_config()
    if not (cfg and cfg.get("enabled") and cfg.get("mesh_id") and cfg.get("node_id")):
        return False

    with _db(db_path) as conn:
        ensure_schema(conn)

    registry = _PeerRegistry(cfg)
    host = cfg.get("listen_host", "0.0.0.0")
    port = int(cfg.get("listen_port", DEFAULT_PORT))
    handler = _make_mesh_handler(cfg, registry, app_version, db_path)
    try:
        server = _ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        log(f"mesh server bind failed on {host}:{port}: {exc}")
        return False

    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"mesh server listening on {host}:{port} (mesh_id={cfg['mesh_id']})")

    def _daemon():
        _announce(cfg, registry)  # startup registration
        interval = int(cfg.get("sync_interval_seconds", DEFAULT_SYNC_INTERVAL))
        while True:
            sync_round(cfg, registry)
            time.sleep(interval)

    threading.Thread(target=_daemon, daemon=True).start()
    return True


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
            hello = _get_json(peer, "/mesh/hello", timeout=3.0)
            ok = "reachable" if hello.get("mesh_id") == cfg.get("mesh_id") else "MESH MISMATCH"
            print(f"             {peer}: {ok}")
        except Exception as exc:
            print(f"             {peer}: unreachable ({exc})")
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

    sub.add_parser("status", help="Show mesh configuration and peer reachability.").set_defaults(func=cmd_status)
    sub.add_parser("disable", help="Disable mesh replication (history preserved).").set_defaults(func=cmd_disable)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
