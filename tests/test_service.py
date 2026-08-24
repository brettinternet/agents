from __future__ import annotations

import errno
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

from agents.config import (
    AgentsConfig,
    ContainerConfig,
    ExecutionConfig,
    IsolationMode,
    ModelChoice,
    ProjectConfig,
    RuntimeConfig,
    WebConfig,
)
from agents.service import (
    ServiceError,
    _acquire_daemon_lock_after_stop,
    _agents_environment,
    _close_mapped_workspaces,
    _foreground_agents_environment,
    _herdr_environment,
    _owned,
    _record,
    _stop_named,
    _wait_for_exit,
    acquire_daemon_lock,
    foreground,
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

    def test_start_host_mode_does_not_probe_colima_when_container_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config = replace(
                config,
                execution=replace(
                    config.execution,
                    container=ContainerConfig("agents", "agents:test", 1, 512, 64, 60, 60, 24),
                ),
            )
            config.state_dir.mkdir(mode=0o700)
            config.db_path.touch()
            with (
                patch("agents.service._owned", return_value=(123, {})),
                patch("agents.service._herdr_health", return_value=True),
                patch("agents.service._web_health_ready", return_value=True),
                patch("agents.service.shutil.which", return_value="/usr/local/bin/tool"),
                patch("agents.container_runtime.ContainerRuntime") as runtime,
            ):
                start(config)
            runtime.assert_not_called()

    def test_start_rejects_dangling_whole_system_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            (config.state_dir / "container-topology.json").symlink_to(config.state_dir / "missing")
            with self.assertRaisesRegex(ServiceError, "Compose topology is owned"):
                start(config)

    def test_start_container_mode_requires_runtime_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config = replace(
                config,
                execution=replace(
                    config.execution,
                    isolation=IsolationMode.CONTAINER,
                    container=ContainerConfig("agents", "agents:test", 1, 512, 64, 60, 60, 24),
                ),
            )
            config.state_dir.mkdir(mode=0o700)
            connection = sqlite3.connect(config.db_path)
            connection.execute("CREATE TABLE project(id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL)")
            connection.execute("INSERT INTO project VALUES(1,'instance')")
            connection.commit()
            connection.close()
            runtime = Mock()
            runtime.profile_state.return_value = "Running"
            runtime.docker.return_value = ""
            with (
                patch("agents.container_runtime.ContainerRuntime", return_value=runtime),
                patch("agents.service._owned", return_value=(123, {})),
                patch("agents.service._herdr_health", return_value=True),
                patch("agents.service._web_health_ready", return_value=True),
            ):
                start(config)
            runtime.initialize.assert_called_once_with(config.root, "instance", config.web.port)
            runtime.resolve_image_id.assert_called_once_with("agents:test")

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
                patch("agents.service._port_free", return_value=True),
                patch("agents.service._launch_process", return_value=FakeProcess()) as launch,
            ):
                start(config)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.args[1], "agentsd")

    def test_service_environments_keep_credentials_only_for_agentsd(self) -> None:
        config = _config(Path("/tmp/project"))
        environment = {
            "OPENCODE_AUTH_JSON": '{"token":"secret"}',
            "AGENTS_TOPOLOGY": "compose",
            "AGENTS_BROKER_PORT": "9891",
            "AGENTS_BROKER_SECRETS_ROOT": "/private/broker",
            "AGENTS_PROVIDER_AUTH_FILE": "/run/agents-secrets/provider-auth",
            "AGENTS_SYSTEM_CONTAINER": "1",
            "AGENTS_SECRETS_TRANSPORT": "agent-api",
        }
        with patch.dict("os.environ", environment, clear=True):
            agents = _agents_environment(config)
            herdr = _herdr_environment(config)
            foreground_agents = _foreground_agents_environment(config)
        self.assertEqual(agents["OPENCODE_AUTH_JSON"], '{"token":"secret"}')
        self.assertNotIn("OPENCODE_AUTH_JSON", herdr)
        self.assertEqual(agents["AGENTS_BROKER_SECRETS_ROOT"], "/private/broker")
        self.assertNotIn("AGENTS_BROKER_SECRETS_ROOT", herdr)
        for name in ("AGENTS_TOPOLOGY", "AGENTS_BROKER_PORT", "AGENTS_SECRETS_TRANSPORT"):
            self.assertNotIn(name, agents)
        self.assertEqual(foreground_agents["AGENTS_SYSTEM_CONTAINER"], "1")
        self.assertEqual(foreground_agents["AGENTS_BROKER_PORT"], "9891")
        self.assertEqual(foreground_agents["AGENTS_SECRETS_TRANSPORT"], "agent-api")
        for name in ("OPENCODE_AUTH_JSON", "AGENTS_PROVIDER_AUTH_FILE"):
            self.assertNotIn(name, foreground_agents)

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
                patch("agents.service._port_free", return_value=True),
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

    def test_stop_signals_verified_owned_process_before_removing_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            record = config.state_dir / "agentsd.pid"
            record.write_text('{"pid": 123, "executable": "/owned", "started": "now"}')
            with (
                patch("agents.service._owned", return_value=(123, {})),
                patch("agents.service.os.killpg") as kill_group,
                patch("agents.service.os.kill", side_effect=ProcessLookupError),
            ):
                stop(config)
            kill_group.assert_called_once_with(123, signal.SIGTERM)
            self.assertFalse(record.exists())

    def test_foreground_removes_provider_credential_when_setup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            auth = root / "provider-auth"
            auth.write_text("{}")
            auth.chmod(0o600)
            config = _config(root)
            config = replace(
                config,
                execution=replace(config.execution, provider="opencode", provider_id="opencode_cli"),
            )
            provider_path = home / ".local/share/opencode/auth.json"
            with (
                patch.dict("os.environ", {"AGENTS_PROVIDER_AUTH_FILE": str(auth)}, clear=False),
                patch("agents.service.Path.home", return_value=home),
                patch("agents.service._write_herdr_config", side_effect=ServiceError("injected setup failure")),
                self.assertRaisesRegex(ServiceError, "injected setup failure"),
            ):
                foreground(config)
            self.assertFalse(provider_path.exists())

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

    def test_acquire_daemon_lock_error_names_path_and_errno(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            first = acquire_daemon_lock(state)
            try:
                with self.assertRaisesRegex(
                    ServiceError, r"agentsd\.lock is already locked by another process \(errno \d+"
                ):
                    acquire_daemon_lock(state)
            finally:
                first.close()

    def test_acquire_daemon_lock_after_stop_retries_transient_contention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            blocker = acquire_daemon_lock(state)

            def release_soon() -> None:
                time.sleep(0.2)
                blocker.close()

            releaser = threading.Thread(target=release_soon)
            releaser.start()
            try:
                handle = _acquire_daemon_lock_after_stop(state, timeout=2.0, interval=0.05)
                handle.close()
            finally:
                releaser.join()

    def test_acquire_daemon_lock_after_stop_gives_up_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            holder = acquire_daemon_lock(state)
            try:
                with self.assertRaises(ServiceError):
                    _acquire_daemon_lock_after_stop(state, timeout=0.2, interval=0.05)
            finally:
                holder.close()

    def test_acquire_daemon_lock_after_stop_does_not_retry_non_lock_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with (
                patch("agents.service.fcntl.flock", side_effect=OSError(errno.EACCES, "Permission denied")),
                patch("agents.service.time.sleep") as sleep,
                self.assertRaisesRegex(ServiceError, "Permission denied"),
            ):
                _acquire_daemon_lock_after_stop(state, timeout=2.0, interval=0.05)
            sleep.assert_not_called()

    def test_wait_for_exit_reaps_a_child_process(self) -> None:
        process = subprocess.Popen(["/usr/bin/true"])
        self.assertTrue(_wait_for_exit(process.pid, 2))
        process.returncode = 0
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def test_stop_raises_when_process_survives_sigterm_and_sigkill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            config.state_dir.mkdir(mode=0o700)
            record = config.state_dir / "agentsd.pid"
            record.write_text('{"pid": 123, "executable": "/owned", "started": "now"}')
            with (
                patch("agents.service._owned", return_value=(123, {})),
                patch("agents.service.os.killpg") as kill_group,
                patch("agents.service._wait_for_exit", return_value=False),
                self.assertRaisesRegex(ServiceError, "did not exit after SIGTERM and SIGKILL"),
            ):
                _stop_named(config, "agentsd")
            self.assertEqual(kill_group.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
            self.assertTrue(record.exists())

    def test_owned_rejects_malformed_field_types_before_inspecting_process(self) -> None:
        invalid_records = (
            {"pid": "123", "executable": "/missing", "started": "old"},
            {"pid": 123, "executable": "", "started": "old"},
            {"pid": 123, "executable": "/missing", "started": ""},
        )
        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / "service.pid"
            for record in invalid_records:
                with self.subTest(record=record):
                    pidfile.write_text(json.dumps(record))
                    with (
                        patch("agents.service.os.kill") as inspect,
                        self.assertRaisesRegex(ServiceError, "invalid service ownership record"),
                    ):
                        _owned(pidfile)
                    inspect.assert_not_called()

    def test_owned_reports_out_of_range_pid_as_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / "service.pid"
            pidfile.write_text('{"pid": 10000000000, "executable": "/missing", "started": "old"}')
            with (
                patch("agents.service.os.kill", side_effect=OverflowError),
                self.assertRaisesRegex(ServiceError, "invalid service ownership record.*supported range"),
            ):
                _owned(pidfile)

    def test_owned_reports_non_utf8_record_as_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / "service.pid"
            pidfile.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ServiceError, "invalid service ownership record.*not UTF-8"):
                _owned(pidfile)

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
