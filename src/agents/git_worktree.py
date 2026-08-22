from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(project: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise GitError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def identity(project: Path) -> tuple[Path, Path]:
    canonical = project.resolve()
    common = Path(git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    return canonical, common


def validate_project(project: Path, default_branch: str) -> tuple[Path, Path]:
    """Validate the repository can support isolated Agents worktrees."""
    if not project.is_dir() or project.is_symlink():
        raise GitError("configured project path is not a real directory")
    canonical, common = identity(project)
    branch_sha(project, default_branch)
    # A non-bare repository must expose the worktree plumbing used by every
    # execution/check/review saga.  This also rejects a repository with a
    # missing or unusable common directory before any dispatch side effect.
    if not common.is_dir():
        raise GitError("git common directory is unavailable")
    listing = git(project, "worktree", "list", "--porcelain")
    if not listing:
        raise GitError("git worktree list returned no main worktree")
    return canonical, common


def branch_sha(project: Path, branch: str) -> str:
    return git(project, "rev-parse", "--verify", f"refs/heads/{branch}")


def head_sha(worktree: Path) -> str:
    return git(worktree, "rev-parse", "HEAD")


def is_clean(worktree: Path) -> bool:
    return git(worktree, "status", "--porcelain=v1", "--untracked-files=all") == ""


def is_ancestor(project: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(project), "merge-base", "--is-ancestor", ancestor, descendant], check=False
        ).returncode
        == 0
    )


def reserve_execution(project: Path, default_branch: str, item_id: str, number: int, worktree: Path) -> tuple[str, str]:
    base = branch_sha(project, default_branch)
    branch = f"agents/{item_id.lower()}/{number}"
    branch_ref = f"refs/heads/{branch}"
    if worktree.is_symlink():
        raise GitError("recorded worktree path is a symlink")
    path = worktree.resolve()
    branch_exists = (
        subprocess.run(
            ["git", "-C", str(project), "show-ref", "--verify", "--quiet", branch_ref], check=False
        ).returncode
        == 0
    )
    if path.exists() and any(path.iterdir()):
        common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        expected = Path(git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if common != expected or head_sha(path) != (git(project, "rev-parse", branch_ref) if branch_exists else base):
            raise GitError("recorded worktree does not match project/branch")
        return base, branch
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists:
        if git(project, "rev-parse", branch_ref) != base:
            raise GitError("recorded branch exists at unexpected base")
        git(project, "worktree", "add", str(path), branch)
    else:
        git(project, "worktree", "add", "-b", branch, str(path), base)
    return base, branch


def add_detached(project: Path, target_sha: str, path: Path) -> None:
    if path.is_symlink():
        raise GitError("detached worktree path is a symlink")
    if path.exists() and any(path.iterdir()):
        common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        expected = Path(git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if common != expected or head_sha(path) != target_sha:
            raise GitError("detached worktree does not match project/HEAD")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    git(project, "worktree", "add", "--detach", str(path), target_sha)


def remove_recorded_worktree(project: Path, path: Path, target_sha: str, *, allow_dirty: bool = False) -> None:
    if path.is_symlink():
        raise GitError("refusing to remove symlinked worktree")
    if not path.exists():
        return
    common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    expected = Path(git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common != expected or head_sha(path) != target_sha:
        raise GitError("refusing to remove mismatched worktree")
    if not allow_dirty and not is_clean(path):
        raise GitError("refusing to remove dirty worktree")
    args = ["worktree", "remove"]
    if allow_dirty:
        args.append("--force")
    args.append(str(path))
    git(project, *args)


def discard_execution_reservation(project: Path, path: Path, branch: str, base_sha: str) -> None:
    branch_ref = f"refs/heads/{branch}"
    if path.exists() and any(path.iterdir()):
        remove_recorded_worktree(project, path, base_sha)
    exists = (
        subprocess.run(
            ["git", "-C", str(project), "show-ref", "--verify", "--quiet", branch_ref],
            check=False,
        ).returncode
        == 0
    )
    if exists:
        if git(project, "rev-parse", branch_ref) != base_sha:
            raise GitError("refusing to delete advanced execution branch")
        git(project, "branch", "-D", branch)


def commit_diff(project: Path, base_sha: str, target_sha: str) -> dict[str, str]:
    return {
        "commits": git(project, "log", "--oneline", f"{base_sha}..{target_sha}"),
        "diff": git(project, "diff", f"{base_sha}..{target_sha}"),
    }
