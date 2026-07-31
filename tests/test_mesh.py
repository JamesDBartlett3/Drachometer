#!/usr/bin/env python3
"""Tests for drachometer mesh replication (phase 1).

Stdlib unittest only -- no third-party dependencies, matching the project.
Run with:  python -m unittest discover -s tests
"""

import json
import os
import sqlite3
import ipaddress
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
            if os.name != 'nt':
                os.set_inheritable(fd, True)
                pass_fds.append(fd)
            env["MESH_LISTEN_FD"] = str(fd)
        
        kwargs = {}
        if os.name != 'nt':
            kwargs['pass_fds'] = pass_fds
        else:
            kwargs['close_fds'] = False

        proc = subprocess.Popen(
            [sys.executable, "-c", self._mesh_server_script(db_path, config_path, log_path)],
            cwd=str(Path(__file__).resolve().parent.parent),
            start_new_session=True if os.name != 'nt' else False,
            env=env,
            **kwargs
        )
        self.addCleanup(self._terminate_process, proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                hello_url = f"http://127.0.0.1:{port}{mesh._mesh_path('/mesh/hello', mesh_id)}"
                with urllib.request.urlopen(hello_url, timeout=0.5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    if payload.get("ok") and payload.get("node_id") == node_id:
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


class TestSubnetDiscovery(MeshTestBase):
    def test_subnets_from_ips_deduplicates_and_masks(self):
        subnets = mesh.subnets_from_ips(
            ["192.168.1.50", "192.168.1.99", "10.0.0.4", "127.0.0.1"]
        )
        self.assertIn("192.168.1.0/24", subnets)
        self.assertIn("10.0.0.0/24", subnets)
        # 192.168.1.50 and .99 collapse to one /24
        self.assertEqual(len([s for s in subnets if s.startswith("192.168.1.")]), 1)

    def test_hosts_for_subnets_respects_cap(self):
        hosts = mesh._hosts_for_subnets(["10.0.0.0/24"], cap=5)
        self.assertEqual(len(hosts), 5)
        self.assertEqual(hosts[0], "10.0.0.1")

    def test_netmask_to_prefix_accepts_hex_dotted_and_bare(self):
        self.assertEqual(mesh._netmask_to_prefix("0xffffff00"), 24)
        self.assertEqual(mesh._netmask_to_prefix("255.255.254.0"), 23)
        self.assertEqual(mesh._netmask_to_prefix("16"), 16)
        self.assertIsNone(mesh._netmask_to_prefix("not-a-mask"))

    def test_parse_ip_addr_output_uses_real_prefix(self):
        text = (
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "2: eth0    inet 192.168.1.50/23 brd 192.168.1.255 scope global eth0\n"
        )
        self.assertEqual(mesh._parse_ip_addr_output(text),
                         [("127.0.0.1", 8), ("192.168.1.50", 23)])

    def test_parse_ifconfig_output_hex_and_dotted_masks(self):
        hexed = mesh._parse_ifconfig_output(
            "inet 10.0.0.5 netmask 0xffffff00 broadcast 10.0.0.255")
        self.assertEqual(hexed, [("10.0.0.5", 24)])
        dotted = mesh._parse_ifconfig_output(
            "inet 172.16.0.9  netmask 255.255.254.0  broadcast 172.16.1.255")
        self.assertEqual(dotted, [("172.16.0.9", 23)])

    def test_parse_windows_ipconfig_pairs_ip_and_mask(self):
        text = (
            "   IPv4 Address. . . . . . . . . . . : 192.168.0.20\n"
            "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
        )
        self.assertEqual(mesh._parse_windows_ipconfig_output(text),
                         [("192.168.0.20", 24)])

    def test_list_local_subnets_derives_from_active_nics(self):
        # Real NIC enumeration: subnets come from the interface netmask, not a
        # hard-coded /24. Assert each returned subnet is the network address of
        # a detected interface at its real prefix length.
        ifaces = mesh.list_local_interfaces()
        subnets = mesh.list_local_subnets()
        self.assertIsInstance(subnets, list)
        for ip, prefix in ifaces:
            expected = str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
            self.assertIn(expected, subnets)

    def test_list_local_subnets_falls_back_when_no_interfaces(self):
        orig = mesh.list_local_interfaces
        mesh.list_local_interfaces = lambda: []
        try:
            subnets = mesh.list_local_subnets(fallback_prefix=24)
        finally:
            mesh.list_local_interfaces = orig
        for cidr in subnets:
            self.assertTrue(cidr.endswith("/24"))

    def test_group_meshes_marks_current_and_counts_nodes(self):
        hellos = [
            {"mesh_id": "home-aaaa1111", "node_id": "n1", "advertise": "192.168.1.10:9874"},
            {"mesh_id": "home-aaaa1111", "node_id": "n2", "advertise": "192.168.1.11:9874"},
            {"mesh_id": "lab-bbbb2222", "node_id": "n3", "advertise": "192.168.1.12:9874"},
        ]
        grouped = mesh._group_meshes(hellos, current_mesh_id="home-aaaa1111")
        by_id = {m["mesh_id"]: m for m in grouped}
        self.assertTrue(by_id["home-aaaa1111"]["is_current"])
        self.assertEqual(by_id["home-aaaa1111"]["node_count"], 2)
        self.assertEqual(by_id["home-aaaa1111"]["name"], "home")
        self.assertEqual(by_id["home-aaaa1111"]["suffix"], "aaaa1111")
        self.assertFalse(by_id["lab-bbbb2222"]["is_current"])
        self.assertEqual(by_id["lab-bbbb2222"]["node_count"], 1)

    def test_probe_node_and_discover_find_live_mesh(self):
        db_a = self.tmp / "a.db"
        seed_db(db_a, "nodeA", ["a1"])
        port, listen_socket = self._reserve_port()
        self._spawn_mesh_node(db_a, "nodeA", "home-cccc3333", port, listen_socket=listen_socket)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/mesh/hello", timeout=0.5) as resp:
            anonymous = json.loads(resp.read().decode("utf-8"))
        self.assertFalse(anonymous["ok"])
        self.assertFalse(anonymous["mesh_id_match"])
        self.assertNotIn("mesh_id", anonymous)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/mesh/discover", timeout=0.5) as resp:
            discovered = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(discovered["ok"])
        self.assertEqual(discovered["mesh_id"], "home-cccc3333")

        hello = mesh.probe_node("127.0.0.1", port, mesh_id="home-cccc3333")
        self.assertIsNotNone(hello)
        self.assertEqual(hello["mesh_id"], "home-cccc3333")
        self.assertEqual(hello["advertise"], f"127.0.0.1:{port}")

        mesh.save_config(mesh.normalize_config({
            "enabled": True,
            "mesh_id": "home-cccc3333",
            "node_id": "local-node",
        }))

        # A /30 over loopback includes 127.0.0.1 as a scannable host.
        result = mesh.discover_meshes(port=port, subnets=["127.0.0.0/30"])
        ids = {m["mesh_id"] for m in result["meshes"]}
        self.assertIn("home-cccc3333", ids)

    def test_discover_meshes_finds_other_mesh_than_current(self):
        db_a = self.tmp / "a.db"
        seed_db(db_a, "nodeA", ["a1"])
        port, listen_socket = self._reserve_port()
        self._spawn_mesh_node(db_a, "nodeA", "lab-dddd4444", port, listen_socket=listen_socket)

        mesh.CONFIG_PATH = self.tmp / "mesh.json"
        mesh.save_config(mesh.normalize_config({
            "enabled": True,
            "mesh_id": "home-cccc3333",
            "node_id": "local-node",
        }))

        result = mesh.discover_meshes(port=port, subnets=["127.0.0.0/30"])
        by_id = {m["mesh_id"]: m for m in result["meshes"]}
        self.assertEqual(result["current_mesh_id"], "home-cccc3333")
        self.assertIn("lab-dddd4444", by_id)
        self.assertFalse(by_id["lab-dddd4444"]["is_current"])

    def test_discover_meshes_works_without_current_mesh(self):
        db_a = self.tmp / "a.db"
        seed_db(db_a, "nodeA", ["a1"])
        port, listen_socket = self._reserve_port()
        self._spawn_mesh_node(db_a, "nodeA", "lab-dddd4444", port, listen_socket=listen_socket)

        mesh.CONFIG_PATH = self.tmp / "mesh.json"
        if mesh.CONFIG_PATH.exists():
            mesh.CONFIG_PATH.unlink()
        result = mesh.discover_meshes(port=port, subnets=["127.0.0.0/30"])
        self.assertIsNone(result["current_mesh_id"])
        self.assertEqual({m["mesh_id"] for m in result["meshes"]}, {"lab-dddd4444"})

    def test_probe_node_rejects_non_mesh_port(self):
        port, _ = self._reserve_port()  # bound but not a mesh server
        self.assertIsNone(mesh.probe_node("127.0.0.1", port, timeout=0.3))


class TestPropagationTracking(MeshTestBase):
    def setUp(self):
        super().setUp()
        mesh._RUNTIME["prop_seconds"] = mesh.deque(maxlen=mesh.PROPAGATION_WINDOW)
        mesh._RUNTIME["prop_recorded"] = set()

    def test_records_only_events_present_on_all_active_peers(self):
        now = mesh.time.time()
        rows = [
            ("e1", "2026-06-26T10:00:00+00:00"),
            ("e2", "2026-06-26T10:00:00+00:00"),
        ]
        peer_has = {"p1": {"e1", "e2"}, "p2": {"e1"}}  # e2 missing on p2
        recorded = mesh._record_propagations(rows, peer_has, now)
        self.assertEqual(recorded, 1)  # only e1 fully propagated
        self.assertIsNotNone(mesh.mean_propagation_seconds())

    def test_events_are_not_double_counted(self):
        now = mesh.time.time()
        rows = [("e1", "2026-06-26T10:00:00+00:00")]
        peer_has = {"p1": {"e1"}}
        self.assertEqual(mesh._record_propagations(rows, peer_has, now), 1)
        self.assertEqual(mesh._record_propagations(rows, peer_has, now), 0)

    def test_rolling_window_keeps_last_15(self):
        now = mesh.time.time()
        rows = [(f"e{i}", "2026-06-26T10:00:00+00:00") for i in range(20)]
        peer_has = {"p1": {f"e{i}" for i in range(20)}}
        mesh._record_propagations(rows, peer_has, now)
        self.assertEqual(len(mesh._RUNTIME["prop_seconds"]), mesh.PROPAGATION_WINDOW)

    def test_no_active_peers_records_nothing(self):
        self.assertEqual(
            mesh._record_propagations([("e1", "2026-06-26T10:00:00+00:00")], {}, mesh.time.time()),
            0,
        )
        self.assertIsNone(mesh.mean_propagation_seconds())


class TestRuntimeControl(MeshTestBase):
    def setUp(self):
        super().setUp()
        # A base schema so backfill during create/join has a turns table.
        conn = sqlite3.connect(mesh.DB_PATH)
        conn.executescript(BASE_SCHEMA)
        conn.commit()
        conn.close()
        self.addCleanup(mesh.stop_mesh)

    def test_create_runtime_writes_config_and_status(self):
        result = mesh.create_mesh_runtime(name="home", restart=False)
        self.assertTrue(result["mesh_id"].startswith("home-"))
        status = mesh.runtime_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["mesh_name"], "home")
        self.assertEqual(status["peer_count"], 0)
        self.assertFalse(status["connected"])  # no peers yet

    def test_join_enforces_single_mesh_by_leaving_current(self):
        mesh.create_mesh_runtime(name="home", restart=False)
        mesh.join_mesh_runtime("lab-bbbb2222", peers=["192.168.1.9:9874"], restart=False)
        cfg = mesh.load_config()
        self.assertEqual(cfg["mesh_id"], "lab-bbbb2222")
        self.assertEqual(cfg["peers"], ["192.168.1.9:9874"])

    def test_leave_clears_membership_but_keeps_node_id(self):
        created = mesh.create_mesh_runtime(name="home", restart=False)
        node_id = created["node_id"]
        result = mesh.leave_mesh()
        self.assertTrue(result["left"].startswith("home-"))
        cfg = mesh.load_config()
        self.assertFalse(cfg["enabled"])
        self.assertIsNone(cfg["mesh_id"])
        self.assertEqual(cfg["peers"], [])
        self.assertEqual(cfg["node_id"], node_id)  # identity preserved
        self.assertFalse(mesh.runtime_status()["enabled"])

    def test_runtime_status_shape_when_disabled(self):
        status = mesh.runtime_status()
        for key in ("available", "enabled", "connected", "peer_count",
                    "syncing", "uptime_seconds", "mean_propagation_seconds",
                    "adjacent_meshes", "active_peers"):
            self.assertIn(key, status)
        self.assertTrue(status["available"])
        self.assertFalse(status["enabled"])

    def test_start_and_stop_mesh_tracks_uptime(self):
        mesh.create_mesh_runtime(name="home", restart=False)
        self.assertTrue(mesh.start_mesh(app_version="test"))
        self.assertIsNotNone(mesh.mesh_uptime_seconds())
        self.assertTrue(mesh.runtime_status()["running"])
        self.assertTrue(mesh.stop_mesh())
        self.assertIsNone(mesh.mesh_uptime_seconds())


if __name__ == "__main__":
    unittest.main(verbosity=2)
