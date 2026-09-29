#!/usr/bin/env python3
"""Tests for the dashboard server's repository-root resolution (the backend
of the Repositories tab's group-by-repo-root behavior)."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The server file uses dashes in its name, so it cannot be imported normally.
_SPEC = importlib.util.spec_from_file_location(
    "drachometer_serve_dashboard", ROOT / "drachometer-serve-dashboard.py"
)
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


@unittest.skipIf(shutil.which("git") is None, "git not available")
class ResolveRepoRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Resolution results are cached for the life of the process; tests
        # create throwaway repos, so each starts from a clean cache.
        server._repo_root_cache.clear()
        server._repo_name_cache.clear()

    def _init_repo(self, path: Path) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)

    def test_repo_and_subdirectory_resolve_to_the_same_root(self):
        repo = self.tmp / "repo"
        self._init_repo(repo)
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        expected = repo.resolve()
        for cwd in (repo, repo / "src", sub):
            with self.subTest(cwd=str(cwd)):
                root = server._resolve_repo_root(str(cwd))
                self.assertIsNotNone(root)
                self.assertEqual(Path(root).resolve(), expected)

    def test_directory_outside_any_repo_returns_none(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        # Skip when a parent of the temp dir happens to be a git work tree.
        probe = server._resolve_repo_root(str(plain))
        if probe is not None:
            self.skipTest("temp directory sits inside a git repository")
        self.assertIsNone(server._resolve_repo_root(str(plain)))

    def test_nonexistent_path_returns_none(self):
        # Mesh-replicated turns carry cwds from other machines; git cannot
        # resolve those locally and the dashboard falls back to path grouping.
        self.assertIsNone(
            server._resolve_repo_root(str(self.tmp / "does" / "not" / "exist"))
        )

    def test_resolve_repo_roots_batches_and_caches(self):
        repo = self.tmp / "repo"
        self._init_repo(repo)
        first = server._resolve_repo_roots([str(repo), str(self.tmp / "nope")])
        self.assertEqual(Path(first[str(repo)]).resolve(), repo.resolve())
        self.assertIsNone(first[str(self.tmp / "nope")])
        # Second call is served from the in-process cache.
        second = server._resolve_repo_roots([str(repo)])
        self.assertEqual(second[str(repo)], first[str(repo)])
        self.assertNotIn(str(self.tmp / "nope"), second)


class RepoFullNameFromUrlTest(unittest.TestCase):
    def test_https_and_ssh_urls(self):
        cases = {
            "https://github.com/acme/widgets.git": "acme/widgets",
            "https://github.com/acme/widgets": "acme/widgets",
            "http://gitlab.example.org/acme/widgets.git": "acme/widgets",
            "ssh://git@github.com/acme/widgets.git": "acme/widgets",
            "git@github.com:acme/widgets.git": "acme/widgets",
            "git@github.com:acme/widgets": "acme/widgets",
            # Hosted servers may nest (GitLab subgroups): the last two
            # segments are the owning namespace and the repository.
            "https://gitlab.com/group/sub/repo.git": "sub/repo",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(server._repo_full_name_from_url(url), expected)

    def test_non_hosted_or_incomplete_urls_return_none(self):
        cases = [
            None,
            "",
            "   ",
            "https://github.com/only-a-repo.git",  # no owner segment
            "https://github.com",
            "/home/james/dev/repo",  # plain local path
            "file:///home/james/dev/repo",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertIsNone(server._repo_full_name_from_url(url))


@unittest.skipIf(shutil.which("git") is None, "git not available")
class ResolveRepoNameTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        server._repo_root_cache.clear()
        server._repo_name_cache.clear()

    def _init_repo(self, path: Path, origin: str | None = None) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        if origin:
            subprocess.run(
                ["git", "-C", str(path), "remote", "add", "origin", origin],
                check=True,
            )

    def test_repo_with_origin_resolves_full_name(self):
        repo = self.tmp / "repo"
        self._init_repo(repo, "https://github.com/acme/widgets.git")
        self.assertEqual(server._resolve_repo_name(str(repo)), "acme/widgets")

    def test_repo_without_origin_returns_none(self):
        repo = self.tmp / "repo"
        self._init_repo(repo)
        self.assertIsNone(server._resolve_repo_name(str(repo)))

    def test_nonexistent_root_returns_none(self):
        self.assertIsNone(server._resolve_repo_name(str(self.tmp / "nope")))

    def test_resolve_repo_names_keys_by_cwd_and_caches_by_root(self):
        repo = self.tmp / "repo"
        self._init_repo(repo, "git@github.com:acme/widgets.git")
        sub = repo / "src"
        sub.mkdir()
        roots = {str(repo): str(repo), str(sub): str(repo), str(self.tmp / "plain"): None}
        (self.tmp / "plain").mkdir()
        names = server._resolve_repo_names(roots)
        self.assertEqual(names[str(repo)], "acme/widgets")
        self.assertEqual(names[str(sub)], "acme/widgets")
        self.assertIsNone(names[str(self.tmp / "plain")])
        # Second call is served from the per-root cache.
        self.assertEqual(server._resolve_repo_names(roots), names)


class PreferencesTest(unittest.TestCase):
    """Round-trips through the preferences storage, pointed at a throwaway
    settings.json instead of the real one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_path = server.SETTINGS_PATH
        server.SETTINGS_PATH = tmp / "settings.json"
        self.addCleanup(setattr, server, "SETTINGS_PATH", self._orig_settings_path)

    def _read(self):
        handler = server.Handler.__new__(server.Handler)
        return handler._read_preferences()

    def _write(self, body):
        # The method only touches module-level SETTINGS_PATH and the body, so
        # an unbound call on an unconstructed handler is safe.
        handler = server.Handler.__new__(server.Handler)
        return handler._write_preferences(body)

    def _stored(self):
        return json.loads(server.SETTINGS_PATH.read_text(encoding="utf-8"))

    def test_group_by_name_round_trips(self):
        self.assertFalse(self._read()["group_repos_by_name"])
        self.assertTrue(self._write({"group_repos_by_name": True})["ok"])
        self.assertTrue(self._stored()["group_repos_by_name"])
        self.assertTrue(self._write({"group_repos_by_name": False})["ok"])
        # An explicit false is a real setting (only None/absent clears a key).
        self.assertFalse(self._stored()["group_repos_by_name"])

    def test_writing_one_preference_leaves_the_other_alone(self):
        self.assertTrue(self._write({"token_usage_retention_days": "30"})["ok"])
        self.assertTrue(self._write({"group_repos_by_name": True})["ok"])
        # The toggle save must not clobber the stored retention.
        self.assertEqual(self._stored()["token_usage_retention_days"], 30)
        self.assertTrue(self._write({"token_usage_retention_days": ""})["ok"])
        self.assertTrue(self._stored()["group_repos_by_name"])

    def test_invalid_retention_rejects_without_touching_settings(self):
        self.assertTrue(self._write({"group_repos_by_name": True})["ok"])
        result = self._write({"token_usage_retention_days": "-5"})
        self.assertFalse(result["ok"])
        self.assertNotIn("token_usage_retention_days", self._stored())


if __name__ == "__main__":
    unittest.main()
