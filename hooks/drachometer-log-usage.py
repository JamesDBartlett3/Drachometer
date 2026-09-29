#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Both helpers are importable from the same directory the installer copies all
# files into. drachometer_common (pricing, model inference, base schema) is
# required; the mesh module is optional -- its absence (or any import error)
# leaves logging fully functional as a single-node tracker.
_HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOK_DIR))
# In a repo checkout the helper modules sit one level up (hooks/ vs. root);
# installed layouts have everything in the same directory, so this is a no-op.
sys.path.insert(1, str(_HOOK_DIR.parent))
try:
    import drachometer_common as common
except Exception as exc:  # never block Claude Code, but make the cause findable
    print(f"drachometer hook: drachometer_common unavailable: {exc}", file=sys.stderr)
    raise SystemExit(0)

try:
    import drachometer_mesh as mesh
except Exception:
    mesh = None

DB_PATH = Path.home() / ".claude" / "drachometer.db"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
LOG_PATH = Path.home() / ".claude" / "drachometer-hook.log"
LEGACY_DASHBOARD_SERVER = Path.home() / ".claude" / "hooks" / "drachometer-serve-report.py"
DASHBOARD_SERVER = Path.home() / ".claude" / "hooks" / "drachometer" / "drachometer-serve-dashboard.py"
DASHBOARD_PORT = 9873


def hook_log(message: str, level: str = "error", **fields) -> None:
    """Append a JSON log line to ~/.claude/drachometer-hook.log.

    The hook used to swallow every exception silently, which made a broken
    install undiagnosable. Errors are rare and the file stays tiny; write
    failures are still swallowed because there is nowhere else to report them.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    entry.update(fields)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ensure_dashboard_server() -> None:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", DASHBOARD_PORT)) == 0:
            return
    server_path = DASHBOARD_SERVER if DASHBOARD_SERVER.exists() else LEGACY_DASHBOARD_SERVER
    if server_path.exists():
        subprocess.Popen(
            [sys.executable, str(server_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def parse_retention_days(value: object) -> int | None:
    """Parse a retention window in days; None disables purging entirely.

    0 is treated as disabled, never as a 0-day window: a 0-day cutoff is
    "everything recorded before now", which would wipe the whole history on
    the next hook run. The dashboard's retention field documents 0 as "keep
    everything", and the mesh's compact_oplog treats retention_days <= 0 as
    disabled -- this keeps all three consistent.
    """
    if value is None:
        return None
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


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
    if retention_days <= 0:
        # Guard, not just parse: a 0-day cutoff is "everything recorded
        # before now", i.e. delete all records. 0 means keep everything.
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM tool_calls WHERE recorded_at < ?", (cutoff,))
    conn.execute("DELETE FROM turns WHERE recorded_at < ?", (cutoff,))
    conn.commit()


def get_transcript_info(transcript_path: str) -> dict:
    """Extract model, usage, stop_reason, and the user-message count in one pass.

    Sums usage across unique assistant API calls (by message ID) in the
    last turn (after the final user message).  The transcript contains
    multiple streaming snapshots per API response (same ``message.id``),
    so we deduplicate — only the *last* snapshot of each message is kept.

    The user-message count is what ``turn-N`` turn ids are derived from;
    reading and parsing the transcript once for both jobs halves the cost
    of every Stop hook on long sessions.
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
        "turn_count": 0,
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
        turn_count = 0

        for line in p.read_text(encoding="utf-8").splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            # One prefilter for both jobs before JSON parsing.
            if '"type"' not in line_s and '"model"' not in line_s:
                continue
            try:
                obj = json.loads(line_s)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "user":
                turn_count += 1
                seen.clear()
                model = None
                stop_reason = None
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
        info["turn_count"] = turn_count
        for u in seen.values():
            info["usage"]["input_tokens"] += u["input_tokens"]
            info["usage"]["output_tokens"] += u["output_tokens"]
            info["usage"]["cache_read_input_tokens"] += u["cache_read_input_tokens"]
            info["usage"]["cache_creation_input_tokens"] += u["cache_creation_input_tokens"]
    except Exception as exc:
        hook_log("transcript parse failed", level="warning", error=str(exc),
                 transcript=transcript_path)
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


def derive_turn_id(payload: dict, turn_count: int | None = None) -> str:
    """Stable per-turn ID: ``turn-<user message count>``.

    Counts user messages in the transcript with proper JSON parsing so that
    the string '"type":"user"' appearing inside tool output or assistant
    content is not mis-counted as a user message boundary. Callers that have
    already parsed the transcript (Stop hooks) pass ``turn_count`` so the
    file is never read twice.
    """
    turn_id = payload.get("turn_id")
    if turn_id:
        return str(turn_id)

    if turn_count is None:
        transcript = payload.get("transcript_path", "")
        if transcript:
            try:
                p = Path(transcript)
                if p.exists():
                    count = 0
                    for line in p.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or '"type"' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict) and obj.get("type") == "user":
                                count += 1
                        except (json.JSONDecodeError, ValueError):
                            pass
                    turn_count = count
            except Exception as exc:
                hook_log("transcript read failed", level="warning", error=str(exc),
                         transcript=transcript)
    if turn_count:
        return f"turn-{turn_count}"
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def mesh_node_id() -> str | None:
    """Return this node's mesh id when replication is enabled, else None."""
    if mesh is None:
        return None
    try:
        if mesh.is_enabled():
            return (mesh.load_config() or {}).get("node_id")
    except Exception:
        pass
    return None


