#!/usr/bin/env python3
"""Tests for drachometer mesh replication (phase 1).

Stdlib unittest only -- no third-party dependencies, matching the project.
Run with:  python -m unittest discover -s tests
"""

import json
import os
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import drachometer_mesh as mesh  # noqa: E402

# Minimal slice of the production schema sufficient to exercise replication.
BASE_SCHEMA = """
CREATE TABLE models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key TEXT NOT NULL UNIQUE,
    model_name TEXT, model_version TEXT, model_provider TEXT,
    input_price_per_mtok REAL, output_price_per_mtok REAL,
    cache_read_price_per_mtok REAL, cache_creation_price_per_mtok REAL
);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, turn_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
    stop_reason TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cwd TEXT, git_branch TEXT, model TEXT,
    model_id INTEGER REFERENCES models(id),
    UNIQUE(session_id, turn_id)
);
CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_pk INTEGER REFERENCES turns(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL, turn_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
    tool_name TEXT, tool_input TEXT, exit_code INTEGER, error TEXT
);
"""


def seed_db(path: Path, node_id: str, sessions, model_key="claude-opus-4-8"):
    """Create a schema-complete DB, insert one turn per session, emit events."""
    conn = sqlite3.connect(path)
    conn.executescript(BASE_SCHEMA)
    mesh.ensure_schema(conn)
    mid = mesh.ensure_model_row(conn, model_key)
    for i, sess in enumerate(sessions):
        recorded = f"2026-06-26T10:0{i}:00+00:00"
        conn.execute(
            """INSERT INTO turns (session_id, turn_id, recorded_at, stop_reason,
                   input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                   cwd, git_branch, model, model_id)
               VALUES (?, 'turn-1', ?, 'end_turn', 100, 50, 0, 0, '/tmp', 'main', ?, ?)""",
            (sess, recorded, model_key, mid),
        )
        conn.execute(
            """INSERT INTO tool_calls (uid, turn_pk, session_id, turn_id, recorded_at,
                   tool_name, tool_input, exit_code, error)
               VALUES (?, NULL, ?, 'turn-1', ?, 'Bash', '{}', 0, NULL)""",
            (f"{node_id}-tc-{i}", sess, recorded),
        )
    conn.commit()
    mesh.backfill(conn, node_id)
    conn.close()


