from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import IO, cast
from unittest.mock import patch

from agents.cao_client import CaoNotFound
from agents.config import AgentsConfig, CaoConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import connect, migrate, utc_now
from agents.service import ServiceError, _owned, _record, acquire_daemon_lock, shutdown, start


class ServiceTests(unittest.TestCase):
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

    def test_cao_launch_forces_loopback_api_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cao = root / ".tools" / "bin" / "cao-server"
            agentsd = root / ".venv" / "bin" / "agentsd"
            cao.parent.mkdir(parents=True)
            agentsd.parent.mkdir(parents=True)
            for executable in (cao, agentsd):
                executable.write_text("#!/bin/sh\n")
                executable.chmod(0o755)
            config = AgentsConfig(
                source=root / "agents.toml",
                root=root,
                project=ProjectConfig("test", root, "main", (("task", "check"),)),
                runtime=RuntimeConfig(5, 1800, 12, 4, 3, 86400),
                cao=CaoConfig("2.4.1", "mock", "mock_cli", 9889, (ModelChoice(""),)),
                web=WebConfig("127.0.0.1", 9890),
                actors=(),
            )
            launches: list[tuple[list[str], dict[str, object]]] = []

            class FakeProcess:
                def __init__(self, pid: int) -> None:
                    self.pid = pid

                def poll(self) -> None:
                    return None

            def fake_popen(args: list[str], **kwargs: object) -> FakeProcess:
                stream = cast(IO[bytes] | None, kwargs.get("stdout"))
                if stream is not None:
                    stream.close()
                launches.append((args, kwargs))
                return FakeProcess(1000 + len(launches))

            port_checks = 0

            def fake_port_free(_: str, __: int) -> bool:
                nonlocal port_checks
                port_checks += 1
                return port_checks <= 2

            with (
                patch.dict("os.environ", {"CAO_API_HOST": "0.0.0.0"}),
                patch("agents.service.subprocess.Popen", side_effect=fake_popen),
                patch("agents.service._record"),
                patch("agents.service._port_free", side_effect=fake_port_free),
                patch("agents.service._health_ready", return_value=True),
            ):
                start(config)

            self.assertEqual(launches[0][0][0], str(cao))
            cao_environment = launches[0][1]["env"]
            assert isinstance(cao_environment, dict)
            self.assertEqual(cao_environment["CAO_API_HOST"], "127.0.0.1")

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
                assert owned is not None
                self.assertEqual(owned[0], process.pid)
                record = json.loads(pidfile.read_text())
                record["started"] = "stale process"
                pidfile.write_text(json.dumps(record))
                with self.assertRaises(ServiceError):
                    _owned(pidfile)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_shutdown_quiesces_agents_before_snapshot_and_cao(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".agents"
            state.mkdir(mode=0o700)
            (state / "agent-auth-key").write_text("6b" * 32)
            (state / "agent-auth-key").chmod(0o600)
            database = state / "agents.db"
            connection = connect(database)
            migrate(connection)
            now = utc_now()
            connection.execute(
                "INSERT INTO project(id,instance_id,name,canonical_path,git_common_dir,default_branch,"
                "verify_json,next_work_seq,created_at,updated_at) VALUES(1,?,?,?,?,?,?,1,?,?)",
                ("12345678", "test", str(root), str(root / ".git"), "main", '[["task","check"]]', now, now),
            )
            connection.execute(
                "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at)"
                "VALUES('elder','agent',1,1,?,?)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO terminal_runs(session_name,profile_name,mcp_name,profile_sha256,provider,model,"
                "generation,actor_slug,purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,"
                "status,output_digest,output_tail,digest_since,launch_count,created_at,updated_at)"
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "cao-agents-12345678-p-elder-g0001",
                    "reserved",
                    "reserved",
                    "",
                    "mock_cli",
                    "",
                    1,
                    "elder",
                    "persistent",
                    "elder",
                    str(root),
                    "digest",
                    "reserved",
                    "live",
                    "idle",
                    "",
                    "",
                    now,
                    0,
                    now,
                    now,
                ),
            )
            connection.commit()
            connection.close()
            config = AgentsConfig(
                source=root / "agents.toml",
                root=root,
                project=ProjectConfig("test", root, "main", (("task", "check"),)),
                runtime=RuntimeConfig(5, 1800, 12, 4, 3, 86400),
                cao=CaoConfig("2.4.1", "mock", "mock_cli", 9889, (ModelChoice(""),)),
                web=WebConfig("127.0.0.1", 9890),
                actors=(),
            )
            events: list[str] = []
            original_connect = connect

            class FakeCao:
                def list_sessions(self) -> list[dict[str, str]]:
                    events.append("list")
                    return [
                        {"name": "cao-agents-12345678-p-orphan"},
                        {"name": "cao-agents-99999999-p-foreign"},
                    ]

                def delete_session(self, name: str) -> None:
                    events.append(f"delete:{name}")
                    check = original_connect(database)
                    try:
                        row = check.execute(
                            "SELECT token_revoked_at,state FROM terminal_runs "
                            "WHERE session_name='cao-agents-12345678-p-elder-g0001'"
                        ).fetchone()
                        assert row is not None
                        if row["token_revoked_at"] is None or row["state"] != "ending":
                            raise AssertionError("CAO deletion ran before durable shutdown fence")
                    finally:
                        check.close()

                def get_session(self, name: str) -> dict[str, str]:
                    raise CaoNotFound(name)

                def close(self) -> None:
                    events.append("close")

            def stop_agents_first(_: AgentsConfig) -> None:
                events.append("agents")

            def stop_cao_last(_: AgentsConfig) -> None:
                events.append("cao")

            def guarded_connect(path: Path):
                if events != ["agents"]:
                    raise AssertionError("shutdown opened state before stopping Agents")
                return original_connect(path)

            with (
                patch("agents.service.stop_agents", side_effect=stop_agents_first),
                patch("agents.service.stop_cao", side_effect=stop_cao_last),
                patch("agents.db.connect", side_effect=guarded_connect),
            ):
                shutdown(config, FakeCao())

            self.assertEqual(
                events,
                [
                    "agents",
                    "list",
                    "delete:cao-agents-12345678-p-elder-g0001",
                    "delete:cao-agents-12345678-p-orphan",
                    "close",
                    "cao",
                ],
            )
            verify = original_connect(database)
            try:
                row = verify.execute(
                    "SELECT token_revoked_at,state FROM terminal_runs WHERE session_name='cao-agents-12345678-p-elder-g0001'"
                ).fetchone()
                self.assertIsNotNone(row["token_revoked_at"])
                self.assertEqual(row["state"], "ended")
            finally:
                verify.close()
