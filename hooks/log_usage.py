#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "token_usage.db"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
REPORT_SERVER = Path.home() / ".claude" / "hooks" / "serve_report.py"
REPORT_PORT = 9873

MODEL_TIER_PRICING = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_create": 18.75},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
    "haiku":  {"input": 0.8,  "output": 4.0,  "cache_read": 0.08, "cache_create": 1.0},
}


def infer_model_attributes(model_key: str | None) -> dict:
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

    if "opus" in lower:
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


def ensure_model_row(conn: sqlite3.Connection, model_key: str | None) -> int | None:
    key = (model_key or "").strip()
    if not key:
        return None

    row = conn.execute("SELECT id FROM models WHERE model_key = ?", (key,)).fetchone()
    if row:
        return row[0]

    attrs = infer_model_attributes(key)
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
        {"model_key": key, **attrs},
    )
    return cur.lastrowid


def backfill_model_dimension(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, model FROM turns WHERE model_id IS NULL AND model IS NOT NULL AND TRIM(model) <> ''"
    ).fetchall()
    for turn_pk, model_key in rows:
        model_id = ensure_model_row(conn, model_key)
        if model_id is not None:
            conn.execute("UPDATE turns SET model_id = ? WHERE id = ?", (model_id, turn_pk))


def init_db(conn: sqlite3.Connection) -> None:
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
    backfill_model_dimension(conn)
    conn.commit()


