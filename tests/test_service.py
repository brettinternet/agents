from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.service import (
    ServiceError,
    _close_mapped_workspaces,
    _owned,
    _record,
    acquire_daemon_lock,
    shutdown,
    start,
    stop,
)


def _config(root: Path, session: str | None = "agents-test") -> AgentsConfig:
    return AgentsConfig(
        source=root / "agents.toml",
        root=root,
        project=ProjectConfig("test", root, "main", (("task", "check"),)),
        runtime=RuntimeConfig(5, 1800, 12, 4, 3, 86400),
        execution=ExecutionConfig("herdr", "0.8.2", session, "mock", "mock_cli", (ModelChoice(""),)),
        web=WebConfig("127.0.0.1", 9890),
        actors=(),
    )


class FakeProcess:
    def __init__(self, pid: int = 1000) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class ServiceTests(unittest.TestCase):
    def test_start_reuses_healthy_owned_services_without_launching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.service._owned", return_value=(123, {})),
                patch("agents.service._herdr_health", return_value=True),
                patch("agents.service._web_health_ready", return_value=True),
                patch("agents.service._launch_process") as launch,
            ):
                start(config)
            launch.assert_not_called()

    def test_start_accepts_running_herdr_and_starts_only_agentsd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agentsd = root / ".venv" / "bin" / "agentsd"
            agentsd.parent.mkdir(parents=True)
            agentsd.write_text("#!/bin/sh\n")
            agentsd.chmod(0o755)
            config = _config(root)
            with (
                patch("agents.service._owned", side_effect=(None, (123, {}))),
                patch("agents.service._herdr_health", return_value=True),
                patch("agents.service._web_health_ready", return_value=True),
                patch("agents.service._launch_process", return_value=FakeProcess()) as launch,
            ):
                start(config)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.args[1], "agentsd")

    def test_start_rejects_unhealthy_owned_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.service._owned", return_value=(123, {})),
                patch("agents.service._herdr_health", return_value=False),
                self.assertRaisesRegex(ServiceError, "Herdr.*unhealthy.*server:stop.*server:start"),
            ):
                start(config)

    def test_start_removes_stale_record_and_starts_agentsd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agentsd = root / ".venv" / "bin" / "agentsd"
            agentsd.parent.mkdir(parents=True)
            agentsd.write_text("#!/bin/sh\n")
            agentsd.chmod(0o755)
            config = _config(root)
            config.state_dir.mkdir(mode=0o700)
            record = config.state_dir / "agentsd.pid"
            record.write_text('{"pid": 123, "executable": "/missing", "started": "old"}')
            with (
                patch("agents.service._owned", side_effect=(None, (123, {}))),
                patch("agents.service._herdr_health", return_value=True),
                patch("agents.service._web_health_ready", return_value=True),
                patch("agents.service._launch_process", return_value=FakeProcess()) as launch,
            ):
                start(config)
            self.assertFalse(record.exists())
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[1], "agentsd")

    def test_start_reports_invalid_record_without_removing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            record = config.state_dir / "agentsd.pid"
            record.write_text("{}")
            with self.assertRaisesRegex(
                ServiceError, "cannot validate service ownership: invalid service ownership record"
            ):
                start(config)
            self.assertTrue(record.exists())

    def test_stop_removes_stale_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            record = config.state_dir / "agentsd.pid"
            record.write_text('{"pid": 123, "executable": "/missing", "started": "old"}')
            with patch("agents.service._owned", return_value=None):
                stop(config)
            self.assertFalse(record.exists())

    def test_stop_reports_invalid_record_without_removing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            record = config.state_dir / "agentsd.pid"
            record.write_text("{}")
            with self.assertRaisesRegex(
                ServiceError, "cannot validate agentsd ownership before stopping: invalid service ownership record"
            ):
                stop(config)
            self.assertTrue(record.exists())

    def test_stop_preserves_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with patch("agents.service._stop_named") as stop_named:
                stop(config)
            stop_named.assert_called_once_with(config, "agentsd")

    def test_daemon_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            first = acquire_daemon_lock(state)
            try:
                with self.assertRaises(ServiceError):
                    acquire_daemon_lock(state)
                self.assertEqual((state / "agentsd.lock").stat().st_mode & 0o777, 0o600)
            finally:
                first.close()

    def test_owned_returns_none_when_recorded_process_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / "service.pid"
            pidfile.write_text('{"pid": 123, "executable": "/missing", "started": "old"}')
            with patch("agents.service.os.kill", side_effect=ProcessLookupError):
                self.assertIsNone(_owned(pidfile))

    def test_owned_process_accepts_resolved_symlink_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "sleep"
            executable.symlink_to("/bin/sleep")
            process = subprocess.Popen([str(executable), "30"])
            pidfile = root / "service.pid"
            try:
                _record(pidfile, process, executable)
                owned = _owned(pidfile)
                self.assertIsNotNone(owned)
                record = json.loads(pidfile.read_text())
                record["started"] = "stale process"
                pidfile.write_text(json.dumps(record))
                with self.assertRaises(ServiceError):
                    _owned(pidfile)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_close_workspaces_uses_exact_snapshot_shape_and_confirms_absence(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.snapshots = [
                    {
                        "type": "session_snapshot",
                        "snapshot": {
                            "workspaces": [
                                {"workspace_id": "w1", "label": "agents-test-p-manager-g0001"},
                                {"workspace_id": "foreign", "label": "other-project-p-manager"},
                            ],
                            "panes": [
                                {"workspace_id": "w1", "cwd": "/tmp/project"},
                                {"workspace_id": "foreign", "cwd": "/tmp/project"},
                            ],
                        },
                    },
                    {"type": "session_snapshot", "snapshot": {"workspaces": [], "panes": []}},
                ]
                self.closed: list[dict[str, str]] = []

            def request(self, method: str, params: dict[str, str]) -> dict[str, object]:
                if method == "workspace.close":
                    self.closed.append(params)
                    return {"type": "ok"}
                return self.snapshots.pop(0)

        client = FakeClient()
        self.assertEqual(_close_mapped_workspaces(client, "agents-test-", {"/tmp/project"}), [])
        self.assertEqual(client.closed, [{"workspace_id": "w1"}])

    def test_close_workspaces_refuses_agents_label_with_wrong_cwd(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.closed: list[dict[str, str]] = []

            def request(self, method: str, params: dict[str, str]) -> dict[str, object]:
                if method == "workspace.close":
                    self.closed.append(params)
                    return {"type": "ok"}
                return {
                    "type": "session_snapshot",
                    "snapshot": {
                        "workspaces": [{"workspace_id": "foreign", "label": "agents-test-p-foreign"}],
                        "panes": [{"workspace_id": "foreign", "cwd": "/tmp/foreign"}],
                    },
                }

        client = FakeClient()
        failures = _close_mapped_workspaces(client, "agents-test-", {"/tmp/project"})
        self.assertEqual(client.closed, [])
        self.assertTrue(any("cwd does not match" in failure for failure in failures))

    def test_shutdown_removes_verified_pid_record_only_after_session_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            pid_record = config.state_dir / "herdr.pid"
            pid_record.write_text("owned")
            client = Mock()
            client.request.return_value = {
                "type": "session_snapshot",
                "snapshot": {"workspaces": [], "panes": []},
            }
            events: list[str] = []

            def delete_session(_: AgentsConfig, __: str) -> None:
                self.assertTrue(pid_record.exists())
                events.append("delete")

            with (
                patch("agents.service.stop_agents"),
                patch("agents.service._stop_named", return_value=pid_record) as stop_named,
                patch("agents.service._delete_session", side_effect=delete_session),
            ):
                shutdown(config, client)
            stop_named.assert_called_once_with(config, "herdr", remove_record=False)
            self.assertEqual(events, ["delete"])
            self.assertFalse(pid_record.exists())


if __name__ == "__main__":
    unittest.main()
