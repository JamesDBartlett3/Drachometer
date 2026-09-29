#!/usr/bin/env python3
"""Installer for drachometer.

Copies hook scripts into ~/.claude/hooks/drachometer/,
merges hook configuration into
~/.claude/settings.json, initializes the SQLite database, and runs a
smoke test.

Usage:
    python drachometer-install.py
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Shared pricing/model-inference/schema helpers (next to this script in the
# repo and in every installed layout).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drachometer_common as common  # noqa: E402

CLAUDE_DIR = Path.home() / ".claude"
HOOKS_ROOT_DIR = CLAUDE_DIR / "hooks"
APP_HOOKS_SUBDIR = "drachometer"
HOOKS_DIR = HOOKS_ROOT_DIR / APP_HOOKS_SUBDIR
SETTINGS_PATH = CLAUDE_DIR / "settings.json"
DB_PATH = CLAUDE_DIR / "drachometer.db"
VERSION_PATH = HOOKS_DIR / "drachometer-version.json"
LEGACY_VERSION_PATH = HOOKS_ROOT_DIR / "drachometer-version.json"
DASHBOARD_PORT = 9873
PID_PATH = CLAUDE_DIR / "drachometer-dashboard.pid"

REPO_HOOKS = Path(__file__).resolve().parent / "hooks"
REPO_DASHBOARD = Path(__file__).resolve().parent / "drachometer-dashboard.html"
REPO_SERVER = Path(__file__).resolve().parent / "drachometer-serve-dashboard.py"
REPO_MESH = Path(__file__).resolve().parent / "drachometer_mesh.py"
REPO_COMMON = Path(__file__).resolve().parent / "drachometer_common.py"

REPO_README = Path(__file__).resolve().parent / "README.md"
REPO_LOGO = Path(__file__).resolve().parent / "drachometer-logo.svg"
REPO_VERSION = Path(__file__).resolve().parent / "drachometer-version.json"
REPO_PRICING = Path(__file__).resolve().parent / "drachometer-pricing.json"

APP_METADATA = json.loads(REPO_VERSION.read_text(encoding="utf-8"))
APP_VERSION = str(APP_METADATA.get("version", "0.0.0"))

HOOK_FILES = {
    "drachometer-log-usage.py": REPO_HOOKS / "drachometer-log-usage.py",
    "drachometer-serve-dashboard.py": REPO_SERVER,
    "drachometer_mesh.py": REPO_MESH,
    "drachometer_common.py": REPO_COMMON,
    "drachometer-dashboard.html": REPO_DASHBOARD,
    "README.md": REPO_README,
    "drachometer-logo.svg": REPO_LOGO,
    "drachometer-version.json": REPO_VERSION,
    "drachometer-pricing.json": REPO_PRICING,
}


def semver_key(version: str | None) -> tuple[int, int, int]:
    text = str(version or "").strip().lstrip("v")
    parts = text.split(".")
    nums: list[int] = []
    for idx in range(3):
        if idx >= len(parts):
            nums.append(0)
            continue
        match = re.match(r"^(\d+)", parts[idx])
        nums.append(int(match.group(1)) if match else 0)
    return (nums[0], nums[1], nums[2])


def detect_installed_version() -> str:
    for version_path in (VERSION_PATH, LEGACY_VERSION_PATH):
        if version_path.exists():
            try:
                data = json.loads(version_path.read_text(encoding="utf-8"))
                return str(data.get("version", "0.0.0"))
            except (OSError, json.JSONDecodeError, ValueError):
                pass
    return "0.0.0"


def write_installed_version(version: str | None = None) -> None:
    """Persist the current app version into both the modern and legacy install paths."""
    next_version = str(version or APP_VERSION or "0.0.0").strip() or "0.0.0"
    for version_path in (VERSION_PATH, LEGACY_VERSION_PATH):
        try:
            version_path.parent.mkdir(parents=True, exist_ok=True)
            # Preserve existing metadata fields (releases_api, releases_url, etc.)
            # so downstream consumers (e.g. checkForUpdates) continue to work.
            try:
                payload = json.loads(version_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = dict(APP_METADATA)
            except (OSError, json.JSONDecodeError, ValueError):
                payload = dict(APP_METADATA)
            payload["version"] = next_version
            version_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Running-instance detection
#
# A previously-installed dashboard server may still be running from an older
# version. Overwriting the hook files while it runs serves stale code and, on
# Windows, can fail because the files are locked. Detect it and stop it (or ask
# the user to) before copying anything.
# --------------------------------------------------------------------------- #
def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def query_running_health(timeout: float = 1.5) -> dict | None:
    """Ask a running server for its /health payload (version + pid)."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{DASHBOARD_PORT}/health", timeout=timeout
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def read_pid_file() -> dict | None:
    try:
        return json.loads(PID_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def request_server_shutdown(timeout: float = 3.0) -> bool:
    """Ask a running server to shut down gracefully via its /shutdown endpoint."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{DASHBOARD_PORT}/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def terminate_pid(pid: int) -> bool:
    """Terminate a process by PID, cross-platform. Returns True if a stop was attempted."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True,
            )
            return result.returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def wait_port_closed(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_port_open(host, port, 0.3):
            return True
        time.sleep(0.3)
    return not is_port_open(host, port, 0.3)


def stop_running_instance(force: bool) -> bool:
    """Detect and stop a running dashboard server before overwriting files.

    Returns True if it is safe to continue installing (nothing running, or the
    running instance was stopped). Returns False if a running instance could not
    be stopped and the user did not authorize continuing anyway.
    """
    if not is_port_open("127.0.0.1", DASHBOARD_PORT):
        return True

    health = query_running_health()
    pid_info = read_pid_file()
    version = (health or {}).get("version") or (pid_info or {}).get("version") or "unknown"
    pid = (health or {}).get("pid") or (pid_info or {}).get("pid")
    print(f"  Detected a running Drachometer instance (version {version}"
          f"{f', pid {pid}' if pid else ''}) on port {DASHBOARD_PORT}.")

    # 1) Graceful shutdown (supported by current and newer servers).
    if request_server_shutdown():
        if wait_port_closed("127.0.0.1", DASHBOARD_PORT, 6.0):
            print("  Stopped the running instance gracefully.")
            return True

    # 2) Terminate by PID (works for older servers that write a pid file).
    if pid and terminate_pid(int(pid)):
        if wait_port_closed("127.0.0.1", DASHBOARD_PORT, 6.0):
            print(f"  Stopped the running instance (pid {pid}).")
            return True

    # 3) Could not stop it automatically.
    if force:
        print("  WARNING: could not stop the running instance; continuing anyway "
              "(--force). Files in use may fail to update.")
        return True

    print("  ERROR: a previous Drachometer instance is still running and could not "
          "be stopped automatically.")
    print("  Close it (stop the process listening on port "
          f"{DASHBOARD_PORT}) and re-run the installer, or pass --force to override.")
    if sys.stdin.isatty():
        answer = input("  Continue anyway? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
    return False


def check_running_instance(args) -> None:
    """Abort the install if a prior version is running and cannot be stopped."""
    if getattr(args, "skip_running_check", False):
        return
    if not stop_running_instance(force=getattr(args, "force", False)):
        sys.exit(1)


def migrate_settings_for_server_changes() -> bool:
    if not SETTINGS_PATH.exists():
        return False
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False

    changed = False
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            hook_defs = group.get("hooks", []) if isinstance(group, dict) else []
            if not isinstance(hook_defs, list):
                continue
            filtered = []
            for hook in hook_defs:
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if "drachometer-serve-dashboard.py" in command:
                    changed = True
                    continue
                filtered.append(hook)
            if filtered:
                if len(filtered) != len(hook_defs):
                    changed = True
                if isinstance(group, dict):
                    group = dict(group)
                    group["hooks"] = filtered
                new_groups.append(group)
            else:
                changed = True
        hooks[event] = new_groups

    if changed:
        SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("  Migrated hook settings for HTTP server behavior")
    return changed


def apply_sql_migrations() -> None:
    if not DB_PATH.exists():
        return
    migrations_dir = Path(__file__).resolve().parent / "migrations"
    if not migrations_dir.is_dir():
        return

    with sqlite3.connect(DB_PATH) as conn:
        # A migration .sql assumes the legacy `turns` table already exists. On a
        # fresh/empty database there is nothing to migrate -- init_database()
        # creates the current schema and records migrations as applied -- so
        # skip here rather than running SQL that would fail against a missing table.
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'"
        ).fetchone():
            return

        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")

        # Check if 001 was already applied implicitly
        cursor = conn.execute("PRAGMA table_info(turns)")
        columns = [row[1] for row in cursor.fetchall()]
        if "model_id" in columns:
            conn.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES ('001_migrate_to_model_dimension.sql')")
            conn.commit()

        for sql_file in sorted(migrations_dir.glob("*.sql")):
            version = sql_file.name
            row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone()
            if not row:
                print(f"  Applying SQL migration: {version}")
                try:
                    conn.executescript(sql_file.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                    conn.commit()
                except Exception as e:
                    # Don't abort the whole install: init_database() runs next and
                    # brings the schema to the current version via its own idempotent
                    # logic. Leave this migration unrecorded so it retries later.
                    print(f"  WARNING: could not apply SQL migration {version}: {e}")
                    print("  Continuing; database initialization will ensure the current schema.")
                    break


def run_install_migrations(installed_version: str) -> None:
    if semver_key(installed_version) >= semver_key(APP_VERSION):
        return
    print(f"  Found installed version {installed_version}; migrating to {APP_VERSION}")
    migrate_settings_for_server_changes()


def prompt_missing_model_attributes(model_key: str, attrs: dict) -> dict:
    if not sys.stdin.isatty():
        return attrs

    print(f"\nModel metadata needed for: {model_key}")
    labels = {
        "model_name": "Model name",
        "model_version": "Model version",
        "model_provider": "Model provider",
        "input_price_per_mtok": "Input token price per 1M",
        "output_price_per_mtok": "Output token price per 1M",
        "cache_read_price_per_mtok": "Cache-read token price per 1M",
        "cache_creation_price_per_mtok": "Cache-creation token price per 1M",
    }
    numeric_keys = {
        "input_price_per_mtok",
        "output_price_per_mtok",
        "cache_read_price_per_mtok",
        "cache_creation_price_per_mtok",
    }
    for key, label in labels.items():
        if attrs.get(key) is not None:
            continue
        value = input(f"  {label}: ").strip()
        if not value:
            continue
        if key in numeric_keys:
            try:
                attrs[key] = float(value)
            except ValueError:
                pass
        else:
            attrs[key] = value
    return attrs


def ensure_model_row(conn: sqlite3.Connection, model_key: str, prompt_if_missing: bool) -> int:
    key = (model_key or "").strip()
    if not key:
        return None
    row = conn.execute("SELECT id FROM models WHERE model_key = ?", (key,)).fetchone()
    if row:
        return row[0]

    attrs = common.infer_model_attributes(key)
    if prompt_if_missing:
        attrs = prompt_missing_model_attributes(key, attrs)

    return common.insert_model_row(conn, key, attrs)


def backfill_model_dimension(conn: sqlite3.Connection, prompt_if_missing: bool) -> None:
    rows = conn.execute(
        "SELECT id, model FROM turns WHERE model_id IS NULL AND model IS NOT NULL AND TRIM(model) <> ''"
    ).fetchall()
    for turn_pk, model_key in rows:
        model_id = ensure_model_row(conn, model_key, prompt_if_missing=prompt_if_missing)
        conn.execute("UPDATE turns SET model_id = ? WHERE id = ?", (model_id, turn_pk))


def find_python() -> str:
    exe = sys.executable
    try:
        result = subprocess.run(
            [exe, "-c", "import sqlite3, json; print('ok')"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() == "ok":
            return exe
    except Exception:
        pass
    for candidate in ("python3", "python", "py"):
        try:
            result = subprocess.run(
                [candidate, "-c", "import sqlite3, json; print('ok')"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "ok":
                return candidate
        except FileNotFoundError:
            continue
    print("ERROR: Could not find a working Python interpreter.")
    sys.exit(1)


def copy_hooks(python_exe: str) -> None:
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for name, src in HOOK_FILES.items():
        dst = HOOKS_DIR / name
        shutil.copy2(src, dst)
        print(f"  Copied {name} -> {dst}")
    write_installed_version(APP_VERSION)


def build_hook_commands(python_exe: str) -> dict:
    hook_script = str(HOOKS_DIR / "drachometer-log-usage.py")
    # Use forward slashes for cross-platform JSON compatibility
    python_json = python_exe.replace("\\", "/")
    script_json = hook_script.replace("\\", "/")
    return {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_json} {script_json} stop",
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_json} {script_json} post-tool-use",
                    }
                ],
            }
        ],
    }


