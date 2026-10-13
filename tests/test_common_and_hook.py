#!/usr/bin/env python3
"""Tests for drachometer_common (shared pricing/model/schema helpers) and the
usage hook's transcript parsing.

Stdlib unittest only -- no third-party dependencies, matching the project.
Run with:  python -m unittest discover -s tests
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import drachometer_common as common  # noqa: E402

# The hook file uses dashes in its name, so it cannot be imported normally.
_HOOK_SPEC = importlib.util.spec_from_file_location(
    "drachometer_log_usage", ROOT / "hooks" / "drachometer-log-usage.py"
)
hook = importlib.util.module_from_spec(_HOOK_SPEC)
_HOOK_SPEC.loader.exec_module(hook)


def line(obj: dict) -> str:
    return json.dumps(obj)


def assistant_line(msg_id: str, model: str, usage: dict) -> str:
    return line({
        "type": "assistant",
        "message": {"id": msg_id, "model": model, "stop_reason": "end_turn", "usage": usage},
    })


class InferModelAttributesTest(unittest.TestCase):
    def test_known_tiers_map_to_pricing(self):
        for tier, key in [
            ("fable", "claude-fable-5-1"),
            ("opus", "claude-opus-5-5"),
            ("sonnet", "claude-sonnet-5"),
            ("haiku", "claude-haiku-4-5-20251001"),
        ]:
            attrs = common.infer_model_attributes(key)
            fallback = common.MODEL_TIER_PRICING[tier]
            self.assertEqual(attrs["model_provider"], "Anthropic")
            self.assertEqual(attrs["input_price_per_mtok"], fallback["input"])
            self.assertEqual(attrs["output_price_per_mtok"], fallback["output"])

    def test_unknown_model_has_no_prices(self):
        attrs = common.infer_model_attributes("gpt-9")
        self.assertIsNone(attrs["model_provider"])
        self.assertIsNone(attrs["input_price_per_mtok"])

    def test_empty_key_returns_all_none(self):
        attrs = common.infer_model_attributes("")
        self.assertTrue(all(v is None for v in attrs.values()))


class PricingOverrideTest(unittest.TestCase):
    def test_partial_tier_is_ignored(self):
        # A tier missing any price key must not override its fallback --
        # half-updated pricing files used to produce NULL prices.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"tiers": {"opus": {"input": 99.0}}}, fh)
            path = Path(fh.name)
        try:
            before = dict(common.MODEL_TIER_PRICING["opus"])
            common.load_pricing_overrides(path)
            self.assertEqual(common.MODEL_TIER_PRICING["opus"], before)
        finally:
            path.unlink(missing_ok=True)

    def test_complete_tier_overrides(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"tiers": {"gemma": {
                "input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_create": 0.2,
            }}}, fh)
            path = Path(fh.name)
        try:
            common.load_pricing_overrides(path)
            self.assertEqual(common.MODEL_TIER_PRICING["gemma"]["input"], 1.0)
        finally:
            path.unlink(missing_ok=True)


class EnsureBaseSchemaTest(unittest.TestCase):
    def test_creates_schema_and_stamps_user_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            conn = sqlite3.connect(db)
            try:
                common.ensure_base_schema(conn)
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertLessEqual({"models", "turns", "tool_calls", "oplog"}, tables)
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    common.SCHEMA_USER_VERSION,
                )
            finally:
                conn.close()

    def test_idempotent_and_upgrades_legacy_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            conn = sqlite3.connect(db)
            try:
                common.ensure_base_schema(conn)
                common.ensure_base_schema(conn)  # second run is a no-op
                cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
                self.assertLessEqual({"cwd", "git_branch", "model", "model_id"}, cols)
            finally:
                conn.close()


class TranscriptParsingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # The incremental turn counter persists per-transcript state; keep
        # tests away from the real ~/.claude file.
        self._orig_state_path = hook.TURN_STATE_PATH
        hook.TURN_STATE_PATH = self.tmp / "turn-state.json"
        self.addCleanup(setattr, hook, "TURN_STATE_PATH", self._orig_state_path)

    def _transcript(self, lines: list[str]) -> str:
        p = self.tmp / "transcript.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_sums_unique_messages_and_counts_turns(self):
        path = self._transcript([
            line({"type": "user", "message": {"role": "user", "content": "hi"}}),
            assistant_line("msg-1", "claude-opus-5-5",
                           {"input_tokens": 100, "output_tokens": 10,
                            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1}),
            # Streaming duplicate of the same message: only the last snapshot counts.
            assistant_line("msg-1", "claude-opus-5-5",
                           {"input_tokens": 100, "output_tokens": 25,
                            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1}),
            # A distinct API call within the same turn is summed.
            assistant_line("msg-2", "claude-opus-5-5",
                           {"input_tokens": 50, "output_tokens": 5,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            line({"type": "user", "message": {"role": "user", "content": "again"}}),
            assistant_line("msg-3", "claude-sonnet-5",
                           {"input_tokens": 7, "output_tokens": 3,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        ])
        info = hook.get_transcript_info(path)
        self.assertEqual(info["turn_count"], 2)
        self.assertEqual(info["model"], "claude-sonnet-5")
        # Current turn only (after the last user message).
        self.assertEqual(info["usage"]["input_tokens"], 7)
        self.assertEqual(info["usage"]["output_tokens"], 3)

    def test_user_string_inside_tool_output_is_not_a_turn_boundary(self):
        tricky = line({
            "type": "assistant",
            "message": {"id": "msg-1", "model": "claude-opus-5-5",
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 9, "output_tokens": 9,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}},
        })
        # A tool result whose content embeds the literal user-type JSON would
        # fool a substring matcher; JSON parsing must not count it.
        fake_user_in_output = line({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "content": 'noise with \\"type\\": \\"user\\" inside'}],
                        },
        })
        path = self._transcript([
            line({"type": "user", "message": {"role": "user", "content": "hi"}}),
            tricky,
            fake_user_in_output,
        ])
        info = hook.get_transcript_info(path)
        self.assertEqual(info["turn_count"], 2)  # the two real user lines

    def test_missing_transcript_returns_empty_info(self):
        info = hook.get_transcript_info(str(self.tmp / "nope.jsonl"))
        self.assertEqual(info["turn_count"], 0)
        self.assertIsNone(info["model"])

    def test_derive_turn_id_prefers_explicit_then_count(self):
        self.assertEqual(hook.derive_turn_id({"turn_id": "turn-7"}), "turn-7")
        path = self._transcript([
            line({"type": "user", "message": {"content": "a"}}),
            line({"type": "user", "message": {"content": "b"}}),
            line({"type": "user", "message": {"content": "c"}}),
        ])
        self.assertEqual(hook.derive_turn_id({"transcript_path": path}), "turn-3")
        # A caller that already parsed the transcript passes the count through.
        self.assertEqual(hook.derive_turn_id({}, turn_count=11), "turn-11")
        self.assertRegex(hook.derive_turn_id({}), r"^\d{8}T")  # timestamp fallback


class ParseRetentionDaysTest(unittest.TestCase):
    def test_valid_values(self):
        self.assertIsNone(hook.parse_retention_days(None))
        self.assertEqual(hook.parse_retention_days("30"), 30)
        self.assertEqual(hook.parse_retention_days(7), 7)
        self.assertIsNone(hook.parse_retention_days("-1"))
        self.assertIsNone(hook.parse_retention_days("nope"))

    def test_zero_means_keep_everything_not_delete_everything(self):
        # Regression: 0 used to parse as a 0-day window, whose cutoff is
        # "now" -- purging every existing record on the next hook run.
        # 0 must disable retention, matching the mesh's compact_oplog.
        for zero in (0, "0", " 0 "):
            self.assertIsNone(hook.parse_retention_days(zero))


class PurgeOldRecordsTest(unittest.TestCase):
    def _seeded_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(common.BASE_SCHEMA_DDL)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=40)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()
        for i, recorded in enumerate((old, recent)):
            conn.execute(
                """INSERT INTO turns (session_id, turn_id, recorded_at, input_tokens, output_tokens)
                   VALUES (?, ?, ?, 10, 5)""",
                (f"s-{i}", "turn-1", recorded),
            )
        conn.execute(
            "INSERT INTO tool_calls (session_id, turn_id, recorded_at, tool_name)"
            " VALUES ('s-0', 'turn-1', ?, 'Bash')",
            (old,),
        )
        conn.commit()
        return conn

    def test_positive_window_deletes_only_rows_older_than_cutoff(self):
        conn = self._seeded_conn()
        try:
            hook.purge_old_records(conn, 30)
            self.assertEqual(
                [r[0] for r in conn.execute("SELECT session_id FROM turns")],
                ["s-1"],
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 0)
        finally:
            conn.close()

    def test_zero_and_negative_windows_delete_nothing(self):
        for days in (0, -3):
            with self.subTest(days=days):
                conn = self._seeded_conn()
                try:
                    hook.purge_old_records(conn, days)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 1)
                finally:
                    conn.close()

    def test_positive_window_also_purges_oplog_and_records_watermark(self):
        # Regression: the oplog was never purged, so mesh gossip re-fed (and
        # re-projected) deleted turns/tool_calls -- purged history resurrected.
        conn = self._seeded_conn()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mesh_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            now = datetime.now(timezone.utc)
            old = (now - timedelta(days=40)).isoformat()
            recent = (now - timedelta(days=1)).isoformat()
            for i, created in enumerate((old, recent)):
                conn.execute(
                    "INSERT INTO oplog (event_id, origin_node, lamport, created_at, entity, op, payload)"
                    " VALUES (?, 'n', ?, ?, 'turn', 'upsert', '{}')",
                    (f"e-{i}", i, created),
                )
            conn.commit()
            hook.purge_old_records(conn, 30)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM oplog").fetchone()[0], 1)
            watermark = conn.execute(
                "SELECT value FROM mesh_meta WHERE key = 'compaction_watermark'").fetchone()
            self.assertIsNotNone(watermark)
            # The watermark is the purge cutoff: strictly newer than the deleted
            # event, not newer than "now" -- anti-entropy skips everything below it.
            self.assertGreater(watermark[0], old)
            self.assertLessEqual(watermark[0], now.isoformat())
        finally:
            conn.close()

    def test_purge_without_mesh_tables_still_purges_base_tables(self):
        # A pre-mesh database has no oplog/mesh_meta; purging must not fail.
        conn = self._seeded_conn()
        try:
            hook.purge_old_records(conn, 30)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
        finally:
            conn.close()


class IncrementalTurnCountTest(unittest.TestCase):
    """_count_user_messages must keep O(delta) cost per PostToolUse and stay
    count-consistent with get_transcript_info across appends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._orig_state_path = hook.TURN_STATE_PATH
        hook.TURN_STATE_PATH = self.tmp / "turn-state.json"
        self.addCleanup(setattr, hook, "TURN_STATE_PATH", self._orig_state_path)

    def _write(self, text: str) -> Path:
        p = self.tmp / "transcript.jsonl"
        p.write_text(text, encoding="utf-8")
        return p

    def _user_line(self, content: str) -> str:
        return line({"type": "user", "message": {"role": "user", "content": content}})

    def test_counts_stay_consistent_across_appends(self):
        p = self._write(self._user_line("a") + "\n")
        self.assertEqual(hook._count_user_messages(str(p)), 1)
        # Tool call appends an assistant line, then a second turn starts.
        with p.open("a", encoding="utf-8") as fh:
            fh.write(assistant_line("msg-1", "claude-opus-5-5",
                                    {"input_tokens": 1, "output_tokens": 1,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0}) + "\n")
        self.assertEqual(hook._count_user_messages(str(p)), 1)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(self._user_line("b") + "\n")
        self.assertEqual(hook._count_user_messages(str(p)), 2)

        # A full parse (the Stop path) must agree with the incremental count.
        self.assertEqual(hook.get_transcript_info(str(p))["turn_count"], 2)

    def test_trailing_partial_line_is_not_counted_and_not_skipped(self):
        p = self._write(self._user_line("a") + "\n")
        self.assertEqual(hook._count_user_messages(str(p)), 1)
        # Concurrently-written transcript: an incomplete line hangs at the end.
        with p.open("a", encoding="utf-8") as fh:
            fh.write('{"type": "user", "mess')
        self.assertEqual(hook._count_user_messages(str(p)), 1)
        # The line completes; the same region must be re-read, not skipped.
        with p.open("a", encoding="utf-8") as fh:
            fh.write('age": {"role": "user", "content": "b"}}\n')
        self.assertEqual(hook._count_user_messages(str(p)), 2)

    def test_truncated_or_new_transcript_falls_back_to_full_parse(self):
        p = self._write(self._user_line("a") + "\n" + self._user_line("b") + "\n")
        self.assertEqual(hook._count_user_messages(str(p)), 2)
        p.write_text(self._user_line("c") + "\n", encoding="utf-8")
        self.assertEqual(hook._count_user_messages(str(p)), 1)

    def test_derive_turn_id_uses_the_incremental_count(self):
        p = self._write(self._user_line("a") + "\n" + self._user_line("b") + "\n")
        self.assertEqual(hook.derive_turn_id({"transcript_path": str(p)}), "turn-2")


if __name__ == "__main__":
    unittest.main()
