from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import socket
import stat
import threading
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .execution import (
    BackendHealth,
    ExecutionBusy,
    ExecutionConflict,
    ExecutionError,
    ExecutionEvent,
    ExecutionNotFound,
    ExecutionProtocolError,
    ExecutionRejected,
    ExecutionStatus,
    ExecutionTerminated,
    ExecutionTimeout,
    ExecutionUnauthorized,
    ExecutionUnavailable,
    RunHandle,
    RunSnapshot,
    RunSpec,
)

if TYPE_CHECKING:
    from .config import AgentsConfig

_EXPECTED_VERSION = "0.8.2"
_EXPECTED_PROTOCOL = 20
_MUTATING_METHODS = {
    "workspace.create",
    "workspace.close",
    "agent.start",
    "agent.prompt",
    "pane.send_input",
}
_EVENT_TYPES = (
    "pane.updated",
    "pane.agent_status_changed",
    "pane.exited",
    "pane.closed",
    "workspace.closed",
)
_EVENT_KIND_MAP = {kind.replace(".", "_"): kind for kind in _EVENT_TYPES}


def herdr_executable() -> Path:
    value = shutil.which("herdr")
    if value is None:
        raise ExecutionUnavailable("herdr_not_found", "herdr executable is not installed")
    return Path(value).resolve()


