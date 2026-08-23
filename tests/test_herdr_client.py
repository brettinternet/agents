from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from agents.execution import (
    ExecutionBusy,
    ExecutionConflict,
    ExecutionProtocolError,
    ExecutionTimeout,
    RunHandle,
    RunSpec,
)
from agents.herdr_client import HerdrBackend, HerdrClient


class UnixResponder:
    def __init__(self, root: Path, handler: Callable[[dict], bytes | tuple[bytes, bytes] | None]) -> None:
        self.path = root / "herdr.sock"
        self.handler = handler
        self.ready = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Path:
        self.thread.start()
        self.assert_ready()
        return self.path

    def assert_ready(self) -> None:
        if not self.ready.wait(2):
            raise RuntimeError("test socket did not start")

    def _serve(self) -> None:
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen()
        self.ready.set()
        connection, _ = self.server.accept()
        with connection:
            request = json.loads(connection.makefile("rb").readline())
            response = self.handler(request)
            if isinstance(response, tuple):
                connection.sendall(response[0])
                time.sleep(0.01)
                connection.sendall(response[1])
            elif response is not None:
                connection.sendall(response)

    def __exit__(self, *_: object) -> None:
        self.server.close()
        self.thread.join(timeout=2)


class HerdrClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def response(request: dict, result: dict) -> bytes:
        return (json.dumps({"id": request["id"], "result": result}) + "\n").encode()

    def test_reads_a_split_ndjson_response_and_negotiates_health(self) -> None:
        def handler(request: dict) -> tuple[bytes, bytes]:
            payload = self.response(
                request,
                {"type": "pong", "version": "0.8.2", "protocol": 20, "capabilities": {"events": True}},
            )
            return payload[:9], payload[9:]

        with UnixResponder(self.root, handler) as path:
            health = HerdrClient(path).health()
        self.assertTrue(health.healthy)
        self.assertEqual((health.version, health.protocol, health.capabilities), ("0.8.2", 20, ("events",)))

    def test_rejects_wrong_ids_and_malformed_json(self) -> None:
        for response, code in (
            (b'{"id":"wrong","result":{"type":"ok"}}\n', "response_id"),
            (b"not-json\n", "malformed_json"),
        ):
            with (
                self.subTest(code=code),
                UnixResponder(self.root, lambda _, response=response: response) as path,
                self.assertRaises(ExecutionProtocolError) as raised,
            ):
                HerdrClient(path).request("ping", {})
                self.assertEqual(raised.exception.code, code)
            if self.root.joinpath("herdr.sock").exists():
                self.root.joinpath("herdr.sock").unlink()

    def test_maps_transient_agent_errors_and_mutating_disconnect(self) -> None:
        for code in ("agent_blocked", "agent_pane_busy"):
            with self.subTest(code=code):

                def blocked(request: dict, code: str = code) -> bytes:
                    return (
                        json.dumps({"id": request["id"], "error": {"code": code, "message": "wait"}}) + "\n"
                    ).encode()

                with UnixResponder(self.root, blocked) as path, self.assertRaises(ExecutionBusy):
                    HerdrClient(path).request("agent.start", {"name": "worker", "pane_id": "w1:p1"})
                self.root.joinpath("herdr.sock").unlink()
        with UnixResponder(self.root, lambda _: None) as path, self.assertRaises(ExecutionTimeout) as raised:
            HerdrClient(path).request("workspace.create", {"cwd": "/tmp"})
        self.assertTrue(raised.exception.outcome_unknown)

    def test_rejects_malformed_snapshot_items(self) -> None:
        def handler(request: dict) -> bytes:
            return self.response(
                request,
                {
                    "type": "session_snapshot",
                    "snapshot": {
                        "version": "0.8.2",
                        "protocol": 20,
                        "workspaces": [{}],
                        "panes": [],
                        "agents": [],
                    },
                },
            )

        with (
            UnixResponder(self.root, handler) as path,
            self.assertRaises(ExecutionProtocolError) as raised,
        ):
            HerdrClient(path).snapshot()
        self.assertEqual(raised.exception.code, "snapshot_shape")

    def test_partial_mutating_send_is_outcome_unknown(self) -> None:
        class PartialSocket:
            def settimeout(self, _: float) -> None:
                return None

            def connect(self, _: str) -> None:
                return None

            def sendall(self, _: bytes) -> None:
                raise OSError("partial write")

            def close(self) -> None:
                return None

        client = HerdrClient(self.root / "unused.sock")
        with (
            patch.object(client, "_validate_socket"),
            patch("agents.herdr_client.socket.socket", return_value=PartialSocket()),
            self.assertRaises(ExecutionTimeout) as raised,
        ):
            client.request("agent.prompt", {"target": "pane", "text": "wake"})
        self.assertTrue(raised.exception.outcome_unknown)

    def test_send_message_returns_socket_request_id(self) -> None:
        seen: list[str] = []

        def handler(request: dict) -> bytes:
            seen.append(str(request["id"]))
            return self.response(request, {"type": "agent_prompted"})

        with UnixResponder(self.root, handler) as path:
            backend = HerdrBackend(HerdrClient(path), provider_id="opencode_cli")
            message_id = backend.send_message(RunHandle("run", "workspace", "pane"), "sender", "wake")
        self.assertEqual(message_id, seen[0])

    def test_get_run_uses_authoritative_pane_get(self) -> None:
        class FakeClient:
            def snapshot(self) -> dict[str, Any]:
                return {
                    "workspaces": [{"workspace_id": "workspace", "label": "run"}],
                    "panes": [{"pane_id": "pane", "workspace_id": "workspace", "cwd": "/stale", "revision": 1}],
                    "agents": [
                        {
                            "pane_id": "pane",
                            "name": "agents-r0000000001-g0001",
                            "agent": "opencode",
                            "agent_status": "idle",
                        }
                    ],
                }

            def request(self, method: str, params: dict[str, str]) -> dict[str, Any]:
                self.method = method
                self.params = params
                return {
                    "type": "pane_info",
                    "pane": {
                        "pane_id": "pane",
                        "workspace_id": "workspace",
                        "cwd": "/expected",
                        "revision": 2,
                    },
                }

        client = FakeClient()
        backend = HerdrBackend(cast(Any, client), provider_id="opencode_cli")
        run = backend.get_run(RunHandle("run", "workspace", "pane"))
        self.assertEqual((client.method, client.params), ("pane.get", {"pane_id": "pane"}))
        self.assertEqual((run.cwd, run.revision), (Path("/expected"), 2))

    def test_duplicate_exact_labels_fail_closed(self) -> None:
        class FakeClient:
            def snapshot(self) -> dict:
                return {
                    "workspaces": [
                        {"workspace_id": "w1", "label": "agents-test", "cwd": "/tmp"},
                        {"workspace_id": "w2", "label": "agents-test", "cwd": "/tmp"},
                    ],
                    "panes": [
                        {"pane_id": "p1", "workspace_id": "w1", "cwd": "/tmp", "revision": 1},
                        {"pane_id": "p2", "workspace_id": "w2", "cwd": "/tmp", "revision": 1},
                    ],
                    "agents": [],
                }

        backend = HerdrBackend(FakeClient(), provider_id="mock_cli")  # type: ignore[arg-type]
        with self.assertRaises(ExecutionConflict) as raised:
            backend.find_run("agents-test")
        self.assertEqual(raised.exception.code, "duplicate_label")

    def test_create_run_keeps_authoritative_created_ids_despite_duplicate_label(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.snapshots = 0
                self.expected_after = 3

            def snapshot(self) -> dict[str, Any]:
                self.snapshots += 1
                if self.snapshots == 1:
                    return {"workspaces": [], "panes": [], "agents": []}
                return {
                    "workspaces": [
                        {"workspace_id": "w1", "label": "agents-test"},
                        {"workspace_id": "w2", "label": "agents-test"},
                    ],
                    "panes": [
                        {"pane_id": "p1", "workspace_id": "w1", "cwd": "/tmp", "revision": 2},
                        {"pane_id": "p2", "workspace_id": "w2", "cwd": "/tmp", "revision": 1},
                    ],
                    "agents": (
                        []
                        if self.snapshots < self.expected_after
                        else [
                            {"pane_id": "p1", "name": "worker", "agent": "opencode", "agent_status": "idle"},
                            {"pane_id": "p2", "name": "other", "agent": "opencode", "agent_status": "idle"},
                        ]
                    ),
                }

            def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
                if method == "workspace.create":
                    return {
                        "type": "workspace_created",
                        "workspace": {"workspace_id": "w1", "label": "agents-test"},
                        "tab": {"tab_id": "t1"},
                        "root_pane": {"pane_id": "p1", "workspace_id": "w1"},
                    }
                if method == "agent.start":
                    return {
                        "type": "agent_started",
                        "agent": {"workspace_id": "w1", "pane_id": "p1"},
                        "argv": ["opencode"],
                    }
                if method == "pane.get":
                    return {
                        "type": "pane_info",
                        "pane": {"pane_id": params["pane_id"], "workspace_id": "w1", "cwd": "/tmp", "revision": 2},
                    }
                raise AssertionError(method)

        spec = RunSpec("agents-test", 1, 1, Path("/tmp"), "worker", "opencode", ("opencode",), (), "opencode")
        with patch("agents.herdr_client.time.sleep") as sleep:
            run = HerdrBackend(cast(Any, FakeClient()), provider_id="opencode_cli").create_run(spec)
        sleep.assert_called_once_with(0.1)
        self.assertEqual(run.handle, RunHandle("agents-test", "w1", "p1"))
        stale = FakeClient()
        stale.expected_after = 999
        with (
            patch("agents.herdr_client.time.sleep") as sleep,
            self.assertRaises(ExecutionConflict) as raised,
        ):
            HerdrBackend(cast(Any, stale), provider_id="opencode_cli").create_run(spec)
        self.assertEqual(raised.exception.code, "agent_start_mismatch")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.1, 0.4, 1.0, 2.0, 3.0, 4.0])

    def test_create_run_rejects_missing_authoritative_workspace_ids(self) -> None:
        class FakeClient:
            def snapshot(self) -> dict[str, Any]:
                return {"workspaces": [], "panes": [], "agents": []}

            def request(self, method: str, _: dict[str, Any]) -> dict[str, Any]:
                if method != "workspace.create":
                    raise AssertionError(method)
                return {
                    "type": "workspace_created",
                    "workspace": {"label": "agents-test"},
                    "tab": {"tab_id": "t1"},
                    "root_pane": {"pane_id": "p1"},
                }

        spec = RunSpec("agents-test", 1, 1, Path("/tmp"), "worker", "opencode", ("opencode",), (), "opencode")
        with self.assertRaises(ExecutionProtocolError) as raised:
            HerdrBackend(cast(Any, FakeClient()), provider_id="opencode_cli").create_run(spec)
        self.assertEqual(raised.exception.code, "workspace_create_shape")


class HerdrEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_eof_reconnects_and_preserves_nested_pane_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "herdr.sock"
            subscriptions = 0

            async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                nonlocal subscriptions
                request = json.loads(await reader.readline())
                method = request["method"]
                if method == "events.subscribe":
                    subscriptions += 1
                    result = {"type": "subscription_started"}
                    response = {"id": request["id"], "result": result}
                    writer.write((json.dumps(response) + "\n").encode())
                    event = {
                        "event": "pane_updated",
                        "data": {
                            "type": "pane_updated",
                            "pane": {
                                "workspace_id": "workspace-1",
                                "pane_id": "pane-1",
                                "revision": subscriptions,
                            },
                        },
                    }
                    writer.write((json.dumps(event) + "\n").encode())
                elif method == "ping":
                    result = {"type": "pong", "version": "0.8.2", "protocol": 20, "capabilities": {}}
                    writer.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
                else:
                    snapshot = {
                        "version": "0.8.2",
                        "protocol": 20,
                        "workspaces": [],
                        "tabs": [],
                        "panes": [],
                        "layouts": [],
                        "agents": [],
                    }
                    result = {"type": "session_snapshot", "snapshot": snapshot}
                    writer.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(respond, path=str(path))
            os.chmod(path, 0o600)
            backend = HerdrBackend(HerdrClient(path), provider_id="opencode_cli")
            events = backend.events()
            try:
                first = await asyncio.wait_for(anext(events), 2)
                second = await asyncio.wait_for(anext(events), 3)
            finally:
                await cast(Any, events).aclose()
                server.close()
                await server.wait_closed()
            self.assertEqual((first.kind, second.kind), ("pane.updated", "pane.updated"))
            self.assertEqual(
                (first.run_id, first.terminal_id, first.revision),
                ("workspace-1", "pane-1", 1),
            )
            self.assertEqual(
                (second.run_id, second.terminal_id, second.revision),
                ("workspace-1", "pane-1", 2),
            )
            self.assertEqual(subscriptions, 2)


if __name__ == "__main__":
    unittest.main()
