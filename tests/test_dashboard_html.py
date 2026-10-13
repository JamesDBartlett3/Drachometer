#!/usr/bin/env python3
"""Regression tests for the dashboard's HTML escaping (stored-XSS hardening).

session_id and stop_reason are DB columns a mesh peer (or a foreign
drag-and-drop database) can fill with any string, so both interpolations
must go through esc() -- rendered markup executes at the dashboard's origin,
where it could read /drachometer.db and drive the loopback control API.

Run with:  python -m unittest discover -s tests
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_SOURCE = (ROOT / "drachometer-dashboard.html").read_text(encoding="utf-8")

HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


class EscapingCallSitesTest(unittest.TestCase):
    """The tripwire: session_id/stop_reason must not reach innerHTML raw."""

    def test_session_table_escapes_session_id(self):
        self.assertIn("${esc(s.session_id.slice(0, 12))}", HTML_SOURCE)
        self.assertNotIn("${s.session_id.slice", HTML_SOURCE)

    def test_turns_table_escapes_stop_reason(self):
        self.assertIn("${esc(t.stop_reason)}", HTML_SOURCE)
        self.assertNotIn("${t.stop_reason", HTML_SOURCE)


class EscBehaviorTest(unittest.TestCase):
    """Runs the page's actual esc() under node and confirms it neutralizes
    hostile DB values. Skipped where node is unavailable."""

    def _esc_source(self) -> str:
        match = re.search(r"function esc\(s\) \{.*?\n      \}", HTML_SOURCE, re.S)
        self.assertIsNotNone(match, "esc() not found in drachometer-dashboard.html")
        return match.group(0)

    def test_hostile_session_id_and_stop_reason_render_escaped(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        script = (
            self._esc_source()
            + f"""
const hostile = {HOSTILE!r};
const sessionCell = `<td class="mono">${{esc(hostile.slice(0, 12))}}...</td>`;
const stopCell = `<td>${{esc(hostile)}}</td>`;
for (const rendered of [sessionCell, stopCell]) {{
    if (/[<>]/.test(rendered.replace(/<(\\/?)td[^>]*>|\\.\\.\\./g, ""))) {{
        console.error(rendered);
        process.exit(1);
    }}
    if (rendered.includes("<img") || rendered.includes("<script")) {{
        console.error(rendered);
        process.exit(1);
    }}
}}
console.log("ok");
"""
        )
        result = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"hostile value rendered unescaped:\n{result.stderr}")
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
