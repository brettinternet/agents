from __future__ import annotations

import json
import os
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


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ContainerRuntimeError(f"unsafe credential path: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ContainerRuntimeError(f"unsafe credential path: {path}") from exc
    try:
        os.write(descriptor, value.encode())
    finally:
        os.close(descriptor)


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
        auth = os.environ.pop("OPENCODE_AUTH_JSON", "")
        if not auth:
            raise ContainerRuntimeError("OPENCODE_AUTH_JSON is required for containerized OpenCode")
        try:
            parsed = json.loads(auth)
        except json.JSONDecodeError as exc:
            raise ContainerRuntimeError("OPENCODE_AUTH_JSON is not valid JSON") from exc
        _write_secret(home / ".local" / "share" / "opencode" / "auth.json", json.dumps(parsed, separators=(",", ":")))
    elif agent == "claude":
        token = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", "")
        if not token:
            raise ContainerRuntimeError("CLAUDE_CODE_OAUTH_TOKEN is required for containerized Claude Code")
        _write_secret(provider_dir / "claude-token", token)

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
    blocked = {"OPENCODE_AUTH_JSON", "CLAUDE_CODE_OAUTH_TOKEN"}
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