def turn_sessions(path: Path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT session_id FROM turns")}
    finally:
        conn.close()


def event_count(path: Path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM oplog").fetchone()[0]
    finally:
        conn.close()


class MeshTestBase(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows a mesh server's daemon thread or a
        # lingering WAL handle can briefly hold a DB file past server shutdown.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        # Registered first -> runs last (LIFO), after any server shutdown a test
        # adds, so the DB files are unlocked before the temp dir is removed.
        self.addCleanup(self._tmp.cleanup)
        # Redirect module globals so nothing touches the real ~/.claude.
        self._orig = (mesh.DB_PATH, mesh.CONFIG_PATH, mesh.LOG_PATH)
        self.addCleanup(self._restore_globals)
        mesh.DB_PATH = self.tmp / "drachometer.db"
        mesh.CONFIG_PATH = self.tmp / "mesh.json"
        mesh.LOG_PATH = self.tmp / "mesh.log"

    def _restore_globals(self):
        mesh.DB_PATH, mesh.CONFIG_PATH, mesh.LOG_PATH = self._orig

    def _reserve_port(self) -> tuple[int, socket.socket]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        return sock.getsockname()[1], sock

    def _mesh_server_script(self, db_path: Path, config_path: Path, log_path: Path) -> str:
        repo_root = Path(__file__).resolve().parent.parent
        return (
            "import os\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "import drachometer_mesh as mesh\n"
            f"mesh.DB_PATH = Path({str(db_path)!r})\n"
            f"mesh.CONFIG_PATH = Path({str(config_path)!r})\n"
            f"mesh.LOG_PATH = Path({str(log_path)!r})\n"
            "mesh.ensure_schema(mesh.connect(mesh.DB_PATH))\n"
            "if os.environ.get('MESH_LISTEN_FD'):\n"
            "    mesh.start_mesh(app_version='test', db_path=mesh.DB_PATH, inherited_socket=int(os.environ['MESH_LISTEN_FD']))\n"
            "else:\n"
            "    mesh.start_mesh(app_version='test', db_path=mesh.DB_PATH)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )

    def _spawn_mesh_node(self, db_path: Path, node_id: str, mesh_id: str, port: int, listen_socket: socket.socket | None = None) -> subprocess.Popen:
        config_path = self.tmp / f"{node_id}-mesh.json"
        log_path = self.tmp / f"{node_id}-mesh.log"
        cfg = {
            "enabled": True,
            "mesh_id": mesh_id,
            "node_id": node_id,
            "schema_version": mesh.SCHEMA_VERSION,
            "listen_host": "127.0.0.1",
            "listen_port": port,
            "advertise_host": "127.0.0.1",
            "advertise_port": port,
            "peers": [],
            "sync_interval_seconds": 1,
            "log_level": "info",
            "max_retries": 1,
            "retry_backoff_seconds": 0.1,
            "retention_days": 0,
            "retention_keep_per_origin": 50,
            "compress_payloads": False,
        }
        mesh.CONFIG_PATH = config_path
        mesh.save_config(cfg)
        mesh.LOG_PATH = log_path
        env = os.environ.copy()
        pass_fds = []
        if listen_socket is not None:
            fd = listen_socket.fileno()
            os.set_inheritable(fd, True)
            env["MESH_LISTEN_FD"] = str(fd)
            pass_fds.append(fd)
        proc = subprocess.Popen(
            [sys.executable, "-c", self._mesh_server_script(db_path, config_path, log_path)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            pass_fds=pass_fds,
        )
        self.addCleanup(self._terminate_process, proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/mesh/hello", timeout=0.5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    if payload.get("node_id") == node_id:
                        return proc
            except Exception:
                time.sleep(0.1)
        self._terminate_process(proc)
        raise RuntimeError(f"mesh node {node_id} failed to start on port {port}")

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


class TestEventIdentity(MeshTestBase):
    def test_emit_is_idempotent_on_identical_content(self):
        db = self.tmp / "a.db"
        seed_db(db, "nodeA", ["s1"])
        conn = sqlite3.connect(db)
        before = conn.execute("SELECT COUNT(*) FROM oplog").fetchone()[0]
        # Re-emit the exact same turn content: content hash collides -> no-op.
        mesh.emit_event(conn, "nodeA", "turn", mesh.turn_payload({
            "session_id": "s1", "turn_id": "turn-1",
            "recorded_at": "2026-06-26T10:00:00+00:00", "stop_reason": "end_turn",
            "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cwd": "/tmp", "git_branch": "main",
            "model_key": "claude-opus-4-8",
        }))
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM oplog").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)

    def test_apply_event_idempotent(self):
        src = self.tmp / "src.db"
        dst = self.tmp / "dst.db"
        seed_db(src, "nodeA", ["s1"])
        conn_dst = sqlite3.connect(dst)
        conn_dst.executescript(BASE_SCHEMA)
        mesh.ensure_schema(conn_dst)
        conn_src = sqlite3.connect(src)
        events = [dict(zip(
            ["event_id", "origin_node", "lamport", "created_at", "entity", "op", "payload"], r))
            for r in conn_src.execute(
                "SELECT event_id, origin_node, lamport, created_at, entity, op, payload FROM oplog")]
        conn_src.close()
        first = sum(mesh.apply_event(conn_dst, ev) for ev in events)
        second = sum(mesh.apply_event(conn_dst, ev) for ev in events)
        conn_dst.commit()
        self.assertEqual(first, len(events))
        self.assertEqual(second, 0)  # replay applies nothing
        conn_dst.close()

    def test_lww_does_not_overwrite_newer_turn(self):
        dst = self.tmp / "dst.db"
        conn = sqlite3.connect(dst)
        conn.executescript(BASE_SCHEMA)
        mesh.ensure_schema(conn)
        newer = {"session_id": "s1", "turn_id": "turn-1",
                 "recorded_at": "2026-06-26T12:00:00+00:00", "stop_reason": "end_turn",
                 "input_tokens": 999, "output_tokens": 0, "cache_read_tokens": 0,
                 "cache_creation_tokens": 0, "cwd": None, "git_branch": None,
                 "model_key": "claude-opus-4-8"}
        older = dict(newer, recorded_at="2026-06-26T08:00:00+00:00", input_tokens=1)
        mesh.apply_event(conn, {"event_id": "e-new", "origin_node": "n", "lamport": 2,
                                "created_at": newer["recorded_at"], "entity": "turn",
                                "op": "upsert", "payload": newer})
        mesh.apply_event(conn, {"event_id": "e-old", "origin_node": "n", "lamport": 1,
                                "created_at": older["recorded_at"], "entity": "turn",
                                "op": "upsert", "payload": older})
        conn.commit()
        tokens = conn.execute(
            "SELECT input_tokens FROM turns WHERE session_id='s1'").fetchone()[0]
        conn.close()
        self.assertEqual(tokens, 999)  # newer record wins regardless of apply order


class TestImportMerge(MeshTestBase):
    def test_import_merges_independent_client_without_oplog(self):
        foreign = self.tmp / "foreign.db"
        # A foreign DB that was never mesh-enabled: build it then drop its oplog.
        seed_db(foreign, "ignored", ["x1", "x2"])
        fc = sqlite3.connect(foreign)
        fc.executescript("DROP TABLE oplog;")
        fc.commit()
        fc.close()

        local = self.tmp / "local.db"
        seed_db(local, "nodeLocal", ["l1"])
        conn = sqlite3.connect(local)
        applied_first = mesh.import_database(conn, foreign, label="bob")
        applied_again = mesh.import_database(conn, foreign, label="bob")
        conn.close()

        self.assertGreater(applied_first, 0)
        self.assertEqual(applied_again, 0)  # re-import is idempotent
        self.assertEqual(turn_sessions(local), {"l1", "x1", "x2"})


class TestMeshPhaseTwoFeatures(MeshTestBase):
    def test_config_defaults_and_schema_metadata(self):
        cfg = mesh.normalize_config({"mesh_id": "test-mesh", "node_id": "nodeA"})
        self.assertEqual(cfg["log_level"], "info")
        self.assertEqual(cfg["max_retries"], 3)
        self.assertEqual(cfg["retention_days"], 0)
        db = self.tmp / "meta.db"
        conn = sqlite3.connect(db)
        try:
            mesh.ensure_schema(conn)
            row = conn.execute("SELECT value FROM mesh_meta WHERE key = 'schema_version'").fetchone()
            self.assertEqual(row[0], str(mesh.SCHEMA_VERSION))
        finally:
            conn.close()

    def test_compact_oplog_preserves_recent_history(self):
        db = self.tmp / "compact.db"
        conn = sqlite3.connect(db)
        try:
            conn.executescript(BASE_SCHEMA)
            mesh.ensure_schema(conn)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            new_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO oplog (event_id, origin_node, lamport, created_at, entity, op, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("old-1", "nodeA", 1, old_ts, "turn", "upsert", "{}"),
            )
            conn.execute(
                "INSERT INTO oplog (event_id, origin_node, lamport, created_at, entity, op, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("new-1", "nodeA", 2, new_ts, "turn", "upsert", "{}"),
            )
            conn.commit()
            summary = mesh.compact_oplog({"retention_days": 3, "retention_keep_per_origin": 1}, db_path=db)
            self.assertEqual(summary["deleted"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM oplog").fetchone()[0], 1)
        finally:
            conn.close()

    def test_collect_health_metrics_reports_alerts(self):
        mesh.reset_metrics()
        cfg = {"mesh_id": "test-mesh", "node_id": "nodeA", "peers": ["127.0.0.1:9999"]}
        metrics = mesh.collect_health_metrics(cfg, db_path=self.tmp / "health.db")
        self.assertEqual(metrics["peer_reachability"]["total"], 1)
        self.assertEqual(metrics["alert"], "unreachable peers")
        self.assertIn("dedupe_rate", metrics)
        self.assertIn("conflict_rate", metrics)


class TestTwoNodeConvergence(MeshTestBase):
    def _serve(self, cfg, db_path):
        port, listen_socket = self._reserve_port()
        self._spawn_mesh_node(db_path, cfg["node_id"], cfg["mesh_id"], port, listen_socket=listen_socket)
        return port

    def test_bidirectional_convergence_and_idempotency(self):
        db_a = self.tmp / "a.db"
        db_b = self.tmp / "b.db"
        seed_db(db_a, "nodeA", ["a1", "a2", "a3"])
        seed_db(db_b, "nodeB", ["b1", "b2"])
        cfg_a = {"mesh_id": "test-mesh", "node_id": "nodeA", "peers": []}
        cfg_b = {"mesh_id": "test-mesh", "node_id": "nodeB", "peers": []}

        port_a = self._serve(cfg_a, db_a)
        port_b = self._serve(cfg_b, db_b)

        # A pulls from B (local DB = A), then B pulls from A (local DB = B).
        mesh.DB_PATH = db_a
        applied_ab = mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_b}")
        mesh.DB_PATH = db_b
        applied_ba = mesh.sync_with_peer(cfg_b, f"127.0.0.1:{port_a}")

        self.assertGreater(applied_ab, 0)
        self.assertGreater(applied_ba, 0)
        all_sessions = {"a1", "a2", "a3", "b1", "b2"}
        self.assertEqual(turn_sessions(db_a), all_sessions)
        self.assertEqual(turn_sessions(db_b), all_sessions)
        self.assertEqual(event_count(db_a), event_count(db_b))

        def turn_rows(path: Path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT session_id, turn_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens FROM turns ORDER BY session_id, turn_id"
                ).fetchall()
            finally:
                conn.close()

        def tool_rows(path: Path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT uid, session_id, turn_id, tool_name, exit_code, error FROM tool_calls ORDER BY uid"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(turn_rows(db_a), turn_rows(db_b))
        self.assertEqual(tool_rows(db_a), tool_rows(db_b))

        # Second round converges with nothing new to apply.
        mesh.DB_PATH = db_a
        self.assertEqual(mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_b}"), 0)

    def test_three_node_convergence_with_offline_rejoin(self):
        db_a = self.tmp / "a.db"
        db_b = self.tmp / "b.db"
        db_c = self.tmp / "c.db"
        seed_db(db_a, "nodeA", ["a1"])
        seed_db(db_b, "nodeB", ["b1"])
        seed_db(db_c, "nodeC", ["c1"])

        cfg_a = {"mesh_id": "test-mesh", "node_id": "nodeA", "peers": []}
        cfg_b = {"mesh_id": "test-mesh", "node_id": "nodeB", "peers": []}
        cfg_c = {"mesh_id": "test-mesh", "node_id": "nodeC", "peers": []}

        port_a = self._serve(cfg_a, db_a)
        port_b = self._serve(cfg_b, db_b)

        def add_activity(path: Path, node_id: str, session_id: str, turn_id: str):
            conn = sqlite3.connect(path)
            try:
                mesh.ensure_schema(conn)
                model_id = mesh.ensure_model_row(conn, "claude-opus-4-8")
                conn.execute(
                    """INSERT INTO turns (
                            session_id, turn_id, recorded_at, stop_reason,
                            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                            cwd, git_branch, model, model_id)
                        VALUES (?, ?, ?, 'end_turn', 200, 100, 10, 5, '/tmp', 'main', ?, ?)""",
                    (session_id, turn_id, f"2026-06-26T10:30:00+00:00", "claude-opus-4-8", model_id),
                )
                turn_pk = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                tool_call_id = conn.execute(
                    """INSERT INTO tool_calls (
                            uid, turn_pk, session_id, turn_id, recorded_at,
                            tool_name, tool_input, exit_code, error)
                        VALUES (?, ?, ?, ?, ?, 'Bash', '{}', 0, NULL)""",
                    (f"{node_id}-tc-{session_id}", turn_pk, session_id, turn_id,
                     f"2026-06-26T10:30:00+00:00"),
                ).lastrowid
                conn.commit()
                turn_row = conn.execute(
                    """SELECT t.session_id, t.turn_id, t.recorded_at, t.stop_reason,
                              t.input_tokens, t.output_tokens, t.cache_read_tokens,
                              t.cache_creation_tokens, t.cwd, t.git_branch, m.model_key
                         FROM turns t LEFT JOIN models m ON t.model_id = m.id
                         WHERE t.id = ?""",
                    (turn_pk,),
                ).fetchone()
                turn_payload = mesh.turn_payload(dict(zip([
                    "session_id", "turn_id", "recorded_at", "stop_reason",
                    "input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_creation_tokens", "cwd", "git_branch", "model_key"
                ], turn_row)))
                mesh.emit_event(conn, node_id, "turn", turn_payload)
                tool_row = conn.execute(
                    """SELECT uid, session_id, turn_id, recorded_at, tool_name,
                              tool_input, exit_code, error
                         FROM tool_calls WHERE id = ?""",
                    (tool_call_id,),
                ).fetchone()
                mesh.emit_event(conn, node_id, "tool_call", mesh.tool_call_payload(dict(zip([
                    "uid", "session_id", "turn_id", "recorded_at", "tool_name",
                    "tool_input", "exit_code", "error"
                ], tool_row))))
                conn.commit()
            finally:
                conn.close()

        def fetch_turn_rows(path: Path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT session_id, turn_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens FROM turns ORDER BY session_id, turn_id"
                ).fetchall()
            finally:
                conn.close()

        def fetch_tool_rows(path: Path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT uid, session_id, turn_id, tool_name, exit_code, error FROM tool_calls ORDER BY uid"
                ).fetchall()
            finally:
                conn.close()

        # C stays offline while A and B create and exchange their own records.
        add_activity(db_a, "nodeA", "a2", "turn-2")
        add_activity(db_b, "nodeB", "b2", "turn-2")
        mesh.DB_PATH = db_a
        mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_b}")
        mesh.DB_PATH = db_b
        mesh.sync_with_peer(cfg_b, f"127.0.0.1:{port_a}")

        self.assertEqual(fetch_turn_rows(db_a), fetch_turn_rows(db_b))
        self.assertEqual(fetch_tool_rows(db_a), fetch_tool_rows(db_b))

        port_c = self._serve(cfg_c, db_c)
        add_activity(db_c, "nodeC", "c2", "turn-2")

        for _ in range(2):
            mesh.DB_PATH = db_a
            mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_b}")
            mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_c}")
            mesh.DB_PATH = db_b
            mesh.sync_with_peer(cfg_b, f"127.0.0.1:{port_a}")
            mesh.sync_with_peer(cfg_b, f"127.0.0.1:{port_c}")
            mesh.DB_PATH = db_c
            mesh.sync_with_peer(cfg_c, f"127.0.0.1:{port_a}")
            mesh.sync_with_peer(cfg_c, f"127.0.0.1:{port_b}")

        self.assertEqual(fetch_turn_rows(db_a), fetch_turn_rows(db_b))
        self.assertEqual(fetch_turn_rows(db_b), fetch_turn_rows(db_c))
        self.assertEqual(fetch_tool_rows(db_a), fetch_tool_rows(db_b))
        self.assertEqual(fetch_tool_rows(db_b), fetch_tool_rows(db_c))
        self.assertEqual(event_count(db_a), event_count(db_b))
        self.assertEqual(event_count(db_b), event_count(db_c))

    def test_new_node_imports_historical_and_recent_records(self):
        db_a = self.tmp / "a.db"
        db_b = self.tmp / "b.db"
        db_joiner = self.tmp / "joiner.db"
        seed_db(db_a, "nodeA", ["a1"])
        seed_db(db_b, "nodeB", ["b1"])

        conn_joiner = sqlite3.connect(db_joiner)
        try:
            conn_joiner.executescript(BASE_SCHEMA)
            mesh.ensure_schema(conn_joiner)
            model_id = mesh.ensure_model_row(conn_joiner, "claude-opus-4-8")
            conn_joiner.execute(
                """INSERT INTO turns (
                        session_id, turn_id, recorded_at, stop_reason,
                        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                        cwd, git_branch, model, model_id)
                    VALUES (?, ?, ?, 'end_turn', 10, 5, 0, 0, '/tmp', 'main', ?, ?)""",
                ("pre-mesh", "turn-1", "2026-06-25T09:00:00+00:00", "claude-opus-4-8", model_id),
            )
            pre_turn_pk = conn_joiner.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn_joiner.execute(
                """INSERT INTO tool_calls (
                        uid, turn_pk, session_id, turn_id, recorded_at,
                        tool_name, tool_input, exit_code, error)
                    VALUES (?, ?, ?, ?, ?, 'Bash', '{}', 0, NULL)""",
                ("joiner-pre", pre_turn_pk, "pre-mesh", "turn-1", "2026-06-25T09:00:00+00:00"),
            )
            conn_joiner.execute(
                """INSERT INTO turns (
                        session_id, turn_id, recorded_at, stop_reason,
                        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                        cwd, git_branch, model, model_id)
                    VALUES (?, ?, ?, 'end_turn', 40, 20, 1, 0, '/tmp', 'main', ?, ?)""",
                ("concurrent", "turn-1", "2026-06-26T10:00:00+00:00", "claude-opus-4-8", model_id),
            )
            concurrent_turn_pk = conn_joiner.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn_joiner.execute(
                """INSERT INTO tool_calls (
                        uid, turn_pk, session_id, turn_id, recorded_at,
                        tool_name, tool_input, exit_code, error)
                    VALUES (?, ?, ?, ?, ?, 'Bash', '{}', 0, NULL)""",
                ("joiner-concurrent", concurrent_turn_pk, "concurrent", "turn-1", "2026-06-26T10:00:00+00:00"),
            )
            conn_joiner.execute(
                """INSERT INTO turns (
                        session_id, turn_id, recorded_at, stop_reason,
                        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                        cwd, git_branch, model, model_id)
                    VALUES (?, ?, ?, 'end_turn', 80, 40, 2, 1, '/tmp', 'main', ?, ?)""",
                ("newer", "turn-1", "2026-06-26T12:00:00+00:00", "claude-opus-4-8", model_id),
            )
            newer_turn_pk = conn_joiner.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn_joiner.execute(
                """INSERT INTO tool_calls (
                        uid, turn_pk, session_id, turn_id, recorded_at,
                        tool_name, tool_input, exit_code, error)
                    VALUES (?, ?, ?, ?, ?, 'Bash', '{}', 0, NULL)""",
                ("joiner-newer", newer_turn_pk, "newer", "turn-1", "2026-06-26T12:00:00+00:00"),
            )
            conn_joiner.commit()
            mesh.backfill(conn_joiner, "nodeJoiner")
            conn_joiner.commit()
        finally:
            conn_joiner.close()

        cfg_a = {"mesh_id": "test-mesh", "node_id": "nodeA", "peers": []}
        cfg_b = {"mesh_id": "test-mesh", "node_id": "nodeB", "peers": []}
        port_a = self._serve(cfg_a, db_a)
        port_b = self._serve(cfg_b, db_b)

        mesh.DB_PATH = db_a
        mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_b}")
        mesh.DB_PATH = db_b
        mesh.sync_with_peer(cfg_b, f"127.0.0.1:{port_a}")

        # Joiner imports existing history first, then catches up with the mesh.
        conn_joiner = sqlite3.connect(db_joiner)
        try:
            mesh.ensure_schema(conn_joiner)
            mesh.import_database(conn_joiner, db_a, label="joiner-from-a")
            mesh.import_database(conn_joiner, db_b, label="joiner-from-b")
            conn_joiner.commit()
        finally:
            conn_joiner.close()

        # The mesh then pulls from the joiner and converges.
        port_joiner = self._serve({"mesh_id": "test-mesh", "node_id": "nodeJoiner", "peers": []}, db_joiner)
        mesh.DB_PATH = db_a
        mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_joiner}")
        mesh.DB_PATH = db_b
        mesh.sync_with_peer(cfg_b, f"127.0.0.1:{port_joiner}")

        def fetch_turn_rows(path: Path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT session_id, turn_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens FROM turns ORDER BY session_id, turn_id"
                ).fetchall()
            finally:
                conn.close()

        def fetch_tool_rows(path: Path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT uid, session_id, turn_id, tool_name, exit_code, error FROM tool_calls ORDER BY uid"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(fetch_turn_rows(db_a), fetch_turn_rows(db_b))
        self.assertEqual(fetch_turn_rows(db_b), fetch_turn_rows(db_joiner))
        self.assertEqual(fetch_tool_rows(db_a), fetch_tool_rows(db_b))
        self.assertEqual(fetch_tool_rows(db_b), fetch_tool_rows(db_joiner))
        self.assertEqual(event_count(db_a), event_count(db_b))
        self.assertEqual(event_count(db_b), event_count(db_joiner))

    def test_mesh_id_mismatch_blocks_replication(self):
        db_a = self.tmp / "a.db"
        db_b = self.tmp / "b.db"
        seed_db(db_a, "nodeA", ["a1"])
        seed_db(db_b, "nodeB", ["b1"])
        cfg_b = {"mesh_id": "their-mesh", "node_id": "nodeB", "peers": []}
        port_b = self._serve(cfg_b, db_b)

        cfg_a = {"mesh_id": "my-mesh", "node_id": "nodeA", "peers": []}
        mesh.DB_PATH = db_a
        applied = mesh.sync_with_peer(cfg_a, f"127.0.0.1:{port_b}")
        self.assertEqual(applied, 0)
        self.assertEqual(turn_sessions(db_a), {"a1"})  # nothing leaked across meshes


if __name__ == "__main__":
    unittest.main(verbosity=2)
