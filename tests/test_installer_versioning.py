#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("drachometer_install", ROOT / "drachometer-install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerVersioningTest(unittest.TestCase):
    def test_write_installed_version_replaces_stale_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            claude_dir = tmp / ".claude"
            hooks_root = claude_dir / "hooks"
            hooks_dir = hooks_root / "drachometer"
            version_path = hooks_dir / "drachometer-version.json"
            legacy_version_path = hooks_root / "drachometer-version.json"

            installer.CLAUDE_DIR = claude_dir
            installer.HOOKS_ROOT_DIR = hooks_root
            installer.HOOKS_DIR = hooks_dir
            installer.VERSION_PATH = version_path
            installer.LEGACY_VERSION_PATH = legacy_version_path

            version_path.parent.mkdir(parents=True, exist_ok=True)
            stale = {"version": "0.0.1", "releases_api": "https://api.example.com", "repository": "owner/repo"}
            version_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
            legacy_version_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_version_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")

            installer.write_installed_version("2.3.4")

            data = json.loads(version_path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "2.3.4")
            # Existing metadata fields must be preserved so checkForUpdates() keeps working.
            self.assertEqual(data["releases_api"], "https://api.example.com")
            self.assertEqual(data["repository"], "owner/repo")
            legacy_data = json.loads(legacy_version_path.read_text(encoding="utf-8"))
            self.assertEqual(legacy_data["version"], "2.3.4")
            self.assertEqual(legacy_data["releases_api"], "https://api.example.com")

    def test_copy_hooks_rewrites_version_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo_root = tmp / "repo"
            repo_root.mkdir()
            (repo_root / "hooks").mkdir()
            (repo_root / "drachometer-version.json").write_text(json.dumps({"version": "9.9.9"}) + "\n", encoding="utf-8")
            (repo_root / "drachometer-dashboard.html").write_text("<html></html>", encoding="utf-8")
            (repo_root / "drachometer-logo.svg").write_text("logo", encoding="utf-8")
            (repo_root / "README.md").write_text("readme", encoding="utf-8")
            (repo_root / "drachometer-pricing.json").write_text("{}", encoding="utf-8")
            (repo_root / "drachometer_mesh.py").write_text("print('mesh')\n", encoding="utf-8")
            (repo_root / "drachometer-serve-dashboard.py").write_text("print('serve')\n", encoding="utf-8")
            (repo_root / "hooks" / "drachometer-log-usage.py").write_text("print('hook')\n", encoding="utf-8")

            claude_dir = tmp / ".claude"
            hooks_root = claude_dir / "hooks"
            hooks_dir = hooks_root / "drachometer"
            installer.CLAUDE_DIR = claude_dir
            installer.HOOKS_ROOT_DIR = hooks_root
            installer.HOOKS_DIR = hooks_dir
            installer.VERSION_PATH = hooks_dir / "drachometer-version.json"
            installer.LEGACY_VERSION_PATH = hooks_root / "drachometer-version.json"
            installer.REPO_VERSION = repo_root / "drachometer-version.json"
            installer.REPO_README = repo_root / "README.md"
            installer.REPO_LOGO = repo_root / "drachometer-logo.svg"
            installer.REPO_PRICING = repo_root / "drachometer-pricing.json"
            installer.REPO_SERVER = repo_root / "drachometer-serve-dashboard.py"
            installer.REPO_MESH = repo_root / "drachometer_mesh.py"
            installer.REPO_DASHBOARD = repo_root / "drachometer-dashboard.html"
            installer.HOOK_FILES = {
                "drachometer-log-usage.py": repo_root / "hooks" / "drachometer-log-usage.py",
                "drachometer-serve-dashboard.py": repo_root / "drachometer-serve-dashboard.py",
                "drachometer_mesh.py": repo_root / "drachometer_mesh.py",
                "drachometer-dashboard.html": repo_root / "drachometer-dashboard.html",
                "README.md": repo_root / "README.md",
                "drachometer-logo.svg": repo_root / "drachometer-logo.svg",
                "drachometer-version.json": repo_root / "drachometer-version.json",
                "drachometer-pricing.json": repo_root / "drachometer-pricing.json",
            }
            installer.APP_VERSION = "9.9.9"

            with mock.patch.object(installer, "write_installed_version") as write_version:
                installer.copy_hooks("python3")

            write_version.assert_called_once_with("9.9.9")


if __name__ == "__main__":
    unittest.main()
