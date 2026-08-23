from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class ExecutionStatus(StrEnum):
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING_USER_ANSWER = "waiting_user_answer"
    COMPLETED = "completed"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RunSpec:
    name: str
    terminal_run_id: int
    generation: int
    cwd: Path
    agent_name: str
    agent_kind: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    provider: str
    mock: bool = False
    container_image_id: str = ""

    @property
    def environment(self) -> Mapping[str, str]:
        return dict(self.env)


@dataclass(frozen=True)
class RunHandle:
    name: str
    run_id: str
    terminal_id: str


@dataclass(frozen=True)
class RunSnapshot:
    handle: RunHandle
    status: ExecutionStatus
    backend_name: str
    cwd: Path
    agent_name: str | None = None
    agent_kind: str | None = None
    revision: int | None = None
    output: str = ""
    output_truncated: bool = False
    terminated: bool = False


@dataclass(frozen=True)
class BackendHealth:
    healthy: bool
    version: str
    protocol: int | None = None
    supports_events: bool = False
    capabilities: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ExecutionEvent:
    kind: str
    run_id: str | None = None
    terminal_id: str | None = None
    revision: int | None = None


class ExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        outcome_unknown: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        super().__init__(f"{code}: {message}")


class ExecutionUnavailable(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=True)


class ExecutionTimeout(ExecutionError):
    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code, message, retryable=True, outcome_unknown=outcome_unknown)


class ExecutionNotFound(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


class ExecutionProtocolError(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


class ExecutionRejected(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


class ExecutionUnauthorized(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


class ExecutionConflict(ExecutionError):
    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code, message, retryable=False, outcome_unknown=outcome_unknown)


class ExecutionBusy(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=True)


class ExecutionTerminated(ExecutionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


class ExecutionBackend(Protocol):
    def health(self) -> BackendHealth: ...

    def create_run(self, spec: RunSpec) -> RunSnapshot: ...

    def find_run(self, name: str) -> RunSnapshot | None: ...

    def get_run(self, handle: RunHandle, *, include_output: bool = False) -> RunSnapshot: ...

    def list_runs(self, prefix: str) -> tuple[RunSnapshot, ...]: ...

    def send_message(self, handle: RunHandle, sender_id: str, message: str) -> str: ...

    def send_input(self, handle: RunHandle, message: str) -> None: ...

    def delete_run(self, handle: RunHandle) -> None: ...

    def close(self) -> None: ...

    def events(self) -> AsyncIterator[ExecutionEvent]: ...