def merge_settings(python_exe: str) -> None:
    if SETTINGS_PATH.exists():
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    new_hooks = build_hook_commands(python_exe)

    for event, entries in new_hooks.items():
        existing = hooks.get(event, [])
        already = any(
            "drachometer-log-usage.py" in h.get("command", "")
            for group in existing
            for h in group.get("hooks", [])
        )
        if already:
            # Update existing entry in place
            for group in existing:
                for h in group.get("hooks", []):
                    if "drachometer-log-usage.py" in h.get("command", ""):
                        h["command"] = entries[0]["hooks"][0]["command"]
            print(f"  Updated existing {event} hook")
        else:
            existing.extend(entries)
            hooks[event] = existing
            print(f"  Added {event} hook")

    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def init_database() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # Full schema (usage tables + mesh oplog + legacy column backfills)
        # lives in drachometer_common so the hook's lazy init and this
        # installer path can never diverge. Model backfilling happens below so
        # unknown models can be prompted for interactively.
        common.ensure_base_schema(conn, backfill_model_ids=False)
        backfill_model_dimension(conn, prompt_if_missing=True)

        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if migrations_dir.is_dir():
            for sql_file in sorted(migrations_dir.glob("*.sql")):
                conn.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", (sql_file.name,))

        conn.commit()
    print(f"  Database ready at {DB_PATH}")


