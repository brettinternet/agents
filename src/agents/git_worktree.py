from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


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


def _container_mode(config: Any) -> bool:
    """Return whether the selected execution topology is per-agent containers."""
    execution = getattr(config, "execution", None)
    mode = getattr(execution, "isolation", "host")
    mode = getattr(mode, "value", mode)
    return str(mode).lower() == "container"


def _assert_nonsymlinked(path: Path, *, label: str) -> None:
    """Reject a symlink at the recorded path itself."""
    if Path(path).is_symlink():
        raise GitError(f"{label} path is a symlink")


def _ref_exists(project: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(project), "show-ref", "--verify", "--quiet", ref],
            check=False,
        ).returncode
        == 0
    )


def _ref_sha(project: Path, ref: str) -> str | None:
    if not _ref_exists(project, ref):
        return None
    return git(project, "rev-parse", "--verify", ref)


def _local_config(project: Path, key: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(project), "config", "--local", "--get", key],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode == 1:
        return None
    raise GitError((result.stderr or result.stdout).strip())


def _copy_local_identity(project: Path, clone: Path) -> None:
    for key in ("user.name", "user.email"):
        value = _local_config(project, key)
        if value is not None:
            git(clone, "config", "--local", key, value)


def _current_branch(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.strip()


def _standalone_git_dir(repo: Path) -> Path:
    _assert_nonsymlinked(repo, label="isolated repository")
    if not repo.is_dir():
        raise GitError("isolated repository is not a directory")
    dotgit = repo / ".git"
    if dotgit.is_symlink():
        raise GitError("isolated repository .git is a symlink")
    try:
        raw_git_dir = Path(git(repo, "rev-parse", "--git-dir"))
        raw_common_dir = Path(git(repo, "rev-parse", "--git-common-dir"))
    except GitError as exc:
        raise GitError("isolated path is not a standalone repository") from exc
    git_dir = (repo / raw_git_dir if not raw_git_dir.is_absolute() else raw_git_dir).resolve()
    common_dir = (repo / raw_common_dir if not raw_common_dir.is_absolute() else raw_common_dir).resolve()
    if git_dir != dotgit.resolve() or common_dir != git_dir:
        raise GitError("isolated repository is not standalone")
    if git(repo, "remote"):
        raise GitError("isolated repository has a remote")
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.is_symlink() or alternates.exists():
        raise GitError("isolated repository uses an object alternates file")
    shallow = git_dir / "shallow"
    if shallow.is_symlink() or not shallow.is_file():
        raise GitError("isolated repository is not shallow")
    return git_dir


def _initialize_standalone(
    project: Path,
    target_sha: str,
    path: Path,
    *,
    branch: str | None = None,
) -> bool:
    """Create or adopt an isolated shallow repository; return whether created."""
    _assert_nonsymlinked(path, label="isolated repository")
    restore_empty = False
    if path.exists():
        if not path.is_dir():
            raise GitError("isolated repository path is not a directory")
        if any(path.iterdir()):
            _standalone_git_dir(path)
            if head_sha(path) != target_sha:
                raise GitError("isolated repository does not match expected HEAD")
            if branch is None:
                if _current_branch(path) is not None:
                    raise GitError("isolated snapshot is not detached")
            elif _current_branch(path) != branch:
                raise GitError("isolated repository is on the wrong branch")
            return False
        restore_empty = True
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
    try:
        git(path, "init")
        _copy_local_identity(project, path)
        git(path, "fetch", "--depth=1", "--no-tags", str(project), target_sha)
        if branch is None:
            git(path, "checkout", "--detach", target_sha)
        else:
            git(path, "checkout", "-b", branch, target_sha)
        _standalone_git_dir(path)
        if head_sha(path) != target_sha:
            raise GitError("isolated repository does not match expected HEAD")
        if branch is not None and _current_branch(path) != branch:
            raise GitError("isolated repository is on the wrong branch")
    except GitError, OSError:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            if restore_empty:
                path.mkdir()
        raise
    return True


def _workspace_root(config: Any, project: Path) -> Path:
    root = getattr(config, "root", None)
    if root is None:
        project_config = getattr(config, "project", None)
        root = getattr(project_config, "root", None)
    if root is None:
        root = project.parent
    return Path(root).resolve()


def _assert_managed_workspace(config: Any, project: Path, path: Path) -> None:
    _assert_nonsymlinked(path, label="recorded workspace")
    root = _workspace_root(config, project)
    candidate = path.resolve(strict=False)
    if candidate == root or candidate == project.resolve():
        raise GitError("refusing to remove unmanaged workspace")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise GitError("refusing to remove unmanaged workspace") from exc
    raw_root = Path(getattr(config, "root", root)).absolute()
    raw_candidate = path.absolute()
    try:
        raw_candidate.relative_to(raw_root)
    except ValueError as exc:
        raise GitError("recorded workspace is outside the managed root") from exc
    current = raw_candidate
    while current != raw_root:
        if current.is_symlink():
            raise GitError("recorded workspace has a symlinked ancestor")
        current = current.parent


def reserve_execution_workspace(
    config: Any,
    project: Path,
    default_branch: str,
    item_id: str,
    number: int,
    path: Path,
) -> tuple[str, str]:
    """Reserve a host worktree or a standalone container workspace."""
    if not _container_mode(config):
        return reserve_execution(project, default_branch, item_id, number, path)

    base = branch_sha(project, default_branch)
    branch = f"agents/{item_id.lower()}/{number}"
    branch_ref = f"refs/heads/{branch}"
    _assert_managed_workspace(config, project, path)
    existing_branch_sha = _ref_sha(project, branch_ref)
    if existing_branch_sha is not None and existing_branch_sha != base:
        raise GitError("recorded branch exists at unexpected base")

    created = False
    try:
        created = _initialize_standalone(project, base, path, branch=branch)
        if existing_branch_sha is None:
            git(project, "update-ref", branch_ref, base, "")
    except Exception:
        if created and path.exists() and not path.is_symlink():
            shutil.rmtree(path)
        raise
    return base, branch


def add_agent_snapshot(config: Any, project: Path, target_sha: str, path: Path) -> None:
    """Add a host detached worktree or an isolated shallow container snapshot."""
    if not _container_mode(config):
        add_detached(project, target_sha, path)
        return
    _assert_managed_workspace(config, project, path)
    _initialize_standalone(project, target_sha, path)


def _validate_branch(branch: str) -> None:
    result = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise GitError("submitted branch is not a valid branch name")


def _delete_owned_import_ref(project: Path, ref: str, commit_sha: str) -> None:
    current = _ref_sha(project, ref)
    if current == commit_sha:
        git(project, "update-ref", "-d", ref, commit_sha)


def import_isolated_submission(
    project: Path,
    clone: Path,
    branch: str,
    base_sha: str,
    commit_sha: str,
    execution_id: int,
    revision: int,
) -> None:
    """Import exactly one clean descendant from a standalone execution clone."""
    _assert_nonsymlinked(clone, label="submission")
    if clone.resolve() == project.resolve():
        raise GitError("submission path is the authoritative repository")
    _standalone_git_dir(clone)
    _validate_branch(branch)
    if execution_id <= 0 or revision <= 0:
        raise GitError("submission import identity must be positive")
    if _current_branch(clone) != branch:
        raise GitError("submission is on the wrong branch")
    if not is_clean(clone):
        raise GitError("refusing to import dirty submission")
    if head_sha(clone) != commit_sha:
        raise GitError("submission HEAD does not match supplied commit")
    if not is_ancestor(clone, base_sha, commit_sha):
        raise GitError("submission does not descend from recorded base")

    branch_ref = f"refs/heads/{branch}"
    if _ref_sha(project, branch_ref) != base_sha:
        raise GitError("authoritative branch no longer matches recorded base")

    import_ref = f"refs/agents/import/{execution_id}/{revision}"
    operation_error: GitError | None = None
    cleanup_error: GitError | None = None
    try:
        git(project, "fetch", "--no-tags", str(clone), f"{commit_sha}:{import_ref}")
        if _ref_sha(project, import_ref) != commit_sha:
            raise GitError("imported object does not match submitted commit")
        if not is_ancestor(project, base_sha, commit_sha):
            raise GitError("submitted commit is not an authoritative descendant")
        git(project, "update-ref", branch_ref, commit_sha, base_sha)
    except GitError as exc:
        operation_error = exc
    finally:
        try:
            _delete_owned_import_ref(project, import_ref, commit_sha)
        except GitError as exc:
            cleanup_error = exc
    if operation_error is not None:
        raise operation_error
    if cleanup_error is not None:
        raise cleanup_error


def remove_recorded_workspace(
    config: Any,
    project: Path,
    path: Path,
    target_sha: str,
    *,
    allow_dirty: bool = False,
) -> None:
    """Remove a host worktree or an identity-checked isolated workspace."""
    if not _container_mode(config):
        remove_recorded_worktree(project, path, target_sha, allow_dirty=allow_dirty)
        return
    if not path.exists():
        if path.is_symlink():
            raise GitError("refusing to remove symlinked workspace")
        return
    _assert_managed_workspace(config, project, path)
    _standalone_git_dir(path)
    if head_sha(path) != target_sha:
        raise GitError("refusing to remove mismatched workspace")
    if not allow_dirty and not is_clean(path):
        raise GitError("refusing to remove dirty workspace")
    if not path.is_dir():
        raise GitError("refusing to remove non-directory workspace")
    shutil.rmtree(path)
