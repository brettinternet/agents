from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx


class CaoUnavailable(RuntimeError):
    pass


class CaoNotFound(CaoUnavailable):
    pass


@dataclass
class CaoClient:
    port: int
    timeout: float = 5.0
    client: httpx.Client | None = None
    _owned_client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._owned_client = self.client or httpx.Client(base_url=self.base_url, timeout=self.timeout)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        if self.client is None:
            self._owned_client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            response = self._owned_client.request(
                method, path, params=params, json=json_body, timeout=timeout or self.timeout
            )
            if response.status_code == 404:
                raise CaoNotFound(path)
            response.raise_for_status()
            return response.json() if response.content else None
        except CaoNotFound:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise CaoUnavailable(str(exc)) from exc

    def health(self) -> bool:
        try:
            return self._owned_client.get("/health", timeout=2.0).is_success
        except httpx.HTTPError:
            return False

    def openapi(self) -> dict[str, Any]:
        return self._object(self._request("GET", "/openapi.json"), "OpenAPI")

    def create_session(
        self,
        *,
        profile: str,
        provider: str,
        session_name: str,
        working_directory: str,
        allowed_tools: list[str],
        env_vars: dict[str, str],
        model: str = "",
    ) -> dict[str, Any]:
        params = {
            "agent_profile": profile,
            "provider": provider,
            "session_name": session_name,
            "working_directory": working_directory,
            "allowed_tools": ",".join(allowed_tools),
        }
        if model:
            params["model"] = model
        value = self._request("POST", "/sessions", params=params, json_body={"env_vars": env_vars}, timeout=120.0)
        return self._object(value, "create session")

    def get_session(self, name: str) -> dict[str, Any]:
        return self._object(self._request("GET", f"/sessions/{quote(name, safe='')}"), "session")

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._objects(self._request("GET", "/sessions"), "sessions")

    def list_terminals(self, session_name: str) -> list[dict[str, Any]]:
        return self._objects(
            self._request("GET", f"/sessions/{quote(session_name, safe='')}/terminals"),
            "terminals",
        )

    def get_terminal(self, terminal_id: str) -> dict[str, Any]:
        return self._object(self._request("GET", f"/terminals/{quote(terminal_id, safe='')}"), "terminal")

    def get_working_directory(self, terminal_id: str) -> str:
        value = self._object(
            self._request("GET", f"/terminals/{quote(terminal_id, safe='')}/working-directory"),
            "working directory",
        )
        directory = value.get("working_directory")
        if not isinstance(directory, str):
            raise CaoUnavailable("working-directory response is invalid")
        return directory

    def get_output(self, terminal_id: str) -> str:
        value = self._object(
            self._request(
                "GET",
                f"/terminals/{quote(terminal_id, safe='')}/output",
                params={"mode": "full"},
            ),
            "output",
        )
        output = value.get("output")
        if not isinstance(output, str):
            raise CaoUnavailable("terminal output response is invalid")
        return output

    def enqueue_wake(self, terminal_id: str, sender_id: str, message: str) -> str:
        value = self._object(
            self._request(
                "POST",
                f"/terminals/{quote(terminal_id, safe='')}/inbox/messages",
                params={"sender_id": sender_id, "message": message},
            ),
            "wake",
        )
        message_id = value.get("message_id")
        if not isinstance(message_id, (str, int)):
            raise CaoUnavailable("wake response has no message_id")
        return str(message_id)

    def send_input(self, terminal_id: str, message: str) -> bool:
        value = self._object(
            self._request(
                "POST",
                f"/terminals/{quote(terminal_id, safe='')}/input",
                params={"message": message},
            ),
            "terminal input",
        )
        success = value.get("success")
        if not isinstance(success, bool):
            raise CaoUnavailable("terminal input response is invalid")
        return success

    def delete_session(self, name: str) -> None:
        self._request("DELETE", f"/sessions/{quote(name, safe='')}")

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CaoUnavailable(f"CAO {label} response is not an object")
        return value

    @staticmethod
    def _objects(value: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise CaoUnavailable(f"CAO {label} response is not an array of objects")
        return value
