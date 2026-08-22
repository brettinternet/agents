from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Any

from .config import AgentsConfig


class ServiceError(RuntimeError):
    pass


_LOCK_HANDLE: IO[str] | None = None


def _terminal_matches(row: Any, terminal: dict[str, Any]) -> bool:
    identity = terminal.get("session_name", terminal.get("tmux_session", terminal.get("name")))
    provider = terminal.get("provider", terminal.get("provider_id"))
    profile = terminal.get("profile", terminal.get("profile_name", terminal.get("agent_profile")))
    return identity == row["session_name"] and provider == row["provider"] and profile == row["profile_name"]


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _health_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError, TimeoutError, OSError:
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


def start(config: AgentsConfig) -> None:
    state = config.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077:
        raise ServiceError(".agents must have mode 0700")
    if _owned(state / "agentsd.pid") or _owned(state / "cao.pid"):
        raise ServiceError("an owned service is already running")
    if not _port_free("127.0.0.1", config.cao.api_port) or not _port_free(config.web.host, config.web.port):
        raise ServiceError("configured listener is already owned by another process")
    cao = config.root / ".tools" / "bin" / "cao-server"
    agentsd = config.root / ".venv" / "bin" / "agentsd"
    if not cao.is_file() or not os.access(cao, os.X_OK) or not agentsd.is_file() or not os.access(agentsd, os.X_OK):
        raise ServiceError("required managed executables are not installed")
    config.cao_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env.update(
        {
            "CAO_HOME_DIR": str(config.cao_home),
            "CAO_API_PORT": str(config.cao.api_port),
            "AGENTS_CONFIG": str(config.source),
        }
    )
    cao_env = env | {"CAO_API_HOST": "127.0.0.1"}
    cao_log = (state / "cao.log").open("ab", buffering=0)
    web_log = (state / "agentsd.log").open("ab", buffering=0)
    cao_process = subprocess.Popen(
        [str(cao), "--port", str(config.cao.api_port), "--terminal", "tmux"],
        cwd=config.root,
        env=cao_env,
        stdin=subprocess.DEVNULL,
        stdout=cao_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _record(state / "cao.pid", cao_process, cao)
    web_process = subprocess.Popen(
        [str(agentsd)],
        cwd=config.root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=web_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _record(state / "agentsd.pid", web_process, agentsd)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if cao_process.poll() is not None or web_process.poll() is not None:
            stop(config)
            raise ServiceError("a service exited before readiness")
        cao_ready = not _port_free("127.0.0.1", config.cao.api_port) and _health_ready(
            f"http://127.0.0.1:{config.cao.api_port}/health"
        )
        web_ready = not _port_free(config.web.host, config.web.port) and _health_ready(
            f"http://{config.web.host}:{config.web.port}/health"
        )
        if cao_ready and web_ready:
            return
        time.sleep(0.25)
    stop(config)
    raise ServiceError("services did not become ready within 30 seconds")


def _stop_named(config: AgentsConfig, name: str) -> None:
    path = config.state_dir / f"{name}.pid"
    owned = _owned(path)
    if owned:
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
    path.unlink(missing_ok=True)


def stop_agents(config: AgentsConfig) -> None:
    """Stop agentsd and its reconciler while preserving CAO and state."""
    _stop_named(config, "agentsd")


def stop_cao(config: AgentsConfig) -> None:
    """Stop the CAO service after Agents-owned session cleanup."""
    _stop_named(config, "cao")


def stop(config: AgentsConfig) -> None:
    stop_agents(config)
    stop_cao(config)


def shutdown(config: AgentsConfig, client: Any | None = None) -> None:
    """Stop Agents, fence its state, clean mapped CAO sessions, then stop CAO."""
    from .auth import derive_agent_token, read_private_secret
    from .cao_client import CaoClient, CaoNotFound
    from .db import connect, utc_now
    from .profiles import remove_profile, validate_manifest_artifact

    database = config.state_dir / "agents.db"
    if not database.exists():
        stop_agents(config)
        stop_cao(config)
        return

    # Quiesce the reconciler before opening state.  Holding this lock after the
    # process exits prevents a replacement agentsd from reserving a run
    # between the durable snapshot and external CAO cleanup.
    stop_agents(config)
    shutdown_lock = acquire_daemon_lock(config.state_dir)
    connection = connect(database)
    cao_client = client or CaoClient(config.cao.api_port)
    failures: list[str] = []
    cleanup_complete = False
    try:
        project = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        if project is None:
            raise ServiceError("Agents project identity is missing; CAO was left running")
        key = bytes.fromhex(read_private_secret(config.state_dir / "agent-auth-key"))
        prefix = f"cao-agents-{project['instance_id']}-"
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = list(
                connection.execute(
                    "SELECT * FROM terminal_runs WHERE session_name LIKE ? ORDER BY id",
                    (f"{prefix}%",),
                )
            )
            connection.execute(
                "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),"
                "state=CASE WHEN state IN ('live','creating','reserved','retained') "
                "THEN 'ending' ELSE state END,updated_at=? WHERE session_name LIKE ?",
                (now, now, f"{prefix}%"),
            )
            connection.execute(
                "UPDATE launch_attempts SET state=CASE WHEN state='reserved' THEN 'aborted' "
                "WHEN state IN ('posting','uncertain') THEN 'failed' ELSE state END,"
                "counted=CASE WHEN state='reserved' THEN 0 ELSE counted END,error=COALESCE(error,'shutdown'),"
                "updated_at=? WHERE terminal_run_id IN "
                "(SELECT id FROM terminal_runs WHERE session_name LIKE ?) "
                "AND state IN ('reserved','posting','uncertain')",
                (now, f"{prefix}%"),
            )
            connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id IN "
                "(SELECT id FROM terminal_runs WHERE session_name LIKE ?) AND released_at IS NULL",
                (now, f"{prefix}%"),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        names = {str(row["session_name"]) for row in rows}
        list_sessions = getattr(cao_client, "list_sessions", None)
        if callable(list_sessions):
            try:
                sessions = list_sessions()
            except Exception as exc:
                failures.append(f"list sessions: {exc}")
            else:
                if not isinstance(sessions, list):
                    failures.append("list sessions: response is not an array")
                    sessions = []
                for session in sessions:
                    if not isinstance(session, dict):
                        continue
                    name = session.get("name", session.get("session_name"))
                    if isinstance(name, str) and name.startswith(prefix):
                        names.add(name)
        unsafe_terminal_rows: set[int] = set()
        terminal_ids: dict[int, str] = {}
        for row in rows:
            terminal_id = row["terminal_id"]
            if isinstance(terminal_id, str):
                terminal_ids[int(row["id"])] = terminal_id
                continue
            if str(row["provider"]) != "claude_code" or str(row["profile_state"]) == "reserved":
                continue
            try:
                terminals = cao_client.list_terminals(str(row["session_name"]))
            except Exception as exc:
                unsafe_terminal_rows.add(int(row["id"]))
                failures.append(f"{row['session_name']}: list terminals: {exc}")
                continue
            if (
                not isinstance(terminals, list)
                or len(terminals) != 1
                or not isinstance(terminals[0], dict)
                or not _terminal_matches(row, terminals[0])
            ):
                unsafe_terminal_rows.add(int(row["id"]))
                failures.append(f"{row['session_name']}: exact terminal identity unavailable")
                continue
            value = terminals[0].get("id") or terminals[0].get("terminal_id")
            if not isinstance(value, str):
                unsafe_terminal_rows.add(int(row["id"]))
                failures.append(f"{row['session_name']}: exact terminal identity unavailable")
                continue
            terminal_ids[int(row["id"])] = value

        unsafe_session_names = {str(row["session_name"]) for row in rows if int(row["id"]) in unsafe_terminal_rows}
        for name in sorted(names):
            if name in unsafe_session_names:
                continue
            try:
                cao_client.delete_session(name)
                cao_client.get_session(name)
            except CaoNotFound:
                continue
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                continue
            failures.append(f"{name}: session still exists after deletion")

        cao = config.root / ".tools" / "bin" / "cao"
        if not cao.is_file():
            cao = config.root / ".tools" / "bin" / "cao-server"
        for row in rows:
            profile = str(row["profile_name"])
            artifact_rows = list(
                connection.execute(
                    "SELECT kind,path,fragment_key,expected_sha256,expected_json_redacted,secret_fields_json "
                    "FROM terminal_artifacts WHERE terminal_run_id=? AND state IN ('staged','installed')",
                    (row["id"],),
                )
            )
            profile_artifacts = [
                {
                    "kind": str(item["kind"]),
                    "path": str(item["path"]),
                    "sha256": str(item["expected_sha256"]),
                    "fragment_key": item["fragment_key"],
                    "expected_json_redacted": item["expected_json_redacted"],
                    "secret_fields_json": item["secret_fields_json"],
                }
                for item in artifact_rows
                if not str(item["kind"]).startswith("runtime_")
            ]
            profile_path = config.state_dir / "profiles" / f"{profile}.md"
            if not profile_artifacts and profile_path.is_file() and not profile_path.is_symlink():
                profile_artifacts = [{"path": str(profile_path), "sha256": str(row["profile_sha256"])}]
            secret_values = {
                "AGENTS_AGENT_TOKEN": derive_agent_token(
                    key, str(project["instance_id"]), int(row["id"]), int(row["generation"])
                )
            }
            try:
                if int(row["id"]) in unsafe_terminal_rows:
                    raise ServiceError("exact terminal identity unavailable; runtime cleanup was not attempted")
                if profile not in {"", "reserved"}:
                    if not cao.is_file():
                        raise ServiceError(f"managed cao executable is missing for profile cleanup: {profile}")
                    remove_profile(
                        cao,
                        config.cao_home,
                        profile,
                        profile_path,
                        profile_artifacts,
                        config.state_dir / "profiles.lock",
                        secret_values=secret_values,
                    )
                terminal_id = terminal_ids.get(int(row["id"]))
                if str(row["provider"]) == "claude_code" and terminal_id is not None:
                    if not terminal_id or Path(terminal_id).name != terminal_id or terminal_id in {".", ".."}:
                        raise ServiceError(f"unsafe terminal ID for runtime cleanup: {terminal_id}")
                    runtime_root = (config.cao_home / "tmp").resolve()
                    runtime_paths = {
                        "runtime_prompt": (runtime_root / f"{terminal_id}.prompt").resolve(),
                        "runtime_mcp": (runtime_root / f"{terminal_id}.mcp.json").resolve(),
                    }
                    if any(path.parent != runtime_root for path in runtime_paths.values()):
                        raise ServiceError(f"runtime path escaped managed root: {terminal_id}")
                    manifests = {
                        str(item["kind"]): item for item in artifact_rows if str(item["kind"]).startswith("runtime_")
                    }
                    for kind, path in runtime_paths.items():
                        manifest = manifests.get(kind)
                        if manifest is not None and Path(str(manifest["path"])).resolve() != path:
                            raise ServiceError(f"runtime manifest path mismatch: {manifest['path']}")
                        if not path.exists():
                            continue
                        if manifest is not None:
                            if not validate_manifest_artifact(
                                path,
                                str(manifest["expected_sha256"]),
                                fragment_key=manifest["fragment_key"],
                                expected_json_redacted=manifest["expected_json_redacted"],
                                secret_fields_json=manifest["secret_fields_json"],
                                secret_values=secret_values,
                                require_secret_values=True,
                            ):
                                raise ServiceError(f"runtime artifact changed: {path}")
                        elif path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
                            raise ServiceError(f"unsafe unsealed runtime artifact: {path}")
                        path.unlink()
                now = utc_now()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
                    (now, row["id"]),
                )
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
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                failures.append(f"profile {profile}: {exc}")
        if failures:
            raise ServiceError("shutdown cleanup incomplete: " + "; ".join(failures))
        cleanup_complete = True
    finally:
        try:
            close = getattr(cao_client, "close", None)
            if callable(close):
                close()
        finally:
            connection.close()
            shutdown_lock.close()
            if cleanup_complete:
                stop_cao(config)


def status(config: AgentsConfig) -> dict[str, bool]:
    return {name: _owned(config.state_dir / f"{name}.pid") is not None for name in ("agentsd", "cao")}
