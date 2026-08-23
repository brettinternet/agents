from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from .config import AgentsConfig, resolve_execution_session


class ServiceError(RuntimeError):
    pass


_LOCK_HANDLE: IO[str] | None = None
_EXPLICIT_RESTART = "run `task server:stop` and then `task server:start` explicitly"
_HERDR_CONFIG = "[session]\nresume_agents_on_restore = false\n\n[experimental]\npane_history = false\n"


def _herdr_symbols() -> tuple[Any, Any, Any]:
    from .herdr_client import HerdrClient, herdr_executable, herdr_socket_path

    return HerdrClient, herdr_executable, herdr_socket_path


def _session(config: AgentsConfig) -> str:
    value = resolve_execution_session(config)
    if not value:
        raise ServiceError("Agents project identity is missing; initialize the database before starting services")
    return value


def _herdr_environment(config: AgentsConfig) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTS_CONFIG": str(config.source),
            "HERDR_CONFIG_PATH": str(config.herdr_config),
        }
    )
    return environment


def _write_herdr_config(config: AgentsConfig) -> None:
    state = config.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or not state.is_dir() or state.stat().st_mode & 0o077:
        raise ServiceError(".agents must have mode 0700")
    path = config.herdr_config
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ServiceError(f"unsafe Herdr config path: {path}")
    path.write_text(_HERDR_CONFIG, encoding="utf-8")
    path.chmod(0o600)


def _herdr_client(config: AgentsConfig, session: str) -> Any:
    HerdrClient, _, herdr_socket_path = _herdr_symbols()
    environment = _herdr_environment(config)
    return HerdrClient(herdr_socket_path(session, env=environment), expected_version=config.execution.version)


def _herdr_health(config: AgentsConfig, session: str) -> bool:
    client = _herdr_client(config, session)
    try:
        health = client.health()
        if isinstance(health, Mapping):
            return bool(
                health.get("healthy")
                and health.get("version") == config.execution.version
                and health.get("protocol") == 20
                and health.get("supports_events")
            )
        return bool(
            health.healthy
            and health.version == config.execution.version
            and health.protocol == 20
            and health.supports_events
        )
    except Exception:
        return False
    finally:
        client.close()


def _web_health_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError, TimeoutError, OSError:
        return False


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _process_started(pid: int) -> str:
    try:
        value = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.SubprocessError as exc:
        raise ServiceError(f"cannot read process {pid} start time") from exc
    if not value:
        raise ServiceError(f"process {pid} has no start time")
    return " ".join(value.split())


def _record(path: Path, process: subprocess.Popen[bytes], executable: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "executable": str(executable.resolve()),
                "started": _process_started(process.pid),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _owned(path: Path) -> tuple[int, dict[str, object]] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = int(record["pid"])
        expected = str(record["executable"])
        started = str(record["started"])
        actual_started = _process_started(pid)
        os.kill(pid, 0)
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=True
        ).stdout.strip()
    except OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, ServiceError:
        return None
    if started != actual_started:
        raise ServiceError(f"pid {pid} start time does not match owned record")
    executable_tokens = {
        str(Path(token).resolve()) for token in shlex.split(command) if token.startswith("/") and Path(token).exists()
    }
    if expected not in executable_tokens:
        raise ServiceError(f"pid {pid} executable does not match owned record")
    return pid, record


