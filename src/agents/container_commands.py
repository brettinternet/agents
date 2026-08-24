from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .auth import derive_agent_token, read_agent_auth_key, token_digest
from .config import AgentsConfig
from .container_runtime import ContainerGarbageCollector, ContainerRuntime, ContainerRuntimeError, _completed
from .db import connect, migrate, utc_now
from .git_worktree import identity as git_identity


class ContainerCommandError(RuntimeError):
    pass


def _instance(config: AgentsConfig) -> str:
    if not config.db_path.is_file():
        raise ContainerCommandError("Agents must be initialized before container commands are used")
    connection = sqlite3.connect(config.db_path)
    try:
        row = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
    finally:
        connection.close()
    if row is None or not row[0]:
        raise ContainerCommandError("Agents project identity is unavailable")
    return str(row[0])


def _runtime(config: AgentsConfig) -> ContainerRuntime:
    if config.execution.container is None:
        raise ContainerCommandError("[execution.container] is required")
    return ContainerRuntime(config.execution.container)


def _mount_root(config: AgentsConfig) -> Path:
    return config.root.resolve()


def runtime_init(config: AgentsConfig) -> None:
    _runtime(config).initialize(_mount_root(config), _instance(config), config.web.port)


def _provider(config: AgentsConfig) -> str:
    provider = config.execution.provider
    if provider not in {"opencode", "claude", "mock"}:
        raise ContainerCommandError(f"unsupported container provider: {provider}")
    return provider


def build(config: AgentsConfig) -> None:
    runtime_init(config)
    runtime = _runtime(config)
    provider = _provider(config)
    uid, gid = os.getuid(), os.getgid()
    runtime.docker(
        "build",
        "--target",
        f"agent-{provider}",
        "--build-arg",
        f"AGENTS_UID={uid}",
        "--build-arg",
        f"AGENTS_GID={gid}",
        "--tag",
        f"agents-agent-{provider}:local",
        str(config.root),
    )
    runtime.docker(
        "build",
        "--target",
        f"system-{provider}",
        "--build-arg",
        f"AGENTS_UID={uid}",
        "--build-arg",
        f"AGENTS_GID={gid}",
        "--tag",
        f"agents-system-{provider}:local",
        str(config.root),
    )
    runtime.docker(
        "build",
        "--target",
        "secrets",
        "--tag",
        "agents-secrets:local",
        str(config.root),
    )


def _secret_source_environment(config: AgentsConfig, provider: str, auth_file: Path) -> dict[str, str]:
    broker_config = auth_file.parent / f"{auth_file.name}-broker.toml"
    if broker_config.is_symlink():
        raise ContainerCommandError("broker config path is unsafe")
    if not broker_config.exists():
        broker_config.write_text(config.source.read_text(encoding="utf-8"), encoding="utf-8")
        broker_config.chmod(0o600)
    broker_local = auth_file.parent / f"{auth_file.name}-broker.env"
    if broker_local.is_symlink():
        raise ContainerCommandError("broker environment path is unsafe")
    if not broker_local.exists():
        broker_local.write_text("", encoding="utf-8")
        broker_local.chmod(0o600)
    common = {
        "AGENTS_BROKER_CONFIG_PATH": str(broker_config),
        "AGENTS_ENV_SCHEMA_PATH": str(config.root / ".env.schema"),
        "AGENTS_ENV_LOCAL_PATH": str(broker_local),
        "AGENTS_SOPS_CONFIG_PATH": str(config.root / ".sops.yaml"),
    }
    if provider != "mock":
        return {
            **common,
            "AGENTS_AGE_KEY_PATH": str(config.root / ".env.sops-age"),
            "AGENTS_SOPS_HOME_PATH": str(config.root / ".sops-isolated-home"),
            "AGENTS_SECRET_STORE_PATH": str(config.root / "agent-secrets.sops.json"),
        }
    root = auth_file.parent / f"{auth_file.name}-broker"
    if root.is_symlink():
        raise ContainerCommandError("mock broker source path is unsafe")
    root.mkdir(mode=0o700, exist_ok=True)
    worktree = root / "worktree"
    worktree.mkdir(mode=0o700, exist_ok=True)
    schema = worktree / ".env.schema"
    schema.write_text(
        "# @defaultSensitive=false @defaultRequired=false\n# ---\n# @sensitive\nTEST_SECRET=\n",
        encoding="utf-8",
    )
    local = worktree / ".env.local"
    local.write_text("", encoding="utf-8")
    local.chmod(0o600)
    from .secret_store import Paths, init_store, set_secret_value

    paths = Paths(
        worktree=worktree,
        common_root=root,
        config=root / "sops-config",
        store=root / "store",
        key=root / "age-key",
        isolated_home=root / "sops-home",
        lock=root / "lock",
    )
    if not paths.store.exists():
        init_store(paths)
        set_secret_value(paths, "TEST_SECRET", b"smoke-only-secret")
    return {
        **common,
        "AGENTS_ENV_SCHEMA_PATH": str(schema),
        "AGENTS_SOPS_CONFIG_PATH": str(paths.config),
        "AGENTS_AGE_KEY_PATH": str(paths.key),
        "AGENTS_SOPS_HOME_PATH": str(paths.isolated_home),
        "AGENTS_SECRET_STORE_PATH": str(paths.store),
    }


