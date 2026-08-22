from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agents.git_worktree import (
    GitError,
    add_detached,
    branch_sha,
    commit_diff,
    head_sha,
    is_ancestor,
    is_clean,
    remove_recorded_worktree,
    reserve_execution,
)


class GitWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "file").write_text("base")
        subprocess.run(["git", "-C", str(self.repo), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "base"], check=True, capture_output=True)
        self.base = branch_sha(self.repo, "main")

    def tearDown(self):
        self.temp.cleanup()

    def test_execution_branch_and_worktree_are_deterministic_and_adoptable(self):
        path = self.root / "work"
        base, branch = reserve_execution(self.repo, "main", "AGENT-0001", 1, path)
        self.assertEqual(base, self.base)
        self.assertEqual(branch, "agents/agent-0001/1")
        self.assertEqual(head_sha(path), base)
        self.assertTrue(is_clean(path))
        self.assertEqual(reserve_execution(self.repo, "main", "AGENT-0001", 1, path), (base, branch))

    def test_partial_mismatch_is_rejected(self):
        subprocess.run(["git", "-C", str(self.repo), "branch", "agents/agent-0001/1"], check=True)
        (self.repo / "file").write_text("advance")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-am", "advance"], check=True, capture_output=True)
        with self.assertRaises(GitError):
            reserve_execution(self.repo, "main", "AGENT-0001", 1, self.root / "work")

    def test_detached_check_tree_and_safe_removal(self):
        path = self.root / "check"
        add_detached(self.repo, self.base, path)
        self.assertEqual(head_sha(path), self.base)
        (path / "generated").write_text("dirty")
        self.assertFalse(is_clean(path))
        with self.assertRaises(GitError):
            remove_recorded_worktree(self.repo, path, self.base)
        remove_recorded_worktree(self.repo, path, self.base, allow_dirty=True)
        self.assertFalse(path.exists())

    def test_commit_diff_and_ancestry(self):
        path = self.root / "work"
        reserve_execution(self.repo, "main", "AGENT-0002", 1, path)
        (path / "file").write_text("changed")
        subprocess.run(["git", "-C", str(path), "commit", "-am", "change"], check=True, capture_output=True)
        target = head_sha(path)
        self.assertTrue(is_ancestor(self.repo, self.base, target))
        evidence = commit_diff(self.repo, self.base, target)
        self.assertIn("change", evidence["commits"])
        self.assertIn("changed", evidence["diff"])
