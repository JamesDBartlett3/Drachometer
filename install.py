#!/usr/bin/env python3
"""Installer for claude-token-logger.

Copies hook scripts into ~/.claude/hooks/claude-code-token-usage-dashboard/,
merges hook configuration into
~/.claude/settings.json, initializes the SQLite database, and runs a
smoke test.

Usage:
    python install.py
"""

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
HOOKS_ROOT_DIR = CLAUDE_DIR / "hooks"
APP_HOOKS_SUBDIR = "claude-code-token-usage-dashboard"
HOOKS_DIR = HOOKS_ROOT_DIR / APP_HOOKS_SUBDIR
SETTINGS_PATH = CLAUDE_DIR / "settings.json"
DB_PATH = CLAUDE_DIR / "token_usage.db"
VERSION_PATH = HOOKS_DIR / "version.json"
LEGACY_VERSION_PATH = HOOKS_ROOT_DIR / "version.json"

REPO_HOOKS = Path(__file__).resolve().parent / "hooks"
REPO_REPORT = Path(__file__).resolve().parent / "report.html"
REPO_SERVER = Path(__file__).resolve().parent / "serve_report.py"

REPO_README = Path(__file__).resolve().parent / "README.md"
REPO_COIN = Path(__file__).resolve().parent / "coin.svg"
REPO_VERSION = Path(__file__).resolve().parent / "version.json"
REPO_PRICING = Path(__file__).resolve().parent / "pricing.json"

APP_METADATA = json.loads(REPO_VERSION.read_text(encoding="utf-8"))
APP_VERSION = str(APP_METADATA.get("version", "0.0.0"))

HOOK_FILES = {
    "log_usage.py": REPO_HOOKS / "log_usage.py",
    "serve_report.py": REPO_SERVER,
    "report.html": REPO_REPORT,
    "README.md": REPO_README,
    "coin.svg": REPO_COIN,
    "version.json": REPO_VERSION,
    "pricing.json": REPO_PRICING,
}

# Offline fallback pricing (USD per 1M tokens). Overlaid below from pricing.json
# so model rows the installer creates/backfills use the latest published rates.
MODEL_TIER_PRICING = {
    "opus":   {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_create": 6.25},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
    "haiku":  {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_create": 1.25},
}


def _load_pricing_overrides() -> None:
    try:
        data = json.loads(REPO_PRICING.read_text(encoding="utf-8"))
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


def migrate_legacy_db_paths() -> bool:
    if DB_PATH.exists():
        return False
    candidates = [
        CLAUDE_DIR / "claude_token_usage.db",
        CLAUDE_DIR / "claude-code-token-usage.db",
        HOOKS_ROOT_DIR / "token_usage.db",
    ]
    for src in candidates:
        if src.exists():
            src.replace(DB_PATH)
            print(f"  Migrated database path: {src} -> {DB_PATH}")
            return True
    return False


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
                if "serve_report.py" in command:
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
    migrate_legacy_db_paths()
    migrate_settings_for_server_changes()


def infer_model_attributes(model_key: str | None) -> dict:
    key = (model_key or "").strip()
    lower = key.lower()

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
    model_name = " ".join(parts[:2]).title() if len(parts) >= 2 and parts[0].lower() == "claude" else (parts[0].title() if parts else None)
    version_match = re.search(r"(\d+(?:[-.]\d+)*(?:-\d{8})?)", key)
    model_version = version_match.group(1) if version_match else None
    provider = "Anthropic" if lower.startswith("claude-") or lower.startswith("claude") else None

    pricing = MODEL_TIER_PRICING.get(tier, {})
    return {
        "model_name": model_name,
        "model_version": model_version,
        "model_provider": provider,
        "input_price_per_mtok": pricing.get("input"),
        "output_price_per_mtok": pricing.get("output"),
        "cache_read_price_per_mtok": pricing.get("cache_read"),
        "cache_creation_price_per_mtok": pricing.get("cache_create"),
    }


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
    row = conn.execute("SELECT id FROM models WHERE model_key = ?", (model_key,)).fetchone()
    if row:
        return row[0]

    attrs = infer_model_attributes(model_key)
    if prompt_if_missing:
        attrs = prompt_missing_model_attributes(model_key, attrs)

    cur = conn.execute(
        """
        INSERT INTO models (
            model_key, model_name, model_version, model_provider,
            input_price_per_mtok, output_price_per_mtok,
            cache_read_price_per_mtok, cache_creation_price_per_mtok
        ) VALUES (
            :model_key, :model_name, :model_version, :model_provider,
            :input_price_per_mtok, :output_price_per_mtok,
            :cache_read_price_per_mtok, :cache_creation_price_per_mtok
        )
        """,
        {"model_key": model_key, **attrs},
    )
    return cur.lastrowid


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


def build_hook_commands(python_exe: str) -> dict:
    hook_script = str(HOOKS_DIR / "log_usage.py")
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
            "log_usage.py" in h.get("command", "")
            for group in existing
            for h in group.get("hooks", [])
        )
        if already:
            # Update existing entry in place
            for group in existing:
                for h in group.get("hooks", []):
                    if "log_usage.py" in h.get("command", ""):
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
        conn.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_calls_turn_pk ON tool_calls(turn_pk);
            CREATE INDEX IF NOT EXISTS idx_calls_session ON tool_calls(session_id, turn_id);
        """)
        for col, typedef in [("cwd", "TEXT"), ("git_branch", "TEXT"), ("model", "TEXT"), ("model_id", "INTEGER REFERENCES models(id)")]:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_model_id ON turns(model_id)")
        backfill_model_dimension(conn, prompt_if_missing=True)

        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if migrations_dir.is_dir():
            for sql_file in sorted(migrations_dir.glob("*.sql")):
                conn.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", (sql_file.name,))

        conn.commit()
    print(f"  Database ready at {DB_PATH}")


def smoke_test(python_exe: str) -> bool:
    hook_script = str(HOOKS_DIR / "log_usage.py")
    payload = json.dumps({
        "session_id": "__install_test__",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    try:
        result = subprocess.run(
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


def main() -> None:
    print("claude-token-logger installer")
    print("=" * 40)

    print("\n[1/6] Finding Python...")
    python_exe = find_python()
    print(f"  Using: {python_exe}")

    print("\n[2/6] Detecting installed version and running migrations...")
    installed_version = detect_installed_version()
    run_install_migrations(installed_version)

    print("\n[3/6] Copying hook files...")
    copy_hooks(python_exe)

    print("\n[4/6] Updating settings.json...")
    merge_settings(python_exe)

    print("\n[5/6] Initializing database...")
    apply_sql_migrations()
    init_database()

    print("\n[6/6] Running smoke test...")
    if smoke_test(python_exe):
        print("  PASS")
    else:
        print("  FAIL - hook did not write to the database.")
        print("  Check that the hook script runs without errors:")
        print(f"    {python_exe} {HOOKS_DIR / 'log_usage.py'} stop")
        sys.exit(1)

    print("\n" + "=" * 40)
    print("Installation complete!\n")
    print("Token usage will be logged automatically every time you")
    print("use Claude Code. The report server starts on first use.\n")
    print("To view the report, open:")
    print("  http://localhost:9873/report.html\n")
    print(f"Database: {DB_PATH}")
    print(f"Hooks:    {HOOKS_DIR}")
    print(f"Version:  {APP_VERSION}")


if __name__ == "__main__":
    main()