def herdr_socket_path(session: str, env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    home = Path(values.get("HOME", str(Path.home()))).expanduser()
    return home / ".config" / "herdr" / "sessions" / session / "herdr.sock"


def _status(value: object) -> ExecutionStatus:
    return {
        "working": ExecutionStatus.PROCESSING,
        "blocked": ExecutionStatus.WAITING_USER_ANSWER,
        "done": ExecutionStatus.COMPLETED,
        "idle": ExecutionStatus.IDLE,
        "error": ExecutionStatus.ERROR,
    }.get(str(value), ExecutionStatus.UNKNOWN)


def _error(code: str, message: str, *, mutating: bool = False) -> ExecutionError:
    normalized = code.lower()
    if normalized in {"not_found", "workspace_not_found", "pane_not_found", "agent_not_found"}:
        return ExecutionNotFound(code, message)
    if normalized in {"agent_blocked", "agent_not_ready", "busy", "pane_busy"}:
        return ExecutionBusy(code, message)
    if normalized in {"unauthorized", "forbidden"}:
        return ExecutionUnauthorized(code, message)
    if normalized in {"conflict", "duplicate", "name_conflict", "ambiguous_target"}:
        return ExecutionConflict(code, message, outcome_unknown=mutating)
    if normalized in {"terminated", "pane_exited", "workspace_closed"}:
        return ExecutionTerminated(code, message)
    return ExecutionRejected(code, message)


class HerdrClient:
    def __init__(
        self,
        socket_path: Path,
        expected_version: str = _EXPECTED_VERSION,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.socket_path = socket_path
        self.expected_version = expected_version
        self.timeout = timeout
        self._counter = 0
        self._lock = threading.Lock()

    def _validate_socket(self) -> None:
        try:
            metadata = self.socket_path.stat()
        except FileNotFoundError as exc:
            raise ExecutionUnavailable("socket_missing", f"Herdr socket is absent: {self.socket_path}") from exc
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ExecutionProtocolError("unsafe_socket", f"Herdr socket path is not a socket: {self.socket_path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
            raise ExecutionProtocolError(
                "unsafe_socket", "Herdr socket must be owned by the current user with mode 0600"
            )

    def request_with_id(self, method: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        self._validate_socket()
        mutating = method in _MUTATING_METHODS
        with self._lock:
            self._counter += 1
            request_id = f"agents-{os.getpid()}-{self._counter}-{uuid.uuid4().hex[:8]}"
            payload = (
                json.dumps(
                    {"id": request_id, "method": method, "params": dict(params)},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(self.timeout)
            written = False
            write_attempted = False
            try:
                client.connect(str(self.socket_path))
                write_attempted = True
                client.sendall(payload)
                written = True
                stream = client.makefile("rb")
                line = stream.readline()
            except TimeoutError as exc:
                raise ExecutionTimeout(
                    "timeout",
                    f"Herdr {method} timed out",
                    outcome_unknown=(written or write_attempted) and mutating,
                ) from exc
            except (ConnectionError, OSError) as exc:
                if written or (write_attempted and mutating):
                    raise ExecutionTimeout(
                        "disconnected",
                        f"Herdr disconnected during {method}: {exc}",
                        outcome_unknown=mutating,
                    ) from exc
                raise ExecutionUnavailable("unavailable", f"cannot connect to Herdr: {exc}") from exc
            finally:
                client.close()
        if not line:
            raise ExecutionTimeout("disconnected", f"Herdr disconnected during {method}", outcome_unknown=mutating)
        try:
            response = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionProtocolError("malformed_json", "Herdr returned malformed JSON") from exc
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise ExecutionProtocolError("response_id", "Herdr response ID does not match the request")
        error = response.get("error")
        if error is not None:
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
            ):
                raise ExecutionProtocolError("error_shape", "Herdr returned a malformed error")
            raise _error(error["code"], error["message"], mutating=mutating)
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("type"), str):
            raise ExecutionProtocolError("result_shape", "Herdr returned a malformed result")
        return cast(dict[str, Any], result), request_id

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        result, _ = self.request_with_id(method, params)
        return result

    def health(self) -> BackendHealth:
        try:
            pong = self.request("ping", {})
        except ExecutionError as exc:
            return BackendHealth(False, "", message=str(exc))
        version = pong.get("version")
        protocol = pong.get("protocol")
        capabilities = pong.get("capabilities")
        if (
            pong.get("type") != "pong"
            or not isinstance(version, str)
            or not isinstance(protocol, int)
            or not isinstance(capabilities, dict)
            or any(not isinstance(value, bool) for value in capabilities.values())
        ):
            return BackendHealth(False, str(version or ""), message="malformed Herdr ping response")
        names = tuple(sorted(str(key) for key, enabled in capabilities.items() if enabled))
        advertised_events = capabilities.get("events")
        supports_events = advertised_events is not False and protocol == _EXPECTED_PROTOCOL
        healthy = version == self.expected_version and protocol == _EXPECTED_PROTOCOL and supports_events
        message = (
            ""
            if healthy
            else f"expected Herdr {self.expected_version} protocol {_EXPECTED_PROTOCOL} with events, "
            f"got {version} protocol {protocol}"
        )
        return BackendHealth(
            healthy=healthy,
            version=version,
            protocol=protocol,
            capabilities=names,
            supports_events=supports_events,
            message=message,
        )

    def snapshot(self) -> dict[str, Any]:
        result = self.request("session.snapshot", {})
        snapshot = result.get("snapshot")
        if result.get("type") != "session_snapshot" or not isinstance(snapshot, dict):
            raise ExecutionProtocolError("snapshot_shape", "Herdr returned a malformed session snapshot")
        if snapshot.get("version") != self.expected_version or snapshot.get("protocol") != _EXPECTED_PROTOCOL:
            raise ExecutionProtocolError("snapshot_protocol", "Herdr snapshot version or protocol is incompatible")
        required = {
            "workspaces": ("workspace_id", "label"),
            "panes": ("pane_id", "workspace_id"),
            "agents": ("pane_id",),
        }
        for collection, fields in required.items():
            values = snapshot.get(collection)
            if not isinstance(values, list) or any(
                not isinstance(item, dict) or any(not isinstance(item.get(field), str) for field in fields)
                for item in values
            ):
                raise ExecutionProtocolError(
                    "snapshot_shape",
                    f"Herdr snapshot has malformed {collection}",
                )
        return cast(dict[str, Any], snapshot)

    def close(self) -> None:
        return None


class HerdrBackend:
    def __init__(self, client: HerdrClient, *, provider_id: str, mock_command: tuple[str, ...] = ()) -> None:
        self.client = client
        self.provider_id = provider_id
        self.mock_command = mock_command

    @classmethod
    def from_config(cls, config: AgentsConfig) -> HerdrBackend:
        from .service import resolve_execution_session

        session = resolve_execution_session(config)
        if session is None:
            raise ExecutionUnavailable("session_missing", "Agents project execution session is not initialized")
        return cls(
            HerdrClient(herdr_socket_path(session), config.execution.version),
            provider_id=config.execution.provider_id,
        )

    def health(self) -> BackendHealth:
        return self.client.health()

    @staticmethod
    def _resource(
        snapshot: Mapping[str, Any], workspace: Mapping[str, Any], *, output: str = "", truncated: bool = False
    ) -> RunSnapshot:
        workspace_id = str(workspace["workspace_id"])
        panes = [
            item for item in snapshot["panes"] if isinstance(item, dict) and item.get("workspace_id") == workspace_id
        ]
        if len(panes) != 1:
            raise ExecutionConflict("workspace_shape", f"workspace {workspace_id} does not have exactly one pane")
        pane = panes[0]
        pane_id = str(pane["pane_id"])
        agents = [item for item in snapshot["agents"] if isinstance(item, dict) and item.get("pane_id") == pane_id]
        if len(agents) > 1:
            raise ExecutionConflict("agent_shape", f"pane {pane_id} has multiple agents")
        agent = agents[0] if agents else None
        cwd = pane.get("cwd") or pane.get("foreground_cwd")
        if not isinstance(cwd, str):
            cwd = "/"
        label = workspace.get("label")
        if not isinstance(label, str):
            raise ExecutionProtocolError("workspace_shape", "Herdr workspace label is missing")
        status_value = agent.get("agent_status") if agent else pane.get("agent_status")
        return RunSnapshot(
            RunHandle(label, workspace_id, pane_id),
            _status(status_value),
            label,
            Path(cwd).resolve(),
            str(agent.get("name")) if agent and agent.get("name") is not None else None,
            str(agent.get("agent")) if agent and agent.get("agent") is not None else None,
            int(pane["revision"]) if isinstance(pane.get("revision"), int) else None,
            output,
            truncated,
        )

    def _matching(self, name: str) -> tuple[RunSnapshot, ...]:
        snapshot = self.client.snapshot()
        values = []
        for workspace in snapshot["workspaces"]:
            if isinstance(workspace, dict) and workspace.get("label") == name:
                values.append(self._resource(snapshot, workspace))
        return tuple(values)

    def find_run(self, name: str) -> RunSnapshot | None:
        values = self._matching(name)
        if len(values) > 1:
            raise ExecutionConflict("duplicate_label", f"multiple Herdr workspaces have label {name!r}")
        return values[0] if values else None

    def list_runs(self, prefix: str) -> tuple[RunSnapshot, ...]:
        snapshot = self.client.snapshot()
        return tuple(
            self._resource(snapshot, workspace)
            for workspace in snapshot["workspaces"]
            if isinstance(workspace, dict)
            and isinstance(workspace.get("label"), str)
            and workspace["label"].startswith(prefix)
        )

    @staticmethod
    def _mock_process_running(process_info: Mapping[str, Any], executable: str) -> bool:
        expected = str(Path(executable).resolve())
        expected_name = Path(executable).name
        foreground = process_info.get("foreground_processes")
        return isinstance(foreground, list) and any(
            isinstance(item, dict)
            and (
                Path(str(item.get("argv0") or "")).name == expected_name
                or str(item.get("name") or "") == expected_name
                or any(Path(str(value)).name == expected_name for value in item.get("argv") or ())
                or expected in tuple(str(value) for value in item.get("argv") or ())
                or expected in str(item.get("cmdline") or "")
            )
            for item in foreground
        )

    @staticmethod
    def _matches_spec(run: RunSnapshot, spec: RunSpec) -> bool:
        return (
            run.handle.name == spec.name
            and run.cwd == spec.cwd.resolve()
            and (run.agent_name, run.agent_kind) == (spec.agent_name, spec.agent_kind)
        )

    def _start_existing(self, run: RunSnapshot, spec: RunSpec) -> RunSnapshot:
        if run.cwd != spec.cwd.resolve():
            raise ExecutionConflict("cwd_mismatch", f"workspace {spec.name!r} has an unexpected cwd")
        if run.agent_name is not None:
            if self._matches_spec(run, spec):
                return run
            raise ExecutionConflict("occupant_mismatch", f"workspace {spec.name!r} has an unexpected occupant")
        if spec.mock:
            process_result = self.client.request(
                "pane.process_info",
                {"pane_id": run.handle.terminal_id},
            )
            process_info = process_result.get("process_info")
            if process_result.get("type") != "pane_process_info" or not isinstance(process_info, dict):
                raise ExecutionProtocolError(
                    "pane_process_shape",
                    "Herdr returned a malformed pane.process_info result",
                )
            if not self._mock_process_running(process_info, spec.argv[0]):
                command = "exec " + shlex.join(spec.argv)
                self.client.request(
                    "pane.send_input",
                    {
                        "pane_id": run.handle.terminal_id,
                        "text": command,
                        "keys": ["enter"],
                    },
                )
        else:
            result = self.client.request(
                "agent.start",
                {
                    "name": spec.agent_name,
                    "kind": spec.agent_kind,
                    "pane_id": run.handle.terminal_id,
                    "args": list(spec.argv[1:]),
                    "timeout_ms": 120000,
                },
            )
            if result.get("type") != "agent_started":
                raise ExecutionProtocolError("agent_start_shape", "Herdr returned a malformed agent.start result")
        adopted = self.find_run(spec.name)
        if adopted is None or (not spec.mock and not self._matches_spec(adopted, spec)):
            raise ExecutionConflict("agent_start_mismatch", "Herdr started an unexpected workspace occupant")
        return adopted

    def create_run(self, spec: RunSpec) -> RunSnapshot:
        existing = self.find_run(spec.name)
        if existing is not None:
            return self._start_existing(existing, spec)
        try:
            result = self.client.request(
                "workspace.create",
                {"cwd": str(spec.cwd.resolve()), "label": spec.name, "focus": False, "env": dict(spec.env)},
            )
        except (ExecutionTimeout, ExecutionConflict) as exc:
            existing = self.find_run(spec.name)
            if existing is None:
                raise exc
            return self._start_existing(existing, spec)
        if (
            result.get("type") != "workspace_created"
            or not isinstance(result.get("workspace"), dict)
            or not isinstance(result.get("root_pane"), dict)
        ):
            raise ExecutionProtocolError("workspace_create_shape", "Herdr returned a malformed workspace.create result")
        workspace = cast(dict[str, Any], result["workspace"])
        pane = cast(dict[str, Any], result["root_pane"])
        handle = RunHandle(spec.name, str(workspace["workspace_id"]), str(pane["pane_id"]))
        shell = RunSnapshot(
            handle,
            ExecutionStatus.UNKNOWN,
            spec.name,
            spec.cwd.resolve(),
            revision=cast(int | None, pane.get("revision")),
        )
        return self._start_existing(shell, spec)

    def get_run(self, handle: RunHandle, *, include_output: bool = False) -> RunSnapshot:
        snapshot = self.client.snapshot()
        workspace = next(
            (
                item
                for item in snapshot["workspaces"]
                if isinstance(item, dict) and item.get("workspace_id") == handle.run_id
            ),
            None,
        )
        if workspace is None:
            raise ExecutionNotFound("workspace_not_found", f"Herdr workspace {handle.run_id} is absent")
        pane_result = self.client.request("pane.get", {"pane_id": handle.terminal_id})
        pane = pane_result.get("pane")
        if pane_result.get("type") != "pane_info" or not isinstance(pane, dict):
            raise ExecutionProtocolError("pane_get_shape", "Herdr returned a malformed pane.get result")
        if pane.get("workspace_id") != handle.run_id or pane.get("pane_id") != handle.terminal_id:
            raise ExecutionConflict("identity_mismatch", "Herdr pane identity changed")
        snapshot = dict(snapshot)
        snapshot["panes"] = [
            pane if isinstance(item, dict) and item.get("pane_id") == handle.terminal_id else item
            for item in snapshot["panes"]
        ]
        run = self._resource(snapshot, workspace)
        if self.provider_id == "mock_cli":
            process_result = self.client.request("pane.process_info", {"pane_id": handle.terminal_id})
            process_info = process_result.get("process_info")
            if process_result.get("type") != "pane_process_info" or not isinstance(process_info, dict):
                raise ExecutionProtocolError(
                    "pane_process_shape",
                    "Herdr returned a malformed pane.process_info result",
                )
            executable = self.mock_command[0] if self.mock_command else "mock_cli"
            if not self._mock_process_running(process_info, executable):
                raise ExecutionTerminated("mock_process_exited", "mock provider process is no longer running")
        if not include_output:
            return run
        result = self.client.request(
            "pane.read",
            {
                "pane_id": handle.terminal_id,
                "source": "recent_unwrapped",
                "lines": 4096,
                "format": "text",
                "strip_ansi": True,
            },
        )
        read = result.get("read")
        if result.get("type") != "pane_read" or not isinstance(read, dict) or not isinstance(read.get("text"), str):
            raise ExecutionProtocolError("pane_read_shape", "Herdr returned a malformed pane.read result")
        return RunSnapshot(
            run.handle,
            run.status,
            run.backend_name,
            run.cwd,
            run.agent_name,
            run.agent_kind,
            int(read["revision"]) if isinstance(read.get("revision"), int) else run.revision,
            str(read["text"])[-128 * 1024 :],
            bool(read.get("truncated")),
            run.terminated,
        )

    def send_message(self, handle: RunHandle, sender_id: str, message: str) -> str:
        del sender_id
        if self.provider_id == "mock_cli":
            result, request_id = self.client.request_with_id(
                "pane.send_input",
                {"pane_id": handle.terminal_id, "text": message, "keys": ["enter"]},
            )
            expected_type = "ok"
        else:
            result, request_id = self.client.request_with_id(
                "agent.prompt", {"target": handle.terminal_id, "text": message}
            )
            expected_type = "agent_prompted"
        if result.get("type") != expected_type:
            raise ExecutionProtocolError("wake_shape", "Herdr returned a malformed wake response")
        return request_id

    def send_input(self, handle: RunHandle, message: str) -> None:
        result = self.client.request(
            "pane.send_input",
            {"pane_id": handle.terminal_id, "text": message, "keys": ["enter"]},
        )
        if result.get("type") != "ok":
            raise ExecutionProtocolError("pane_input_shape", "Herdr returned a malformed pane.send_input result")

    def delete_run(self, handle: RunHandle) -> None:
        try:
            result = self.client.request("workspace.close", {"workspace_id": handle.run_id})
        except ExecutionNotFound:
            return
        if result.get("type") != "ok":
            raise ExecutionProtocolError("workspace_close_shape", "Herdr returned a malformed workspace.close result")
        if any(run.handle.run_id == handle.run_id for run in self.list_runs("")):
            raise ExecutionConflict("delete_unconfirmed", f"Herdr workspace {handle.run_id} still exists")

    async def events(self) -> AsyncIterator[ExecutionEvent]:
        subscriptions = [{"type": kind} for kind in _EVENT_TYPES]
        while True:
            writer: asyncio.StreamWriter | None = None
            retry_delay = 0.0
            try:
                self.client._validate_socket()
                reader, writer = await asyncio.open_unix_connection(str(self.client.socket_path))
                request_id = f"agents-events-{uuid.uuid4().hex}"
                writer.write(
                    json.dumps(
                        {"id": request_id, "method": "events.subscribe", "params": {"subscriptions": subscriptions}},
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                first = json.loads(await reader.readline())
                result = first.get("result") if isinstance(first, dict) else None
                if (
                    not isinstance(first, dict)
                    or first.get("id") != request_id
                    or not isinstance(result, dict)
                    or result.get("type") != "subscription_started"
                ):
                    raise ExecutionProtocolError(
                        "subscription_shape", "Herdr returned a malformed subscription response"
                    )
                while line := await reader.readline():
                    event = json.loads(line)
                    if (
                        not isinstance(event, dict)
                        or not isinstance(event.get("event"), str)
                        or not isinstance(event.get("data"), dict)
                    ):
                        raise ExecutionProtocolError("event_shape", "Herdr returned a malformed event")
                    data = event["data"]
                    pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
                    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
                    run_id = data.get("workspace_id") or pane.get("workspace_id") or workspace.get("workspace_id")
                    terminal_id = data.get("pane_id") or pane.get("pane_id")
                    revision = data.get("revision") if isinstance(data.get("revision"), int) else pane.get("revision")
                    yield ExecutionEvent(
                        _EVENT_KIND_MAP.get(str(event["event"]), str(event["event"])),
                        str(run_id) if run_id is not None else None,
                        str(terminal_id) if terminal_id is not None else None,
                        int(revision) if isinstance(revision, int) else None,
                    )
            except asyncio.CancelledError:
                raise
            except ExecutionError, OSError, json.JSONDecodeError:
                retry_delay = 1.0
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(ConnectionError):
                        await writer.wait_closed()
            if retry_delay:
                await asyncio.sleep(retry_delay)
            health = await asyncio.to_thread(self.health)
            if health.healthy:
                await asyncio.to_thread(self.client.snapshot)

    def close(self) -> None:
        self.client.close()
