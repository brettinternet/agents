from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import ContainerConfig
from .container_runtime import ContainerRuntime, ContainerRuntimeError


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ContainerRuntimeError(f"missing runner metadata: {name}")
    return value


def _write_secret(root: Path, relative_path: Path, value: str) -> None:
    if relative_path.is_absolute() or not relative_path.name or ".." in relative_path.parts:
        raise ContainerRuntimeError(f"unsafe credential path: {relative_path}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for part in relative_path.parent.parts:
            if part in {"", "."}:
                continue
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        for descriptor in descriptors:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise OSError("credential directory is not private")
        try:
            target_metadata = os.stat(relative_path.name, dir_fd=descriptors[-1], follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target_metadata.st_mode):
                raise OSError("credential target is a symlink")
            os.unlink(relative_path.name, dir_fd=descriptors[-1])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(relative_path.name, flags, 0o600, dir_fd=descriptors[-1])
        try:
            remaining = memoryview(value.encode())
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ContainerRuntimeError(f"unsafe credential path: {root / relative_path}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_provider_credential(provider_dir: Path) -> str:
    value = os.environ.pop("AGENTS_PROVIDER_CREDENTIAL_FILE", "")
    path = Path(value)
    expected = provider_dir / "provider-auth"
    if path != expected or path.is_symlink() or not path.is_file():
        raise ContainerRuntimeError("provider credential path is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise OSError("provider credential file is not private")
        try:
            raw = os.read(descriptor, metadata.st_size + 1)
        finally:
            os.close(descriptor)
        path.unlink()
        return raw.decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise ContainerRuntimeError("provider credential path is unsafe") from exc


def _metadata() -> tuple[ContainerRuntime, dict[str, Any]]:
    profile = _required("AGENTS_CONTAINER_PROFILE")
    runtime = ContainerRuntime(
        ContainerConfig(
            colima_profile=profile,
            image=_required("AGENTS_CONTAINER_IMAGE_ID"),
            cpus=float(_required("AGENTS_CONTAINER_CPUS")),
            memory_mb=int(_required("AGENTS_CONTAINER_MEMORY_MB")),
            pids_limit=int(_required("AGENTS_CONTAINER_PIDS")),
            gc_interval_seconds=1,
            gc_grace_seconds=1,
            build_cache_retention_hours=1,
        )
    )
    try:
        labels = json.loads(_required("AGENTS_CONTAINER_LABELS"))
        env_names = json.loads(_required("AGENTS_CONTAINER_ENV_NAMES"))
    except json.JSONDecodeError as exc:
        raise ContainerRuntimeError("malformed runner metadata") from exc
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise ContainerRuntimeError("malformed runner labels")
    if not isinstance(env_names, list) or not all(isinstance(name, str) for name in env_names):
        raise ContainerRuntimeError("malformed runner environment names")
    return runtime, {"labels": labels, "env_names": env_names}


def _path_has_symlink(path: Path) -> bool:
    return path.is_symlink() or path.parent.is_symlink()


def run(argv: tuple[str, ...] | None = None) -> int:
    arguments = tuple(sys.argv if argv is None else argv)
    agent = Path(arguments[0]).name
    if agent not in {"opencode", "claude", "mock_cli", "agents-container-runner"}:
        raise ContainerRuntimeError(f"unsupported provider wrapper: {agent}")
    if agent == "agents-container-runner":
        agent = _required("HERDR_AGENT")
    runtime, metadata = _metadata()
    name = _required("AGENTS_CONTAINER_NAME")
    image_id = _required("AGENTS_CONTAINER_IMAGE_ID")
    raw_cwd = Path(_required("AGENTS_CONTAINER_CWD"))
    raw_runtime_dir = Path(_required("AGENTS_CONTAINER_RUNTIME"))
    if (
        _path_has_symlink(raw_cwd)
        or not raw_cwd.is_dir()
        or _path_has_symlink(raw_runtime_dir)
        or not raw_runtime_dir.is_dir()
    ):
        raise ContainerRuntimeError("container bind path is unsafe")
    cwd = raw_cwd.resolve()
    runtime_dir = raw_runtime_dir.resolve()
    user = _required("AGENTS_CONTAINER_USER")

    home = runtime_dir / "home"
    provider_dir = runtime_dir / "provider"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if agent == "opencode":
        auth = _read_provider_credential(provider_dir)
        if not auth:
            raise ContainerRuntimeError("OPENCODE_AUTH_JSON is required for containerized OpenCode")
        try:
            parsed = json.loads(auth)
        except json.JSONDecodeError as exc:
            raise ContainerRuntimeError("OPENCODE_AUTH_JSON is not valid JSON") from exc
        _write_secret(home, Path(".local/share/opencode/auth.json"), json.dumps(parsed, separators=(",", ":")))
    elif agent == "claude":
        token = _read_provider_credential(provider_dir)
        if not token:
            raise ContainerRuntimeError("CLAUDE_CODE_OAUTH_TOKEN is required for containerized Claude Code")
        _write_secret(provider_dir, Path("claude-token"), token)

    docker = [
        "run",
        "--name",
        name,
        "--interactive",
        "--tty",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        user,
        "--cpus",
        _required("AGENTS_CONTAINER_CPUS"),
        "--memory",
        f"{_required('AGENTS_CONTAINER_MEMORY_MB')}m",
        "--pids-limit",
        _required("AGENTS_CONTAINER_PIDS"),
        "--network",
        _required("AGENTS_CONTAINER_NETWORK"),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev",
        "--tmpfs",
        "/run:rw,noexec,nosuid,nodev",
        "--mount",
        f"type=bind,src={cwd},dst={cwd}",
        "--mount",
        f"type=bind,src={runtime_dir},dst={runtime_dir}",
        "--workdir",
        str(cwd),
    ]
    for key, value in sorted(metadata["labels"].items()):
        docker.extend(("--label", f"{key}={value}"))
    blocked = {"AGENTS_PROVIDER_CREDENTIAL_FILE", "OPENCODE_AUTH_JSON", "CLAUDE_CODE_OAUTH_TOKEN"}
    for name_to_pass in sorted(set(metadata["env_names"]) - blocked):
        if name_to_pass in os.environ:
            docker.extend(("--env", name_to_pass))
    docker.extend(
        (
            "--env",
            f"HOME={home}",
            "--env",
            "AGENTS_SECRETS_TRANSPORT=agent-api",
            "--env",
            "AGENTS_SECRETS_CLI=/opt/agents/.venv/bin/python -m agents.secret_store",
        )
    )
    if agent == "claude":
        docker.extend(("--env", f"AGENTS_CLAUDE_TOKEN_FILE={provider_dir / 'claude-token'}"))
    command = {"opencode": "opencode", "claude": "/opt/agents/bin/claude-entrypoint", "mock_cli": "mock_cli"}[agent]
    docker.extend((image_id, command, *arguments[1:]))
    environment = runtime.docker_environment()
    for name_to_pass in metadata["env_names"]:
        if name_to_pass in os.environ and name_to_pass not in blocked:
            environment[name_to_pass] = os.environ[name_to_pass]
    return subprocess.run(("docker", *docker), env=environment, check=False).returncode


def main() -> None:
    try:
        raise SystemExit(run())
    except ContainerRuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
