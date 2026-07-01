#!/usr/bin/env python3
"""Minimal HTTP server that serves drachometer-dashboard.html, drachometer.db,
SSE live-refresh, and a loopback-only mesh control API for the dashboard.

The mesh control API (``/mesh/api/*``), ``/health``, and ``/shutdown`` are bound
to 127.0.0.1 only. They let the dashboard configure the LAN mesh (scan, create,
join, leave, live status) and let the installer detect and gracefully stop a
prior running instance before it overwrites files.
"""

import json
import os
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

PORT = 9873
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = Path.home() / ".claude" / "drachometer.db"
PID_PATH = Path.home() / ".claude" / "drachometer-dashboard.pid"

# Optional mesh replication; absence leaves the loopback dashboard server unchanged.
sys.path.insert(0, str(SCRIPT_DIR))
try:
    import drachometer_mesh as mesh
except Exception:
    mesh = None


def _app_version() -> str:
    try:
        data = json.loads((SCRIPT_DIR / "drachometer-version.json").read_text(encoding="utf-8"))
        return str(data.get("version", ""))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


# Tracks the last-known mtime of the DB file; SSE clients poll this.
_db_mtime = 0.0
_db_mtime_lock = threading.Lock()


def _watch_db():
    """Background thread: update _db_mtime when the DB file changes."""
    global _db_mtime
    while True:
        try:
            mt = DB_PATH.stat().st_mtime if DB_PATH.exists() else 0.0
            with _db_mtime_lock:
                _db_mtime = mt
        except OSError:
            pass
        time.sleep(1)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    # -- helpers ------------------------------------------------------------ #
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, ValueError):
            return {}

    # -- routing ------------------------------------------------------------ #
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/drachometer.db":
            if DB_PATH.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                data = DB_PATH.read_bytes()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404, "Database not found")
            return

        if path == "/events":
            self._handle_sse()
            return

        if path == "/health":
            self._send_json({"app": "drachometer", "version": _app_version(),
                             "pid": os.getpid(), "mesh": mesh is not None})
            return

        if path == "/mesh/api/status":
            self._send_json(mesh.runtime_status() if mesh else {"available": False})
            return

        if path == "/mesh/api/scan":
            if not mesh:
                self._send_json({"available": False}, status=200)
                return
            try:
                self._send_json(mesh.discover_meshes())
            except Exception as exc:  # scanning is best-effort
                self._send_json({"available": True, "error": str(exc), "meshes": []})
            return

        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/shutdown":
            self._send_json({"ok": True, "stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if path.startswith("/mesh/api/"):
            if not mesh:
                self._send_json({"available": False, "error": "mesh module unavailable"}, status=503)
                return
            body = self._read_json_body()
            try:
                if path == "/mesh/api/create":
                    result = mesh.create_mesh_runtime(
                        name=body.get("name"),
                        port=int(body.get("port") or mesh.DEFAULT_PORT),
                        advertise=body.get("advertise"),
                        peers=body.get("peers") or [],
                    )
                    self._send_json({"ok": True, **result})
                    return
                if path == "/mesh/api/join":
                    mesh_id = body.get("mesh_id")
                    if not mesh_id and body.get("name") and body.get("suffix"):
                        mesh_id = f"{body['name']}-{body['suffix']}"
                    if not mesh_id:
                        self._send_json({"ok": False, "error": "mesh_id (or name + suffix) required"}, status=400)
                        return
                    result = mesh.join_mesh_runtime(
                        mesh_id=mesh_id,
                        port=int(body.get("port") or mesh.DEFAULT_PORT),
                        advertise=body.get("advertise"),
                        peers=body.get("peers") or [],
                    )
                    self._send_json({"ok": True, **result})
                    return
                if path == "/mesh/api/leave":
                    self._send_json({"ok": True, **mesh.leave_mesh()})
                    return
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, status=404)
            return

        self.send_error(404, "Not found")

    def _handle_sse(self):
        """Server-Sent Events endpoint. Sends a 'refresh' event when DB mtime changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_sent = 0.0
        try:
            while True:
                with _db_mtime_lock:
                    current = _db_mtime
                if current > last_sent:
                    last_sent = current
                    self.wfile.write(f"data: {current}\n\n".encode())
                    self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, format, *args):
        pass


def _write_pid_file():
    try:
        PID_PATH.write_text(json.dumps({"pid": os.getpid(), "version": _app_version(),
                                        "port": PORT}) + "\n", encoding="utf-8")
    except OSError:
        pass


def _remove_pid_file():
    try:
        PID_PATH.unlink()
    except OSError:
        pass


def main():
    watcher = threading.Thread(target=_watch_db, daemon=True)
    watcher.start()

    # Start mesh replication if the user has enabled it. The mesh listener binds
    # its own LAN-facing port; this dashboard server stays loopback-only.
    if mesh is not None:
        try:
            if mesh.start_mesh(_app_version()):
                print("Mesh replication active.")
        except Exception:
            pass

    class ThreadingServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    try:
        server = ThreadingServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(0)
    _write_pid_file()
    print(f"Serving dashboard at http://localhost:{PORT}/drachometer-dashboard.html")
    try:
        server.serve_forever()
    finally:
        _remove_pid_file()


if __name__ == "__main__":
    main()
