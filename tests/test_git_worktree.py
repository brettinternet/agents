from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.git_worktree import (
    GitError,
    add_agent_snapshot,
    add_detached,
    branch_sha,
    commit_diff,
    git,
    head_sha,
    import_isolated_submission,
    is_ancestor,
    is_clean,
    remove_recorded_workspace,
    remove_recorded_worktree,
    reserve_execution,
    reserve_execution_workspace,
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

    def _config(self, mode="container", root=None):
        isolation = SimpleNamespace(value=mode)
        return SimpleNamespace(
            root=self.root if root is None else root,
            execution=SimpleNamespace(isolation=isolation),
        )

    def _commit(self, repo, message):
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True, capture_output=True)
        return head_sha(repo)

    def test_mode_aware_host_helpers_delegate_unchanged(self):
        config = self._config("host")
        path = self.root / "work"
        with patch("agents.git_worktree.reserve_execution", return_value=("base", "branch")) as reserve:
            self.assertEqual(
                reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path),
                ("base", "branch"),
            )
        reserve.assert_called_once_with(self.repo, "main", "ITEM", 1, path)

        with patch("agents.git_worktree.add_detached") as snapshot:
            add_agent_snapshot(config, self.repo, self.base, path)
        snapshot.assert_called_once_with(self.repo, self.base, path)

        with patch("agents.git_worktree.remove_recorded_worktree") as remove:
            remove_recorded_workspace(config, self.repo, path, self.base, allow_dirty=True)
        remove.assert_called_once_with(self.repo, path, self.base, allow_dirty=True)

    def test_container_reservation_is_standalone_shallow_and_adoptable(self):
        config = self._config()
        path = self.root / "managed" / "work"
        base, branch = reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path)

        self.assertEqual((base, branch), (self.base, "agents/item/1"))
        self.assertEqual(head_sha(path), base)
        self.assertEqual(git(path, "remote"), "")
        self.assertEqual(git(path, "rev-parse", "--is-shallow-repository"), "true")
        self.assertFalse((path / ".git" / "objects" / "info" / "alternates").exists())
        self.assertEqual(git(path, "config", "--local", "--get", "user.name"), "Test")
        self.assertEqual(git(path, "config", "--local", "--get", "user.email"), "test@example.com")
        self.assertEqual(git(path, "symbolic-ref", "--short", "HEAD"), branch)
        self.assertEqual(branch_sha(self.repo, branch), base)
        self.assertEqual(
            reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path),
            (base, branch),
        )

    def test_container_snapshot_is_detached_standalone_and_shallow(self):
        config = self._config()
        path = self.root / "managed" / "snapshot"
        add_agent_snapshot(config, self.repo, self.base, path)

        self.assertEqual(head_sha(path), self.base)
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(git(path, "remote"), "")
        self.assertEqual(git(path, "rev-parse", "--is-shallow-repository"), "true")
        self.assertFalse((path / ".git" / "objects" / "info" / "alternates").exists())

    def test_import_transfers_exact_descendant_and_cleans_temporary_ref(self):
        config = self._config()
        path = self.root / "managed" / "work"
        base, branch = reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path)
        (path / "file").write_text("changed")
        submitted = self._commit(path, "change")

        import_isolated_submission(self.repo, path, branch, base, submitted, 1, 1)

        self.assertEqual(branch_sha(self.repo, branch), submitted)
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/agents/import"), "")

    def test_import_rejects_dirty_wrong_branch_and_wrong_sha(self):
        config = self._config()
        path = self.root / "managed" / "work"
        base, branch = reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path)

        (path / "untracked").write_text("dirty")
        with self.assertRaises(GitError):
            import_isolated_submission(self.repo, path, branch, base, base, 1, 1)
        (path / "untracked").unlink()

        git(path, "checkout", "-b", "wrong")
        with self.assertRaises(GitError):
            import_isolated_submission(self.repo, path, branch, base, base, 1, 1)
        git(path, "checkout", branch)

        (path / "file").write_text("changed")
        submitted = self._commit(path, "change")
        with self.assertRaises(GitError):
            import_isolated_submission(self.repo, path, branch, base, base, 1, 1)
        self.assertEqual(branch_sha(self.repo, branch), base)
        self.assertEqual(head_sha(path), submitted)

    def test_import_rejects_non_descendant_and_symlink(self):
        config = self._config()
        path = self.root / "managed" / "work"
        base, branch = reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path)
        git(path, "checkout", "--orphan", "unrelated")
        git(path, "rm", "-rf", ".")
        (path / "other").write_text("unrelated")
        unrelated = self._commit(path, "unrelated")
        git(path, "branch", "-M", branch)
        with self.assertRaises(GitError):
            import_isolated_submission(self.repo, path, branch, base, unrelated, 1, 1)
        self.assertEqual(branch_sha(self.repo, branch), base)

        real = self.root / "managed" / "real"
        reserve_execution_workspace(config, self.repo, "main", "SYMLINK", 1, real)
        link = self.root / "managed" / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(GitError):
            import_isolated_submission(self.repo, link, "agents/symlink/1", self.base, self.base, 1, 1)

    def test_import_uses_expected_base_for_atomic_branch_update(self):
        config = self._config()
        path = self.root / "managed" / "work"
        base, branch = reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path)
        (path / "file").write_text("changed")
        submitted = self._commit(path, "change")

        (self.repo / "file").write_text("advanced")
        advanced = self._commit(self.repo, "advance")
        git(self.repo, "update-ref", f"refs/heads/{branch}", advanced, base)

        with self.assertRaises(GitError):
            import_isolated_submission(self.repo, path, branch, base, submitted, 1, 1)
        self.assertEqual(branch_sha(self.repo, branch), advanced)
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/agents/import"), "")

    def test_container_removal_checks_identity_scope_and_dirty_policy(self):
        config = self._config()
        path = self.root / "managed" / "work"
        base, _ = reserve_execution_workspace(config, self.repo, "main", "ITEM", 1, path)

        with self.assertRaises(GitError):
            remove_recorded_workspace(config, self.repo, path, "0" * 40)
        self.assertTrue(path.exists())

        (path / "dirty").write_text("dirty")
        with self.assertRaises(GitError):
            remove_recorded_workspace(config, self.repo, path, base)
        remove_recorded_workspace(config, self.repo, path, base, allow_dirty=True)
        self.assertFalse(path.exists())

        outside_config = self._config(root=self.root / "managed")
        outside = self.root / "outside"
        with self.assertRaisesRegex(GitError, "unmanaged workspace"):
            reserve_execution_workspace(outside_config, self.repo, "main", "OUTSIDE", 1, outside)
        self.assertFalse(outside.exists())

        real = self.root / "managed" / "real"
        reserve_execution_workspace(config, self.repo, "main", "REAL", 1, real)
        link = self.root / "managed" / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(GitError):
            remove_recorded_workspace(config, self.repo, link, self.base)
