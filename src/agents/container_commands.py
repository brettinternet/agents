from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .config import AgentsConfig
from .container_runtime import ContainerGarbageCollector, ContainerRuntime, ContainerRuntimeError, _completed
from .db import connect, migrate
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
    common_dir = git_identity(config.project.path)[1]
    return Path(os.path.commonpath((config.root.resolve(), common_dir.resolve())))


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
    common = {
        "AGENTS_BROKER_CONFIG_PATH": str(broker_config),
        "AGENTS_ENV_SCHEMA_PATH": str(config.root / ".env.schema"),
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
    age_key = root / "age-key"
    store = root / "store"
    sops_home = root / "sops-home"
    sops_home.mkdir(mode=0o700, exist_ok=True)
    for path, content in ((age_key, b"# mock broker has no age identity\n"), (store, b"{}\n")):
        if path.is_symlink():
            raise ContainerCommandError("mock broker source file is unsafe")
        if not path.exists():
            path.write_bytes(content)
            path.chmod(0o600)
    return {
        **common,
        "AGENTS_AGE_KEY_PATH": str(age_key),
        "AGENTS_SOPS_HOME_PATH": str(sops_home),
        "AGENTS_SECRET_STORE_PATH": str(store),
    }


def _compose_environment(config: AgentsConfig, topology_id: str, auth_file: Path) -> dict[str, str]:
    instance = _instance(config)
    provider = _provider(config)
    environment = {
        **_runtime(config).docker_environment(),
        "AGENTS_INSTANCE_ID": instance,
        "AGENTS_TOPOLOGY_ID": topology_id,
        "AGENTS_REPO_PATH": str(config.root.resolve()),
        "AGENTS_UID": str(os.getuid()),
        "AGENTS_GID": str(os.getgid()),
        "AGENTS_PROVIDER": provider,
        "AGENTS_WEB_PORT": str(config.web.port),
        "AGENTS_GIT_COMMON_DIR": str(git_identity(config.project.path)[1]),
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
    return value.encode()


def _topology_record(config: AgentsConfig) -> Path:
    return config.state_dir / "container-topology.json"


def _recover_dead_topology(config: AgentsConfig) -> None:
    record = _topology_record(config)
    if record.exists() or record.is_symlink():
        if not record.is_file() or record.is_symlink():
            raise ContainerCommandError("whole-system topology ownership record is unsafe")
        try:
            value = json.loads(record.read_text())
            topology_id = str(value["topology_id"])
            auth_file = Path(str(value["auth_file"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ContainerCommandError("whole-system topology ownership record is malformed") from exc
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
        if any(
            bool(inspect.get("State", {}).get("Running"))
            for name in names.splitlines()
            if (inspect := runtime.inspect_container(name)) is not None
        ):
            raise ContainerCommandError("whole-system topology ownership record belongs to live containers")
        expected = (config.state_dir / "runtime" / "system-auth").resolve()
        if auth_file.is_symlink() or auth_file.parent.resolve() != expected:
            raise ContainerCommandError("whole-system credential path is unsafe")
        _completed(
            ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--remove-orphans"),
            env=_compose_environment(config, topology_id, auth_file),
        )
        auth_file.unlink(missing_ok=True)
        record.unlink()

    directory = config.state_dir / "runtime" / "system-auth"
    if not directory.is_dir() or directory.is_symlink():
        return
    runtime = _runtime(config)
    instance = _instance(config)
    for candidate in directory.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            continue
        containers = runtime.docker(
            "container",
            "ls",
            "--all",
            "--filter",
            f"label=dev.agents.instance={instance}",
            "--filter",
            f"label=dev.agents.topology={candidate.name}",
            "--format",
            "{{.Names}}",
        )
        if not containers:
            candidate.unlink()


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
    topology_id = uuid.uuid4().hex
    directory = config.state_dir / "runtime" / "system-auth"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_file = directory / topology_id
    auth_value = _auth_value(config)
    descriptor = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, auth_value)
    finally:
        os.close(descriptor)
    environment = _compose_environment(config, topology_id, auth_file)
    janitor_process = None
    try:
        _completed(
            ("docker", "compose", "-f", str(config.root / "compose.yaml"), "up", "--detach", "--wait"),
            env=environment,
        )
        record.write_text(json.dumps({"topology_id": topology_id, "auth_file": str(auth_file)}))
        record.chmod(0o600)
        executable = config.root / ".venv" / "bin" / "python"
        janitor_process = service._launch_process(
            config,
            "container-janitor",
            executable,
            ["-m", "agents.cli", "container", "janitor"],
            environment,
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
            _completed(
                ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--remove-orphans"),
                env=environment,
            )
        record.unlink(missing_ok=True)
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
    expected = (config.state_dir / "runtime" / "system-auth").resolve()
    if auth_file.is_symlink() or auth_file.parent.resolve() != expected:
        raise ContainerCommandError("whole-system credential path is unsafe")
    service._stop_named(config, "container-janitor")
    _completed(
        ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--remove-orphans"),
        env=_compose_environment(config, topology_id, auth_file),
    )
    deadline = time.monotonic() + 10
    while not _web_port_available(config.web.port):
        if time.monotonic() >= deadline:
            raise ContainerCommandError(
                f"web port 127.0.0.1:{config.web.port} remained occupied after whole-system shutdown"
            )
        time.sleep(0.1)
    auth_file.unlink(missing_ok=True)
    record.unlink(missing_ok=True)


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
        return ContainerGarbageCollector(config, connection).collect()
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
            and labels.get("dev.agents.retention") == "ephemeral"
            and not bool(state.get("Running"))
        ):
            runtime.remove_container(name)
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
        time.sleep(3600)


def reset(config: AgentsConfig) -> None:
    from . import service

    if _topology_record(config).exists() or any(service.status(config).values()):
        raise ContainerCommandError("all Agents topologies must be stopped before container:reset")
    runtime = _runtime(config)
    environment = _compose_environment(
        config,
        "reset",
        config.state_dir / "runtime" / "system-auth" / "reset",
    )
    _completed(
        ("docker", "compose", "-f", str(config.root / "compose.yaml"), "down", "--volumes", "--remove-orphans"),
        env=environment,
    )
    _completed(("colima", "--profile", runtime.config.colima_profile, "delete", "--force"))


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
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            smoke_port = int(probe.getsockname()[1])
        instance = _instance(config)
        runtime.initialize(_mount_root(config), instance, smoke_port)
        environment = dict(os.environ)
        environment["AGENTS_SMOKE_API_PORT"] = str(smoke_port)
        try:
            _completed(
                (
                    sys.executable,
                    "-m",
                    "tests.smoke_e2e",
                    "--backend",
                    "herdr",
                    "--isolation",
                    "container",
                ),
                env=environment,
            )
        finally:
            runtime.initialize(_mount_root(config), instance, config.web.port)
        return
    runtime_init(config)
    if status(config) == "stopped":
        raise ContainerCommandError("whole-system topology is not running")
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{config.web.port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    return
        except urllib.error.URLError, OSError, TimeoutError:
            time.sleep(0.25)
    raise ContainerCommandError("whole-system health endpoint did not become ready")