def smoke_test(python_exe: str) -> bool:
    hook_script = str(HOOKS_DIR / "drachometer-log-usage.py")
    payload = json.dumps({
        "session_id": "__install_test__",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    try:
        subprocess.run(
            [python_exe, hook_script, "stop"],
            input=payload, capture_output=True, text=True, timeout=10,
        )
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT 1 FROM turns WHERE session_id = '__install_test__'"
            ).fetchone()
            conn.execute("DELETE FROM turns WHERE session_id = '__install_test__'")
            conn.commit()
        return row is not None
    except Exception as e:
        print(f"  Smoke test error: {e}")
        return False


def _import_mesh():
    """Import the freshly-copied mesh library from the install directory."""
    sys.path.insert(0, str(HOOKS_DIR))
    import drachometer_mesh as mesh  # noqa: E402  (path set up just above)
    return mesh


def configure_mesh(args) -> None:
    """Optionally set up mesh replication from explicit CLI flags only.

    Mesh is now configured from the dashboard (hamburger menu -> "Configure Local
    Mesh Network"), so the installer no longer prompts interactively. Explicit
    flags (``--enable-mesh`` / ``--join-mesh``) remain supported for automation.
    With no mesh flags this is a no-op and single-node users are unaffected.
    """
    if not (args.enable_mesh or args.join_mesh):
        print("  Mesh is configured from the dashboard: open the hamburger menu (top-left)")
        print("  and choose \"Configure Local Mesh Network\" to scan, create, or join a mesh.")
        return
    try:
        mesh = _import_mesh()
    except Exception as exc:
        print(f"  Mesh module unavailable ({exc}); skipping mesh setup.")
        return

    peers = args.peer or []
    if args.join_mesh:
        cfg, emitted = mesh.join_mesh(args.join_mesh, args.mesh_port, args.advertise, peers)
        print(f"  Joined mesh {cfg['mesh_id']} as node {cfg['node_id']}")
        print(f"  Listening on {cfg['listen_host']}:{cfg['listen_port']}; "
              f"backfilled {emitted} local event(s).")
        return

    cfg, emitted = mesh.enable_new_mesh(args.mesh_name, args.mesh_port, args.advertise, peers)
    print(f"  Mesh enabled: {cfg['mesh_id']} (node {cfg['node_id']})")
    print(f"  Listening on {cfg['listen_host']}:{cfg['listen_port']}; "
          f"advertising {cfg['advertise_host']}:{cfg['advertise_port']}; "
          f"backfilled {emitted} event(s).")
    print("  Other nodes can now join from their dashboard, or with:")
    print(f"    python {HOOKS_DIR / 'drachometer_mesh.py'} join {cfg['mesh_id']} "
          f"--peer {cfg['advertise_host']}:{cfg['advertise_port']}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Installer for drachometer.")
    parser.add_argument("--enable-mesh", action="store_true",
                        help="Create a new mesh on this node during install.")
    parser.add_argument("--mesh-name", help="Human label for a new mesh (e.g. 'home').")
    parser.add_argument("--join-mesh", metavar="MESH_ID",
                        help="Join an existing mesh by id during install.")
    parser.add_argument("--peer", action="append", metavar="HOST:PORT",
                        help="Seed peer for mesh (repeatable).")
    parser.add_argument("--mesh-port", type=int, default=9874,
                        help="Mesh replication port (default: 9874).")
    parser.add_argument("--advertise", help="Advertise host/IP peers use to reach this node.")
    parser.add_argument("--no-mesh", action="store_true",
                        help="Skip the interactive mesh opt-in prompt.")
    parser.add_argument("--force", action="store_true",
                        help="Continue installing even if a running instance cannot be stopped.")
    parser.add_argument("--skip-running-check", action="store_true",
                        help="Do not check for a running instance before installing.")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)

    print("drachometer installer")
    print("=" * 40)

    print("\n[1/8] Checking for a running instance...")
    check_running_instance(args)

    print("\n[2/8] Finding Python...")
    python_exe = find_python()
    print(f"  Using: {python_exe}")

    print("\n[3/8] Detecting installed version and running migrations...")
    installed_version = detect_installed_version()
    run_install_migrations(installed_version)

    print("\n[4/8] Copying hook files...")
    copy_hooks(python_exe)

    print("\n[5/8] Updating settings.json...")
    merge_settings(python_exe)

    print("\n[6/8] Initializing database...")
    apply_sql_migrations()
    init_database()

    print("\n[7/8] Running smoke test...")
    if smoke_test(python_exe):
        print("  PASS")
    else:
        print("  FAIL - hook did not write to the database.")
        print("  Check that the hook script runs without errors:")
        print(f"    {python_exe} {HOOKS_DIR / 'drachometer-log-usage.py'} stop")
        sys.exit(1)

    print("\n[8/8] Configuring mesh replication...")
    configure_mesh(args)

    print("\n" + "=" * 40)
    print("Installation complete!\n")
    print("Token usage will be logged automatically every time you")
    print("use Claude Code. The dashboard server starts on first use.\n")
    print("To view the dashboard, open:")
    print("  http://localhost:9873/drachometer-dashboard.html\n")
    print(f"Database: {DB_PATH}")
    print(f"Hooks:    {HOOKS_DIR}")
    print(f"Version:  {APP_VERSION}")


if __name__ == "__main__":
    main()