def _cleanup_secret_source_artifacts(auth_file: Path) -> None:
    for suffix in ("-broker.toml", "-broker.env"):
        candidate = auth_file.parent / f"{auth_file.name}{suffix}"
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise ContainerCommandError("broker credential artifact path is unsafe")
        candidate.unlink(missing_ok=True)
    root = auth_file.parent / f"{auth_file.name}-broker"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ContainerCommandError("broker credential artifact path is unsafe")
    if root.is_dir():
        shutil.rmtree(root)


def _system_auth_directory(config: AgentsConfig, *, create: bool = False) -> Path:
    runtime = config.state_dir / "runtime"
    directory = runtime / "system-auth"
    for candidate in (config.state_dir, runtime, directory):
        if create and not candidate.exists() and not candidate.is_symlink():
            candidate.mkdir(mode=0o700)
        if not candidate.is_dir() or candidate.is_symlink():
            raise ContainerCommandError("whole-system credential directory is unsafe")
        metadata = candidate.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ContainerCommandError("whole-system credential directory is unsafe")
    return directory


def _topology_owner_alive(owner_pid: int, owner_started: str) -> bool:
    from . import service

    try:
        os.kill(owner_pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise ContainerCommandError("cannot establish whole-system topology owner liveness") from exc
    try:
        return service._process_started(owner_pid) == owner_started
    except service.ServiceError as exc:
        raise ContainerCommandError("cannot establish whole-system topology owner identity") from exc


def _compose_environment(config: AgentsConfig, topology_id: str, auth_file: Path) -> dict[str, str]:
    instance = _instance(config)
    provider = _provider(config)
    repository = config.root.resolve()
    common_dir = git_identity(config.project.path)[1].resolve()
    try:
        common_dir.relative_to(repository)
    except ValueError as exc:
        raise ContainerCommandError(
            "whole-system Compose requires a standalone repository whose Git common directory is inside its root"
        ) from exc
    environment = {
        **_runtime(config).docker_environment(),
        "AGENTS_INSTANCE_ID": instance,
        "AGENTS_TOPOLOGY_ID": topology_id,
        "AGENTS_REPO_PATH": str(repository),
        "AGENTS_UID": str(os.getuid()),
        "AGENTS_GID": str(os.getgid()),
        "AGENTS_PROVIDER": provider,
        "AGENTS_WEB_PORT": str(config.web.port),
        "AGENTS_SYSTEM_IMAGE": f"agents-system-{provider}:local",
        "AGENTS_SECRETS_IMAGE": "agents-secrets:local",
        "AGENTS_PROVIDER_AUTH_FILE": str(auth_file.resolve()),
        **_secret_source_environment(config, provider, auth_file),
    }
    environment.pop("OPENCODE_AUTH_JSON", None)
    environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return environment


def _auth_value(config: AgentsConfig) -> bytes:
    provider = _provider(config)
    if provider == "mock":
        return b"{}"
    name = "OPENCODE_AUTH_JSON" if provider == "opencode" else "CLAUDE_CODE_OAUTH_TOKEN"
    value = os.environ.get(name)
    if not value:
        raise ContainerCommandError(f"{name} is required for the selected whole-system provider")
    if name == "OPENCODE_AUTH_JSON":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContainerCommandError("OPENCODE_AUTH_JSON must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ContainerCommandError("OPENCODE_AUTH_JSON must contain a JSON object")
    return value.encode()


def _topology_record(config: AgentsConfig) -> Path:
    return config.state_dir / "container-topology.json"


def _write_topology_record(path: Path, value: dict[str, object], *, replace: bool = False) -> None:
    data = json.dumps(value).encode()
    if not replace:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _verify_compose_project_scope(config: AgentsConfig, topology_id: str) -> None:
    runtime = _runtime(config)
    instance = _instance(config)
    project = f"agents-{instance}"
    names = runtime.docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        "{{.Names}}",
    )
    for name in names.splitlines():
        inspect = runtime.inspect_container(name)
        labels = inspect.get("Config", {}).get("Labels", {}) if inspect is not None else {}
        if labels.get("dev.agents.instance") != instance or labels.get("dev.agents.topology") != topology_id:
            raise ContainerCommandError("Compose project contains a container from a different topology")


def _recover_dead_topology(config: AgentsConfig) -> None:
    record = _topology_record(config)
    if record.exists() or record.is_symlink():
        if not record.is_file() or record.is_symlink():
            raise ContainerCommandError("whole-system topology ownership record is unsafe")
        try:
            value = json.loads(record.read_text())
            topology_id = str(value["topology_id"])
            auth_file = Path(str(value["auth_file"]))
            owner_pid = int(value["owner_pid"])
            owner_started = str(value["owner_started"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ContainerCommandError("whole-system topology ownership record is malformed") from exc
        if _topology_owner_alive(owner_pid, owner_started):
            raise ContainerCommandError("whole-system topology owner is still running")
        runtime = _runtime(config)
        names = runtime.docker(
            "container",
            "ls",
            "--all",
            "--filter",
            f"label=dev.agents.instance={_instance(config)}",
            "--filter",
            f"label=dev.agents.topology={topology_id}",
            "--format",
            "{{.Names}}",
        )
        for name in names.splitlines():
            inspect = runtime.inspect_container(name)
            labels = inspect.get("Config", {}).get("Labels") if inspect is not None else None
            if (
                not isinstance(labels, dict)
                or labels.get("dev.agents.instance") != _instance(config)
                or labels.get("dev.agents.topology") != topology_id
            ):
                raise ContainerCommandError("whole-system topology container identity is unsafe")
        expected = _system_auth_directory(config)
        if auth_file.is_symlink() or auth_file.parent.resolve() != expected.resolve():
            raise ContainerCommandError("whole-system credential path is unsafe")
        _verify_compose_project_scope(config, topology_id)
        _completed(
            ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--remove-orphans"),
            env=_compose_environment(config, topology_id, auth_file),
        )
        _cleanup_secret_source_artifacts(auth_file)
        auth_file.unlink(missing_ok=True)
        record.unlink()
    else:
        runtime = _runtime(config)
        remnants = runtime.docker(
            "container",
            "ls",
            "--all",
            "--filter",
            f"label=dev.agents.instance={_instance(config)}",
            "--filter",
            "label=dev.agents.topology",
            "--format",
            "{{.Names}}",
        )
        if remnants:
            raise ContainerCommandError("whole-system containers exist without an ownership record")

    directory = config.state_dir / "runtime" / "system-auth"
    if not directory.exists() and not directory.is_symlink():
        return
    if not directory.is_dir() or directory.is_symlink():
        raise ContainerCommandError("whole-system credential directory is unsafe")
    directory_metadata = directory.stat()
    if directory_metadata.st_uid != os.getuid() or directory_metadata.st_mode & 0o077:
        raise ContainerCommandError("whole-system credential directory is unsafe")
    candidates = [
        candidate
        for candidate in directory.iterdir()
        if candidate.is_file()
        and not candidate.is_symlink()
        and len(candidate.name) == 32
        and all(character in "0123456789abcdef" for character in candidate.name)
    ]
    if candidates:
        raise ContainerCommandError("whole-system credential files exist without provably dead ownership records")
    orphaned_topologies: set[str] = set()
    for candidate in directory.iterdir():
        for suffix in ("-broker.toml", "-broker.env", "-broker"):
            if candidate.name.endswith(suffix):
                topology_id = candidate.name[: -len(suffix)]
                if len(topology_id) == 32 and all(character in "0123456789abcdef" for character in topology_id):
                    orphaned_topologies.add(topology_id)
                break
    for topology_id in orphaned_topologies:
        _cleanup_secret_source_artifacts(directory / topology_id)


def _web_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start(config: AgentsConfig) -> None:
    from . import service

    owned = service.status(config)
    if any(owned.values()):
        raise ContainerCommandError("host Agents/Herdr services are already owned; stop them before container:start")
    _recover_dead_topology(config)
    record = _topology_record(config)
    if not _web_port_available(config.web.port):
        raise ContainerCommandError(f"web port 127.0.0.1:{config.web.port} is already owned by another process")
    janitor_record = config.state_dir / "container-janitor.pid"
    if janitor_record.exists() or janitor_record.is_symlink():
        if service._owned(janitor_record) is not None:
            raise ContainerCommandError("whole-system janitor is already running")
        janitor_record.unlink()
    runtime_init(config)
    runtime = _runtime(config)
    instance = _instance(config)
    live_owned = runtime.docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"label=dev.agents.instance={instance}",
        "--format",
        "{{json .}}",
    )
    for line in live_owned.splitlines():
        try:
            item = json.loads(line)
            inspect = runtime.inspect_container(str(item["Names"]))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ContainerCommandError("Docker returned malformed owned container identity") from exc
        labels = inspect.get("Config", {}).get("Labels", {}) if inspect is not None else {}
        if (
            isinstance(labels, dict)
            and labels.get("dev.agents.instance") == instance
            and labels.get("dev.agents.execution")
            and not labels.get("dev.agents.topology")
        ):
            raise ContainerCommandError(
                "a per-agent container is still running; stop host execution before container:start"
            )
    container = config.execution.container
    if container is None:
        raise ContainerCommandError("container configuration is required")
    runtime.resolve_image_id(container.image)
    system_image_id = runtime.resolve_image_id(f"agents-system-{_provider(config)}:local")
    secrets_image_id = runtime.resolve_image_id("agents-secrets:local")
    topology_id = uuid.uuid4().hex
    directory = _system_auth_directory(config, create=True)
    auth_file = directory / topology_id
    auth_value = _auth_value(config)
    descriptor = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, auth_value)
    finally:
        os.close(descriptor)
    try:
        environment = _compose_environment(config, topology_id, auth_file)
        _write_topology_record(
            record,
            {
                "topology_id": topology_id,
                "auth_file": str(auth_file),
                "system_image_id": system_image_id,
                "secrets_image_id": secrets_image_id,
                "owner_pid": os.getpid(),
                "owner_started": service._process_started(os.getpid()),
            },
        )
    except BaseException:
        auth_file.unlink(missing_ok=True)
        raise
    janitor_process = None
    try:
        _completed(
            ("docker", "compose", "-f", str(config.root / "compose.yaml"), "up", "--detach", "--wait"),
            env=environment,
        )
        _verify_system_topology(config, exercise_janitor=False)
        executable = config.root / ".venv" / "bin" / "python"
        janitor_process = service._launch_process(
            config,
            "container-janitor",
            executable,
            ["-m", "agents.cli", "container", "janitor"],
            environment,
        )
        _write_topology_record(
            record,
            {
                "topology_id": topology_id,
                "auth_file": str(auth_file),
                "system_image_id": system_image_id,
                "secrets_image_id": secrets_image_id,
                "owner_pid": janitor_process.pid,
                "owner_started": service._process_started(janitor_process.pid),
            },
            replace=True,
        )
    except BaseException:
        try:
            logs = _completed(
                (
                    "docker",
                    "compose",
                    "-f",
                    str(config.root / "compose.yaml"),
                    "logs",
                    "--no-color",
                    "--tail",
                    "100",
                ),
                env=environment,
            ).stdout
        except ContainerRuntimeError:
            logs = ""
        if logs:
            print(logs, file=sys.stderr)
        if janitor_process is not None:
            service._stop_started_process(config, "container-janitor", janitor_process)
        with contextlib.suppress(ContainerRuntimeError):
            _verify_compose_project_scope(config, topology_id)
            _completed(
                ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--remove-orphans"),
                env=environment,
            )
        record.unlink(missing_ok=True)
        _cleanup_secret_source_artifacts(auth_file)
        auth_file.unlink(missing_ok=True)
        raise


def stop(config: AgentsConfig) -> None:
    from . import service

    record = _topology_record(config)
    if not record.is_file() or record.is_symlink():
        raise ContainerCommandError("whole-system topology ownership record is absent")
    try:
        value = json.loads(record.read_text())
        topology_id = str(value["topology_id"])
        auth_file = Path(str(value["auth_file"]))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ContainerCommandError("whole-system topology ownership record is malformed") from exc
    expected = _system_auth_directory(config)
    if auth_file.is_symlink() or auth_file.parent.resolve() != expected.resolve():
        raise ContainerCommandError("whole-system credential path is unsafe")
    service._stop_named(config, "container-janitor")
    _verify_compose_project_scope(config, topology_id)
    _completed(
        ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--remove-orphans"),
        env=_compose_environment(config, topology_id, auth_file),
    )
    _cleanup_secret_source_artifacts(auth_file)
    auth_file.unlink(missing_ok=True)
    record.unlink(missing_ok=True)
    deadline = time.monotonic() + 10
    while not _web_port_available(config.web.port):
        if time.monotonic() >= deadline:
            raise ContainerCommandError(
                f"web port 127.0.0.1:{config.web.port} remained occupied after whole-system shutdown"
            )
        time.sleep(0.1)


def status(config: AgentsConfig) -> str:
    record = _topology_record(config)
    if not record.is_file() or record.is_symlink():
        return "stopped"
    value = json.loads(record.read_text())
    environment = _compose_environment(config, str(value["topology_id"]), Path(str(value["auth_file"])))
    return _completed(
        ("docker", "compose", "-f", str(config.root / "compose.yaml"), "ps", "--format", "json"),
        env=environment,
    ).stdout.strip()


def gc(config: AgentsConfig) -> dict[str, object]:
    connection = connect(config.db_path)
    try:
        migrate(connection)
        result = ContainerGarbageCollector(config, connection).collect()
        for message in result.get("cleanup_errors", []):
            now = utc_now()
            connection.execute(
                "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,"
                "created_at,updated_at) VALUES('container_gc_refused','container','gc','high','open',?,'{}',?,?) "
                "ON CONFLICT(kind,entity_kind,entity_id) WHERE state='open' "
                "DO UPDATE SET summary=excluded.summary,updated_at=excluded.updated_at",
                (str(message), now, now),
            )
        connection.commit()
        return result
    finally:
        connection.close()


def _remove_stopped_topology_containers(config: AgentsConfig) -> list[str]:
    record = _topology_record(config)
    if not record.is_file() or record.is_symlink():
        return []
    try:
        topology_id = str(json.loads(record.read_text())["topology_id"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ContainerCommandError("whole-system topology ownership record is malformed") from exc
    runtime = _runtime(config)
    instance = _instance(config)
    output = runtime.docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"label=dev.agents.instance={instance}",
        "--filter",
        f"label=dev.agents.topology={topology_id}",
        "--filter",
        f"label=com.docker.compose.project=agents-{instance}",
        "--format",
        "{{.Names}}",
    )
    removed: list[str] = []
    for name in output.splitlines():
        inspect = runtime.inspect_container(name)
        if inspect is None:
            continue
        labels = inspect.get("Config", {}).get("Labels", {})
        state = inspect.get("State", {})
        if (
            labels.get("dev.agents.instance") == instance
            and labels.get("dev.agents.topology") == topology_id
            and labels.get("com.docker.compose.project") == f"agents-{instance}"
            and labels.get("dev.agents.retention") == "ephemeral"
            and not bool(state.get("Running"))
        ):
            container_id = inspect.get("Id")
            if not isinstance(container_id, str) or not container_id:
                raise ContainerCommandError(f"whole-system container {name!r} has no immutable identity")
            runtime.remove_container(name, container_id)
            removed.append(name)
    return removed


def janitor(config: AgentsConfig) -> None:
    if config.execution.container is None:
        raise ContainerCommandError("[execution.container] is required")
    while True:
        try:
            result = gc(config)
            _remove_stopped_topology_containers(config)
            if result.get("trim_error"):
                print(result["trim_error"], flush=True)
        except (ContainerRuntimeError, OSError, sqlite3.Error, ValueError) as exc:
            print(f"container janitor: {exc}", flush=True)
        time.sleep(config.execution.container.gc_interval_seconds)


def smoke(config: AgentsConfig, topology: str = "system") -> None:
    if topology == "agent":
        runtime_init(config)
        runtime = _runtime(config)
        if config.execution.container is None:
            raise ContainerCommandError("[execution.container] is required")
        image_id = runtime.resolve_image_id(config.execution.container.image)
        runtime.docker(
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev",
            "--entrypoint",
            "/bin/true",
            image_id,
        )
        environment = dict(os.environ)
        environment["AGENTS_SMOKE_API_PORT"] = str(config.web.port)
        _completed(
            (
                sys.executable,
                "-m",
                "tests.smoke_e2e",
                "--backend",
                "herdr",
                "--isolation",
                "container",
                "--instance",
                _instance(config),
            ),
            env=environment,
        )
        return

    runtime_init(config)
    if status(config) == "stopped":
        raise ContainerCommandError("whole-system topology is not running")
    instance = _instance(config)
    before_project = instance
    before_volume = _verify_system_topology(config)
    _verify_system_http(config)
    runtime = _runtime(config)
    agents_name = runtime.docker(
        "container",
        "ls",
        "--filter",
        f"label=dev.agents.instance={instance}",
        "--filter",
        "label=com.docker.compose.service=agents",
        "--format",
        "{{.Names}}",
    )
    if len(agents_name.splitlines()) != 1:
        raise ContainerCommandError("whole-system Agents service identity is ambiguous")
    runtime.docker(
        "exec",
        "--env",
        f"PYTHONPATH={config.root / 'src'}",
        agents_name,
        "/opt/agents/.venv/bin/python",
        "-m",
        "tests.smoke_e2e",
        "--backend",
        "herdr",
        "--isolation",
        "host",
    )
    _verify_system_secret_roundtrip(config, runtime, agents_name)
    stop(config)
    if _instance(config) != before_project:
        raise ContainerCommandError("whole-system SQLite project identity changed while stopped")
    start(config)
    if _verify_system_topology(config) != before_volume:
        raise ContainerCommandError("whole-system persistent Herdr volume changed across restart")
    _verify_system_http(config)


def _verify_system_secret_roundtrip(config: AgentsConfig, runtime: ContainerRuntime, agents_name: str) -> None:
    connection = connect(config.db_path)
    run_id: int | None = None
    try:
        actor = connection.execute("SELECT slug FROM actors WHERE kind='agent' ORDER BY slug LIMIT 1").fetchone()
        project = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        if actor is None or project is None:
            raise ContainerCommandError("whole-system secret smoke has no initialized agent identity")
        now = utc_now()
        purpose_id = f"system-secret-smoke-{uuid.uuid4().hex}"
        cursor = connection.execute(
            "INSERT INTO terminal_runs("
            "execution_name,execution_backend,profile_name,mcp_name,profile_sha256,provider,model,"
            "generation,actor_slug,purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,"
            "output_tail,launch_count,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',0,?,?)",
            (
                "reserved",
                "herdr",
                "reserved",
                "reserved",
                "",
                config.execution.provider_id,
                "",
                1,
                str(actor["slug"]),
                "work",
                purpose_id,
                str(config.root),
                "",
                "installed",
                "live",
                now,
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise ContainerCommandError("whole-system secret smoke could not reserve an execution")
        run_id = int(cursor.lastrowid)
        execution_id = f"agents-{str(project['instance_id'])[:8]}-system-secret-smoke-{run_id}"
        token = derive_agent_token(
            read_agent_auth_key(config.state_dir / "agent-auth-key"),
            str(project["instance_id"]),
            run_id,
            1,
        )
        connection.execute(
            "UPDATE terminal_runs SET execution_name=?,profile_name=?,mcp_name=?,agent_auth_id=?,token_digest=? "
            "WHERE id=?",
            (execution_id, execution_id, execution_id, execution_id, token_digest(token), run_id),
        )
        connection.execute(
            "INSERT INTO actor_leases(actor_slug,purpose_kind,purpose_id,terminal_run_id,acquired_at)VALUES(?,?,?,?,?)",
            (str(actor["slug"]), "work", purpose_id, run_id, now),
        )
        connection.commit()
        command_environment = runtime.docker_environment()
        command_environment.update(
            {
                "AGENTS_AGENT_TOKEN": token,
                "AGENTS_EXECUTION_ID": execution_id,
            }
        )
        result = _completed(
            (
                "docker",
                "exec",
                "--env",
                "AGENTS_AGENT_TOKEN",
                "--env",
                "AGENTS_EXECUTION_ID",
                agents_name,
                "task",
                "--taskfile",
                "/opt/agents/Taskfile.dist.yaml",
                "secrets:run",
                "--",
                "TEST_SECRET",
                "--",
                "/opt/agents/.venv/bin/python",
                "-c",
                "import os,sys;paths=('/run/agents-secrets/age-key','/run/agents-secrets/sops-home','/run/agents-secrets/store','/run/agents-state/source/agent-auth-key','/run/agents-state/source/agents.db');ok=os.environ.get('TEST_SECRET')=='smoke-only-secret' and all(not os.access(path,os.R_OK) for path in paths);sys.stdout.write('system-secret-ok' if ok else 'bad');raise SystemExit(0 if ok else 1)",
            ),
            env=command_environment,
        )
        if result.stdout.strip() != "system-secret-ok":
            raise ContainerCommandError("whole-system secret broker returned an unexpected child result")
    finally:
        if run_id is not None:
            connection.execute("DELETE FROM actor_leases WHERE terminal_run_id=?", (run_id,))
            connection.execute("DELETE FROM launch_attempts WHERE terminal_run_id=?", (run_id,))
            connection.execute("DELETE FROM terminal_runs WHERE id=?", (run_id,))
            connection.commit()
        connection.close()


def _verify_system_http(config: AgentsConfig) -> None:
    deadline = time.monotonic() + 30
    base_url = f"http://127.0.0.1:{config.web.port}"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=base_url, timeout=2.0) as client:
                health = client.get("/health")
                if health.status_code < 300:
                    token = (config.state_dir / "web-token").read_text(encoding="utf-8").strip()
                    login = client.post(
                        "/auth/login",
                        data={"token": token},
                        headers={"Origin": base_url},
                        follow_redirects=False,
                    )
                    dashboard = client.get("/")
                    if login.status_code == 303 and dashboard.status_code == 200:
                        return
        except httpx.HTTPError, OSError:
            pass
        time.sleep(0.25)
    raise ContainerCommandError("whole-system authenticated dashboard did not become ready")


def _verify_system_topology(config: AgentsConfig, *, exercise_janitor: bool = True) -> str:
    runtime = _runtime(config)
    instance = _instance(config)
    record = _topology_record(config)
    if not record.is_file() or record.is_symlink():
        raise ContainerCommandError("whole-system topology ownership record is absent")
    try:
        record_value = json.loads(record.read_text())
        topology_id = str(record_value["topology_id"])
        auth_file = Path(str(record_value["auth_file"]))
        system_image_id = str(record_value["system_image_id"])
        secrets_image_id = str(record_value["secrets_image_id"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ContainerCommandError("whole-system topology ownership record is malformed") from exc
    system_auth = _system_auth_directory(config)
    if auth_file.is_symlink() or auth_file.parent.resolve() != system_auth.resolve():
        raise ContainerCommandError("whole-system credential path is unsafe")
    names = runtime.docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"label=dev.agents.instance={instance}",
        "--format",
        "{{.Names}}",
    )
    services: dict[str, dict[str, Any]] = {}
    service_names: dict[str, str] = {}
    for name in names.splitlines():
        inspect = runtime.inspect_container(name)
        if inspect is None:
            raise ContainerCommandError(f"whole-system container disappeared during verification: {name}")
        labels = inspect.get("Config", {}).get("Labels", {})
        service_name = labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        if not isinstance(service_name, str):
            raise ContainerCommandError(f"whole-system topology has non-Compose container {name!r}")
        if service_name not in {"agents", "secrets", "herdr-init"}:
            raise ContainerCommandError(f"whole-system topology has unexpected service {service_name!r}")
        if labels.get("com.docker.compose.project") != f"agents-{instance}":
            raise ContainerCommandError(f"whole-system {service_name} service has mismatched Compose project")
        expected_image = (
            system_image_id
            if service_name in {"agents", "herdr-init"}
            else secrets_image_id
            if service_name == "secrets"
            else None
        )
        if expected_image is not None and (
            not expected_image.startswith("sha256:") or inspect.get("Image") != expected_image
        ):
            raise ContainerCommandError(f"whole-system {service_name} service has mismatched image identity")
        if labels.get("dev.agents.topology") != topology_id:
            raise ContainerCommandError(f"whole-system {service_name} service has mismatched topology identity")
        if service_name in services:
            raise ContainerCommandError(f"whole-system {service_name} service identity is duplicated")
        services[service_name] = inspect
        service_names[service_name] = name
    for service_name in ("agents", "secrets"):
        inspect = services.get(service_name)
        if inspect is None:
            raise ContainerCommandError(f"whole-system {service_name} service is absent")
        state = inspect.get("State", {})
        health = state.get("Health", {}) if isinstance(state, dict) else {}
        if not state.get("Running") or not isinstance(health, dict) or health.get("Status") != "healthy":
            raise ContainerCommandError(f"whole-system {service_name} service is not healthy")
        if "docker.sock" in json.dumps(inspect.get("Mounts", [])):
            raise ContainerCommandError(f"whole-system {service_name} service exposes a Docker socket")
    agents = services["agents"]
    agents_network = agents.get("NetworkSettings", {}).get("Networks", {}).get("agents-system", {})
    port_key = f"{config.web.port}/tcp"
    expected_ports = {port_key: [{"HostIp": "127.0.0.1", "HostPort": str(config.web.port)}]}
    agents_ports = agents.get("HostConfig", {}).get("PortBindings", {})
    live_ports = agents.get("NetworkSettings", {}).get("Ports", {})
    if (
        agents_network.get("IPAddress") != "172.30.1.2"
        or agents_ports != expected_ports
        or live_ports != expected_ports
    ):
        raise ContainerCommandError("whole-system Agents address or published-port boundary is incorrect")
    agent_mounts = {
        str(mount.get("Destination")): (str(mount.get("Type")), str(mount.get("Source")))
        for mount in agents.get("Mounts", [])
        if isinstance(mount, dict)
    }
    agent_tmpfs = agents.get("HostConfig", {}).get("Tmpfs", {})
    for masked in (
        config.root / ".env.local",
        config.root / ".env.sops-age",
        config.root / "agent-secrets.sops.json",
    ):
        if agent_mounts.get(str(masked)) != ("bind", "/dev/null"):
            raise ContainerCommandError(f"whole-system Agents service exposes secret identity path {masked}")
    for private_tmpfs in (
        config.root / ".sops-isolated-home",
        config.state_dir / "runtime" / "system-auth",
        Path("/home/agents/.local/share/opencode"),
        Path("/home/agents/bin"),
    ):
        if str(private_tmpfs) not in agent_tmpfs and agent_mounts.get(str(private_tmpfs), ("", ""))[0] != "tmpfs":
            raise ContainerCommandError(f"whole-system private path is not tmpfs-backed: {private_tmpfs}")
    secrets = services["secrets"]
    secrets_network = secrets.get("NetworkSettings", {}).get("Networks", {}).get("agents-system", {})
    if secrets_network.get("IPAddress") != "172.30.1.3" or secrets.get("HostConfig", {}).get("PortBindings"):
        raise ContainerCommandError("whole-system secret broker address or publication boundary is incorrect")
    secret_mounts = {
        str(mount.get("Destination")): (
            str(mount.get("Type")),
            str(mount.get("Source")),
            bool(mount.get("RW")),
        )
        for mount in secrets.get("Mounts", [])
        if isinstance(mount, dict)
    }
    secret_tmpfs = secrets.get("HostConfig", {}).get("Tmpfs", {})
    control_plane = str(config.root / ".agents")
    if control_plane not in secret_tmpfs and secret_mounts.get(control_plane, ("", "", False))[0] != "tmpfs":
        raise ContainerCommandError("whole-system secret broker exposes the repository control plane")
    for masked in (
        config.root / ".env.local",
        config.root / ".env.sops-age",
        config.root / "agent-secrets.sops.json",
    ):
        if secret_mounts.get(str(masked)) != ("bind", "/dev/null", False):
            raise ContainerCommandError(f"whole-system secret broker exposes secret identity path {masked}")
    broker_root = auth_file.parent / f"{auth_file.name}-broker"
    if config.execution.provider == "mock":
        schema_source = broker_root / "worktree" / ".env.schema"
        sops_config_source = broker_root / "sops-config"
        age_key_source = broker_root / "age-key"
        sops_home_source = broker_root / "sops-home"
        store_source = broker_root / "store"
    else:
        schema_source = config.root / ".env.schema"
        sops_config_source = config.root / ".sops.yaml"
        age_key_source = config.root / ".env.sops-age"
        sops_home_source = config.root / ".sops-isolated-home"
        store_source = config.root / "agent-secrets.sops.json"
    expected_secret_mounts = {
        "/run/agents.toml": (auth_file.parent / f"{auth_file.name}-broker.toml", False),
        "/run/agents-secrets/.env.schema": (schema_source, False),
        "/run/agents-secrets/.env.local": (auth_file.parent / f"{auth_file.name}-broker.env", False),
        "/run/agents-secrets/sops-config": (sops_config_source, False),
        "/run/agents-secrets/age-key": (age_key_source, False),
        "/run/agents-secrets/sops-home": (sops_home_source, False),
        "/run/agents-secrets/store": (store_source, True),
        "/run/agents-state/source": (config.root / ".agents", False),
    }
    for destination, (source, writable) in expected_secret_mounts.items():
        if secret_mounts.get(destination) != ("bind", str(source), writable):
            raise ContainerCommandError(f"whole-system secret broker mount is incorrect: {destination}")
    runtime.docker(
        "exec",
        service_names["agents"],
        "/opt/agents/.venv/bin/python",
        "-c",
        "import urllib.request;urllib.request.urlopen('http://172.30.1.3:9891/health',timeout=2)",
    )
    volumes = runtime.docker(
        "volume",
        "ls",
        "--filter",
        f"label=dev.agents.instance={instance}",
        "--filter",
        "label=dev.agents.retention=persistent",
        "--format",
        "{{.Name}}",
    ).splitlines()
    if len(volumes) != 1:
        raise ContainerCommandError("whole-system persistent Herdr volume identity is ambiguous")
    if exercise_janitor:
        before = {
            service_names[service_name]
            for service_name, inspect in services.items()
            if inspect.get("State", {}).get("Running")
        }
        _remove_stopped_topology_containers(config)
        after = set(
            runtime.docker(
                "container",
                "ls",
                "--all",
                "--filter",
                f"label=dev.agents.instance={instance}",
                "--format",
                "{{.Names}}",
            ).splitlines()
        )
        if not before.issubset(after):
            raise ContainerCommandError("whole-system janitor removed an active service")
    return volumes[0]


def reset(config: AgentsConfig) -> None:
    from . import service

    topology_record = _topology_record(config)
    if topology_record.exists() or topology_record.is_symlink() or any(service.status(config).values()):
        raise ContainerCommandError("all Agents topologies must be stopped before container:reset")
    runtime = _runtime(config)
    environment = _compose_environment(
        config,
        "reset",
        config.state_dir / "runtime" / "system-auth" / "reset",
    )
    _verify_compose_project_scope(config, "reset")
    _completed(
        ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--volumes", "--remove-orphans"),
        env=environment,
    )
    _completed(("colima", "--profile", runtime.config.colima_profile, "delete", "--force"))