def ensure_report_server() -> None:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", REPORT_PORT)) == 0:
            return
    if REPORT_SERVER.exists():
        subprocess.Popen(
            [sys.executable, str(REPORT_SERVER)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def parse_retention_days(value: object) -> int | None:
    if value is None:
        return None
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if days >= 0 else None


def get_retention_days() -> int | None:
    env_days = parse_retention_days(os.getenv("TOKEN_USAGE_RETENTION_DAYS"))
    if env_days is not None:
        return env_days
    try:
        if SETTINGS_PATH.exists():
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return parse_retention_days(settings.get("token_usage_retention_days"))
    except Exception:
        pass
    return None


def purge_old_records(conn: sqlite3.Connection, retention_days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM tool_calls WHERE recorded_at < ?", (cutoff,))
    conn.execute("DELETE FROM turns WHERE recorded_at < ?", (cutoff,))
    conn.commit()


def get_transcript_info(transcript_path: str) -> dict:
    """Extract model, usage, and stop_reason from the current turn.

    Sums usage across unique assistant API calls (by message ID) in the
    last turn (after the final user message).  The transcript contains
    multiple streaming snapshots per API response (same ``message.id``),
    so we deduplicate — only the *last* snapshot of each message is kept.
    """
    info: dict = {
        "model": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "stop_reason": None,
    }
    try:
        p = Path(transcript_path)
        if not p.exists():
            return info

        # Collect per-message-id usage for the current (last) turn.
        # On each user message we reset.  Within a turn, multiple API
        # calls have distinct message IDs; streaming duplicates share
        # the same ID and we keep the last occurrence.
        seen: dict[str, dict] = {}   # msg_id -> usage dict
        model = None
        stop_reason = None

        for line in p.read_text(encoding="utf-8").splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            # Fast pre-filter before JSON parsing
            if '"type":"user"' in line_s or '"type": "user"' in line_s:
                try:
                    obj = json.loads(line_s)
                    if isinstance(obj, dict) and obj.get("type") == "user":
                        seen.clear()
                        model = None
                        stop_reason = None
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass
            if '"model"' not in line_s:
                continue
            try:
                obj = json.loads(line_s)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = obj.get("message") or {}
            if not msg.get("model"):
                continue
            model = msg["model"]
            stop_reason = msg.get("stop_reason")
            u = msg.get("usage") or {}
            msg_id = msg.get("id") or id(line)  # fallback for missing id
            seen[msg_id] = {
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            }

        # Sum across unique API calls in this turn
        info["model"] = model
        info["stop_reason"] = stop_reason
        for u in seen.values():
            info["usage"]["input_tokens"] += u["input_tokens"]
            info["usage"]["output_tokens"] += u["output_tokens"]
            info["usage"]["cache_read_input_tokens"] += u["cache_read_input_tokens"]
            info["usage"]["cache_creation_input_tokens"] += u["cache_creation_input_tokens"]
    except Exception:
        pass
    return info


def get_git_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=3,
        )
        branch = result.stdout.strip()
        return branch or None
    except Exception:
        return None


def derive_turn_id(payload: dict) -> str:
    """Count user messages in the transcript to get a stable per-turn ID.

    Uses proper JSON parsing so that the string '"type":"user"' appearing
    inside tool output or assistant content is not mis-counted as a user
    message boundary.
    """
    transcript = payload.get("transcript_path", "")
    if transcript:
        try:
            p = Path(transcript)
            if p.exists():
                count = 0
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and obj.get("type") == "user":
                            count += 1
                    except (json.JSONDecodeError, ValueError):
                        pass
                return f"turn-{count}"
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def handle_stop(conn: sqlite3.Connection, payload: dict) -> None:
    session_id = payload.get("session_id", "unknown")
    turn_id = derive_turn_id(payload)
    now = datetime.now(timezone.utc).isoformat()
    cwd = payload.get("cwd")
    git_branch = get_git_branch(cwd) if cwd else None
    transcript = payload.get("transcript_path", "")
    t_info = get_transcript_info(transcript) if transcript else {"model": None, "usage": {}, "stop_reason": None}
    model = t_info["model"]
    model_id = ensure_model_row(conn, model)
    usage = t_info["usage"]
    stop_reason = t_info["stop_reason"] or payload.get("stop_reason")

    conn.execute("""
        INSERT INTO turns (
            session_id, turn_id, recorded_at, stop_reason,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens,
            cwd, git_branch, model_id
        ) VALUES (
            :session_id, :turn_id, :recorded_at, :stop_reason,
            :input_tokens, :output_tokens,
            :cache_read_tokens, :cache_creation_tokens,
            :cwd, :git_branch, :model_id
        )
        ON CONFLICT(session_id, turn_id) DO UPDATE SET
            stop_reason           = excluded.stop_reason,
            recorded_at           = excluded.recorded_at,
            input_tokens          = excluded.input_tokens,
            output_tokens         = excluded.output_tokens,
            cache_read_tokens     = excluded.cache_read_tokens,
            cache_creation_tokens = excluded.cache_creation_tokens,
            cwd                   = excluded.cwd,
            git_branch            = excluded.git_branch,
            model_id              = excluded.model_id
    """, {
        "session_id":            session_id,
        "turn_id":               turn_id,
        "recorded_at":           now,
        "stop_reason":           stop_reason,
        "input_tokens":          usage.get("input_tokens", 0),
        "output_tokens":         usage.get("output_tokens", 0),
        "cache_read_tokens":     usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cwd":                   cwd,
        "git_branch":            git_branch,
        "model_id":              model_id,
    })

    # Back-fill turn_pk on any tool_calls that arrived before Stop fired.
    # Tool_calls may have slightly higher turn numbers than the actual turn
    # (due to transcript growth between PostToolUse and Stop), so match any
    # tool_calls in this session whose turn number is >= this turn's number
    # and < the next turn's number (or unbounded if this is the latest turn).
    turn_num = int(turn_id.replace("turn-", "")) if turn_id.startswith("turn-") else None
    turn_pk = conn.execute(
        "SELECT id FROM turns WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id),
    ).fetchone()
    if turn_pk and turn_num is not None:
        turn_pk = turn_pk[0]
        # Find the next turn's number in this session (if any)
        next_row = conn.execute(
            """SELECT CAST(REPLACE(turn_id, 'turn-', '') AS INTEGER) as n
               FROM turns WHERE session_id = ? AND CAST(REPLACE(turn_id, 'turn-', '') AS INTEGER) > ?
               ORDER BY n ASC LIMIT 1""",
            (session_id, turn_num),
        ).fetchone()
        if next_row:
            conn.execute(
                """UPDATE tool_calls SET turn_pk = ?
                   WHERE session_id = ? AND turn_pk IS NULL
                   AND turn_id LIKE 'turn-%'
                   AND CAST(REPLACE(turn_id, 'turn-', '') AS INTEGER) >= ?
                   AND CAST(REPLACE(turn_id, 'turn-', '') AS INTEGER) < ?""",
                (turn_pk, session_id, turn_num, next_row[0]),
            )
        else:
            conn.execute(
                """UPDATE tool_calls SET turn_pk = ?
                   WHERE session_id = ? AND turn_pk IS NULL
                   AND turn_id LIKE 'turn-%'
                   AND CAST(REPLACE(turn_id, 'turn-', '') AS INTEGER) >= ?""",
                (turn_pk, session_id, turn_num),
            )

    conn.commit()


def handle_post_tool_use(conn: sqlite3.Connection, payload: dict) -> None:
    session_id = payload.get("session_id", "unknown")
    turn_id = derive_turn_id(payload)
    now = datetime.now(timezone.utc).isoformat()

    tool = payload.get("tool") or {}
    tool_name  = tool.get("name")  or payload.get("tool_name")
    tool_input = tool.get("input") or payload.get("tool_input")

    result    = payload.get("tool_result") or payload.get("result") or {}
    exit_code = result.get("exit_code")    or payload.get("exit_code")
    error     = result.get("stderr")       or payload.get("error")

    # Resolve turn_pk if the turns row already exists (it usually won't yet)
    cur = conn.execute(
        "SELECT id FROM turns WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id)
    )
    row = cur.fetchone()
    turn_pk = row[0] if row else None

    conn.execute("""
        INSERT INTO tool_calls (
            turn_pk, session_id, turn_id, recorded_at,
            tool_name, tool_input, exit_code, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        turn_pk,
        session_id,
        turn_id,
        now,
        tool_name,
        json.dumps(tool_input) if tool_input is not None else None,
        exit_code,
        str(error) if error else None,
    ))
    conn.commit()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("stop", "post-tool-use"):
        sys.exit(0)

    event = sys.argv[1]

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    try:
        with sqlite3.connect(DB_PATH) as conn:
            init_db(conn)
            retention_days = get_retention_days()
            if retention_days is not None:
                purge_old_records(conn, retention_days)
            if event == "stop":
                handle_stop(conn, payload)
            elif event == "post-tool-use":
                handle_post_tool_use(conn, payload)
        try:
            ensure_report_server()
        except Exception:
            pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