def acquire_daemon_lock(state_dir: Path) -> IO[str]:
    global _LOCK_HANDLE
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / "agentsd.lock"
    handle = path.open("a+", encoding="utf-8")
    path.chmod(0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ServiceError("agentsd lock is already held") from exc
    _LOCK_HANDLE = handle
    return handle


def _herdr_command(config: AgentsConfig, session: str, *arguments: str) -> tuple[list[str], dict[str, str]]:
    _, herdr_executable, _ = _herdr_symbols()
    try:
        executable = herdr_executable()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ServiceError(f"required Herdr executable is not installed: {exc}") from exc
    return [str(executable), "--session", session, *arguments], _herdr_environment(config)


def _launch_process(
    config: AgentsConfig,
    name: str,
    executable: Path,
    arguments: list[str],
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    log = (config.state_dir / f"{name}.log").open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [str(executable), *arguments],
            cwd=config.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        log.close()
        raise
    _record(config.state_dir / f"{name}.pid", process, executable)
    return process


def _stop_started_process(config: AgentsConfig, name: str, process: subprocess.Popen[bytes]) -> None:
    """Stop a process launched by this call, tolerating an exit before readiness."""
    if process.poll() is None:
        _stop_named(config, name)
        return
    path = config.state_dir / f"{name}.pid"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, json.JSONDecodeError:
        return
    if record.get("pid") == process.pid:
        path.unlink(missing_ok=True)


def start(config: AgentsConfig) -> None:
    state = config.state_dir
    if os.environ.get("AGENTS_TOPOLOGY") != "compose" and (state / "container-topology.json").exists():
        raise ServiceError("whole-system Compose topology is owned; stop it before starting host services")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077:
        raise ServiceError(".agents must have mode 0700")
    session = _session(config)
    paths = {name: state / f"{name}.pid" for name in ("agentsd", "herdr")}
    try:
        owned = {name: _owned(path) for name, path in paths.items()}
    except ServiceError as exc:
        raise ServiceError(f"{exc}; {_EXPLICIT_RESTART}") from exc
    stale = [name for name, path in paths.items() if (path.exists() or path.is_symlink()) and owned[name] is None]
    if stale:
        raise ServiceError(f"stale service ownership record for {', '.join(stale)}; {_EXPLICIT_RESTART}")

    herdr_owned = owned["herdr"] is not None
    agentsd_owned = owned["agentsd"] is not None
    herdr_ready = herdr_owned and _herdr_health(config, session)
    web_ready = agentsd_owned and _web_health_ready(f"http://{config.web.host}:{config.web.port}/health")
    if herdr_owned and not herdr_ready:
        raise ServiceError(f"owned Herdr is running but unhealthy; {_EXPLICIT_RESTART}")
    if agentsd_owned and not herdr_owned:
        raise ServiceError(f"Agents is running without its owned Herdr session; {_EXPLICIT_RESTART}")
    if herdr_ready and agentsd_owned and not web_ready:
        raise ServiceError(f"owned Agents service is running but unhealthy; {_EXPLICIT_RESTART}")
    if herdr_ready and web_ready:
        return

    _write_herdr_config(config)
    herdr_process: subprocess.Popen[bytes] | None = None
    agentsd_process: subprocess.Popen[bytes] | None = None
    try:
        if not herdr_ready:
            command, environment = _herdr_command(config, session, "server")
            herdr_process = _launch_process(config, "herdr", Path(command[0]), command[1:], environment)
        if not agentsd_owned:
            agentsd = config.root / ".venv" / "bin" / "agentsd"
            if not agentsd.is_file() or not os.access(agentsd, os.X_OK):
                raise ServiceError("required managed executable is not installed: agentsd")
            agentsd_process = _launch_process(config, "agentsd", agentsd, [], _herdr_environment(config))

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if herdr_process is not None and herdr_process.poll() is not None:
                raise ServiceError("Herdr exited before readiness")
            if agentsd_process is not None and agentsd_process.poll() is not None:
                raise ServiceError("agentsd exited before readiness")
            if _herdr_health(config, session) and _web_health_ready(
                f"http://{config.web.host}:{config.web.port}/health"
            ):
                return
            time.sleep(0.25)
    except BaseException:
        if agentsd_process is not None:
            _stop_started_process(config, "agentsd", agentsd_process)
        if herdr_process is not None:
            _stop_started_process(config, "herdr", herdr_process)
        raise
    if agentsd_process is not None:
        _stop_started_process(config, "agentsd", agentsd_process)
    if herdr_process is not None:
        _stop_started_process(config, "herdr", herdr_process)
    raise ServiceError("services did not become ready within 30 seconds")


def foreground(config: AgentsConfig) -> None:
    """Supervise Herdr and agentsd in the current PID namespace."""
    auth_path_value = os.environ.get("AGENTS_PROVIDER_AUTH_FILE")
    if auth_path_value and config.execution.provider != "mock":
        auth_path = Path(auth_path_value)
        if auth_path.is_symlink() or not auth_path.is_file() or auth_path.stat().st_mode & 0o077:
            raise ServiceError("unsafe whole-system provider credential file")
        value = auth_path.read_text()
        if config.execution.provider == "opencode":
            target = Path.home() / ".local/share/opencode/auth.json"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_text(value)
            target.chmod(0o600)
        elif config.execution.provider == "claude":
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = value
    _write_herdr_config(config)
    session = _session(config)
    command, environment = _herdr_command(config, session, "server")
    agentsd_value = shutil.which("agentsd")
    if agentsd_value is None:
        raise ServiceError("required agentsd executable is not installed")
    herdr = subprocess.Popen(command, cwd=config.root, env=environment, start_new_session=True)
    agentsd: subprocess.Popen[bytes] | None = None
    stopping = False

    def terminate(_: int, __: Any) -> None:
        nonlocal stopping
        stopping = True
        for process in (agentsd, herdr):
            if process is not None and process.poll() is None:
                process.terminate()

    previous = {value: signal.signal(value, terminate) for value in (signal.SIGTERM, signal.SIGINT)}
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _herdr_health(config, session):
            if herdr.poll() is not None:
                raise ServiceError("Herdr exited before foreground readiness")
            time.sleep(0.25)
        if not _herdr_health(config, session):
            raise ServiceError("Herdr did not become ready within 30 seconds")
        agentsd = subprocess.Popen(
            [agentsd_value],
            cwd=config.root,
            env=environment,
            start_new_session=True,
        )
        while not stopping:
            if herdr.poll() is not None:
                raise ServiceError("Herdr exited while supervising whole-system services")
            if agentsd.poll() is not None:
                raise ServiceError("agentsd exited while supervising whole-system services")
            time.sleep(0.25)
    finally:
        for process in (agentsd, herdr):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (agentsd, herdr):
            if process is not None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for value, handler in previous.items():
            signal.signal(value, handler)


def _stop_named(config: AgentsConfig, name: str, *, remove_record: bool = True) -> Path | None:
    path = config.state_dir / f"{name}.pid"
    if not path.exists() and not path.is_symlink():
        return None
    owned = _owned(path)
    if owned is None:
        raise ServiceError(f"cannot stop unowned {name} process; {_EXPLICIT_RESTART}")
    pid, _ = owned
    try:
        os.killpg(pid, signal.SIGTERM)
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except PermissionError:
            os.kill(pid, signal.SIGKILL)
    if remove_record:
        path.unlink(missing_ok=True)
    return path


def stop_agents(config: AgentsConfig) -> None:
    """Stop agentsd while deliberately retaining the owned Herdr session."""
    _stop_named(config, "agentsd")


def stop_herdr(config: AgentsConfig) -> None:
    """Stop the owned Herdr server without deleting its persisted session."""
    _stop_named(config, "herdr")


def stop(config: AgentsConfig) -> None:
    stop_agents(config)


def _workspaces(snapshot: Any) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    payload = snapshot.get("snapshot")
    if not isinstance(payload, Mapping):
        return []
    value = payload.get("workspaces")
    return [entry for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []


def _workspace_label(workspace: Mapping[str, Any]) -> str:
    value = workspace.get("label")
    return value if isinstance(value, str) else ""


def _workspace_id(workspace: Mapping[str, Any]) -> str | None:
    value = workspace.get("workspace_id")
    return value if isinstance(value, str) and value else None


def _workspace_cwd(workspace: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str | None:
    workspace_id = _workspace_id(workspace)
    panes = snapshot.get("panes")
    if workspace_id is not None and isinstance(panes, list):
        for pane in panes:
            if isinstance(pane, Mapping) and pane.get("workspace_id") == workspace_id:
                value = pane.get("cwd")
                if isinstance(value, str):
                    return value
    worktree = workspace.get("worktree")
    if isinstance(worktree, Mapping):
        value = worktree.get("checkout_path")
        if isinstance(value, str):
            return value
    return None


def _close_mapped_workspaces(client: Any, prefix: str, expected_cwds: set[str]) -> list[str]:
    expected_cwds = {str(Path(cwd).resolve()) for cwd in expected_cwds}
    failures: list[str] = []
    try:
        response = client.request("session.snapshot", {})
    except Exception as exc:
        return [f"session snapshot: {exc}"]
    snapshot = response.get("snapshot") if isinstance(response, Mapping) else None
    if not isinstance(snapshot, Mapping):
        return ["session snapshot: malformed response"]
    matches = [workspace for workspace in _workspaces(response) if _workspace_label(workspace).startswith(prefix)]
    for workspace in matches:
        label = _workspace_label(workspace)
        workspace_id = _workspace_id(workspace)
        cwd = _workspace_cwd(workspace, snapshot)
        if workspace_id is None:
            failures.append(f"{label}: workspace identity unavailable")
            continue
        if cwd is None or (expected_cwds and str(Path(cwd).resolve()) not in expected_cwds):
            failures.append(f"{label}: workspace cwd does not match Agents state")
            continue
        try:
            client.request("workspace.close", {"workspace_id": workspace_id})
        except Exception as exc:
            failures.append(f"{label}: workspace close: {exc}")
    try:
        remaining = [
            workspace
            for workspace in _workspaces(client.request("session.snapshot", {}))
            if _workspace_label(workspace).startswith(prefix)
        ]
    except Exception as exc:
        failures.append(f"session snapshot after close: {exc}")
    else:
        failures.extend(f"{_workspace_label(workspace)}: workspace still exists" for workspace in remaining)
    return failures


def _delete_session(config: AgentsConfig, session: str) -> None:
    command, environment = _herdr_command(config, session, "session", "delete", session)
    result = subprocess.run(command, cwd=config.root, env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "session delete failed").strip()
        raise ServiceError(detail)


def _cleanup_profiles(config: AgentsConfig, connection: Any, rows: list[Any], project: Any) -> list[str]:
    """Remove manifest-owned provider artifacts after external workspaces are gone."""
    from .auth import derive_agent_token, read_private_secret
    from .profiles import remove_profile

    key = bytes.fromhex(read_private_secret(config.state_dir / "agent-auth-key"))
    failures: list[str] = []
    for row in rows:
        profile = str(row["profile_name"])
        if profile in {"", "reserved"}:
            continue
        artifacts = [
            {
                "kind": str(item["kind"]),
                "path": str(item["path"]),
                "sha256": str(item["expected_sha256"]),
                "fragment_key": item["fragment_key"],
                "expected_json_redacted": item["expected_json_redacted"],
                "secret_fields_json": item["secret_fields_json"],
            }
            for item in connection.execute(
                "SELECT kind,path,fragment_key,expected_sha256,expected_json_redacted,secret_fields_json "
                "FROM terminal_artifacts WHERE terminal_run_id=? AND state IN ('staged','installed')",
                (row["id"],),
            )
        ]
        profile_path = config.state_dir / "profiles" / f"{profile}.md"
        if not artifacts and profile_path.is_file() and not profile_path.is_symlink():
            artifacts = [{"path": str(profile_path), "sha256": str(row["profile_sha256"])}]
        secret_values = {
            "AGENTS_AGENT_TOKEN": derive_agent_token(
                key, str(project["instance_id"]), int(row["id"]), int(row["generation"])
            )
        }
        try:
            remove_profile(
                profile,
                profile_path,
                artifacts,
                config.state_dir / "profiles.lock",
                runtime_dir=config.state_dir / "runtime",
                secret_values=secret_values,
            )
        except Exception as exc:
            failures.append(f"profile {profile}: {exc}")
    return failures


def shutdown(config: AgentsConfig, client: Any | None = None) -> None:
    """Fence durable runs, close mapped workspaces, remove artifacts, and delete the Herdr session."""
    from .db import connect, utc_now

    stop_agents(config)
    database = config.db_path
    if not database.exists():
        session = _session(config)
        herdr_client = client
        if herdr_client is None:
            herdr_client = _herdr_client(config, session)
        try:
            failures = _close_mapped_workspaces(
                herdr_client,
                f"{session}-",
                {str(config.project.path.resolve())},
            )
        finally:
            if client is None and herdr_client is not None:
                herdr_client.close()
        if failures:
            raise ServiceError("shutdown cleanup incomplete: " + "; ".join(failures))
        pid_record = _stop_named(config, "herdr", remove_record=False)
        _delete_session(config, session)
        if pid_record is not None:
            pid_record.unlink(missing_ok=True)
        return

    shutdown_lock = acquire_daemon_lock(config.state_dir)
    connection = connect(database)
    herdr_client: Any | None = client
    cleanup_complete = False
    session: str | None = None
    try:
        project = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        if project is None:
            raise ServiceError("Agents project identity is missing; Herdr was left running")
        session = resolve_execution_session(config, connection)
        if not session:
            raise ServiceError("Agents execution session is not configured or initialized")
        prefix = f"agents-{project['instance_id']}-"
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = list(
                connection.execute(
                    "SELECT * FROM terminal_runs WHERE execution_name LIKE ? ORDER BY id", (f"{prefix}%",)
                )
            )
            connection.execute(
                "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),"
                "state=CASE WHEN state IN ('live','creating','reserved','retained') THEN 'ending' ELSE state END,"
                "updated_at=? WHERE execution_name LIKE ?",
                (now, now, f"{prefix}%"),
            )
            connection.execute(
                "UPDATE launch_attempts SET state=CASE WHEN state='reserved' THEN 'aborted' "
                "WHEN state IN ('posting','uncertain') THEN 'failed' ELSE state END,"
                "counted=CASE WHEN state='reserved' THEN 0 ELSE counted END,error=COALESCE(error,'shutdown'),"
                "updated_at=? WHERE terminal_run_id IN "
                "(SELECT id FROM terminal_runs WHERE execution_name LIKE ?) "
                "AND state IN ('reserved','posting','uncertain')",
                (now, f"{prefix}%"),
            )
            connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id IN "
                "(SELECT id FROM terminal_runs WHERE execution_name LIKE ?) AND released_at IS NULL",
                (now, f"{prefix}%"),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        expected_cwds = {str(Path(row["working_directory"]).resolve()) for row in rows}
        if herdr_client is None:
            herdr_client = _herdr_client(config, session)
        failures = _close_mapped_workspaces(herdr_client, prefix, expected_cwds)
        failures.extend(_cleanup_profiles(config, connection, rows, project))
        if failures:
            raise ServiceError("shutdown cleanup incomplete: " + "; ".join(failures))
        for row in rows:
            now = utc_now()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE terminal_artifacts SET state='removed',updated_at=? "
                "WHERE terminal_run_id=? AND state IN ('staged','installed')",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE terminal_runs SET state='ended',profile_state='removed',updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            connection.commit()
        cleanup_complete = True
    finally:
        if herdr_client is not None and client is None:
            herdr_client.close()
        connection.close()
        shutdown_lock.close()
        if cleanup_complete:
            if session is None:
                raise ServiceError("Agents execution session disappeared during shutdown")
            pid_record = _stop_named(config, "herdr", remove_record=False)
            _delete_session(config, session)
            if pid_record is not None:
                pid_record.unlink(missing_ok=True)


def status(config: AgentsConfig) -> dict[str, bool]:
    return {name: _owned(config.state_dir / f"{name}.pid") is not None for name in ("agentsd", "herdr")}