def handle_stop(conn: sqlite3.Connection, payload: dict, mesh_node: str | None = None) -> None:
    session_id = payload.get("session_id", "unknown")
    now = datetime.now(timezone.utc).isoformat()
    cwd = payload.get("cwd")
    git_branch = get_git_branch(cwd) if cwd else None
    transcript = payload.get("transcript_path", "")
    message = payload.get("message") or {}
    t_info = get_transcript_info(transcript) if transcript else {"model": None, "usage": {}, "stop_reason": None, "turn_count": 0}
    # The transcript was parsed once above; reuse its user-message count for
    # the turn id instead of re-reading (and re-parsing) the whole file.
    turn_id = derive_turn_id(payload, t_info.get("turn_count"))
    model = t_info["model"] or payload.get("model") or message.get("model")
    model_id = common.ensure_model_row(conn, model)
    usage = t_info["usage"] if transcript else (payload.get("usage") or message.get("usage") or {})
    stop_reason = t_info["stop_reason"] or payload.get("stop_reason") or message.get("stop_reason")

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

    if mesh_node and mesh is not None and not mesh.is_synthetic_session(session_id):
        try:
            mesh.emit_event(conn, mesh_node, "turn", mesh.turn_payload({
                "session_id": session_id,
                "turn_id": turn_id,
                "recorded_at": now,
                "stop_reason": stop_reason,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                "cwd": cwd,
                "git_branch": git_branch,
                "model_key": model,
            }))
        except Exception as exc:
            hook_log("mesh emit failed (turn)", error=str(exc), session_id=session_id)

    conn.commit()


def handle_post_tool_use(conn: sqlite3.Connection, payload: dict, mesh_node: str | None = None) -> None:
    session_id = payload.get("session_id", "unknown")
    turn_id = derive_turn_id(payload)
    now = datetime.now(timezone.utc).isoformat()

    tool = payload.get("tool") or {}
    tool_name  = tool.get("name")  or payload.get("tool_name")
    tool_input = tool.get("input") or payload.get("tool_input")

    result    = payload.get("tool_result") or payload.get("result") or {}
    # Use an explicit None check: a successful exit_code of 0 is falsy, so
    # `result.get("exit_code") or payload.get("exit_code")` would discard it.
    exit_code = result.get("exit_code")
    if exit_code is None:
        exit_code = payload.get("exit_code")
    error     = result.get("stderr")       or payload.get("error")

    # Resolve turn_pk if the turns row already exists (it usually won't yet)
    cur = conn.execute(
        "SELECT id FROM turns WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id)
    )
    row = cur.fetchone()
    turn_pk = row[0] if row else None

    uid = uuid.uuid4().hex
    tool_input_json = json.dumps(tool_input) if tool_input is not None else None
    error_text = str(error) if error else None
    conn.execute("""
        INSERT INTO tool_calls (
            uid, turn_pk, session_id, turn_id, recorded_at,
            tool_name, tool_input, exit_code, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        uid,
        turn_pk,
        session_id,
        turn_id,
        now,
        tool_name,
        tool_input_json,
        exit_code,
        error_text,
    ))

    if mesh_node and mesh is not None and not mesh.is_synthetic_session(session_id):
        try:
            mesh.emit_event(conn, mesh_node, "tool_call", mesh.tool_call_payload({
                "uid": uid,
                "session_id": session_id,
                "turn_id": turn_id,
                "recorded_at": now,
                "tool_name": tool_name,
                "tool_input": tool_input_json,
                "exit_code": exit_code,
                "error": error_text,
            }))
        except Exception as exc:
            hook_log("mesh emit failed (tool_call)", error=str(exc), session_id=session_id)

    conn.commit()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("stop", "post-tool-use"):
        sys.exit(0)

    event = sys.argv[1]

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    conn = None
    try:
        # The hook fires on every turn and every tool call, so the schema is
        # prepared exactly once per database (PRAGMA user_version stamps it)
        # instead of re-running the full DDL + backfill on each invocation.
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        # WAL + a busy timeout let the hook write safely while the mesh
        # gossip daemon (a separate process) reads/applies concurrently.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        if conn.execute("PRAGMA user_version").fetchone()[0] < common.SCHEMA_USER_VERSION:
            common.ensure_base_schema(conn)
        mesh_node = mesh_node_id()
        retention_days = get_retention_days()
        if retention_days is not None:
            purge_old_records(conn, retention_days)
        if event == "stop":
            handle_stop(conn, payload, mesh_node)
        elif event == "post-tool-use":
            handle_post_tool_use(conn, payload, mesh_node)
        conn.commit()
    except Exception as exc:
        hook_log("hook failed", error=repr(exc), event=event)
    finally:
        if conn is not None:
            conn.close()
    try:
        ensure_dashboard_server()
    except Exception as exc:
        hook_log("dashboard server launch failed", error=str(exc))


if __name__ == "__main__":
    main()
