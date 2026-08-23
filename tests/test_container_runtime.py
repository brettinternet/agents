from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, MagicMock, call, patch

from agents.config import AgentsConfig, ContainerConfig, IsolationMode
from agents.container_runner import _write_secret
from agents.container_runner import run as run_container
from agents.container_runtime import (
    ContainerGarbageCollector,
    ContainerizedHerdrBackend,
    ContainerRuntime,
    ContainerRuntimeError,
    _completed,
)
from agents.execution import ExecutionConflict, RunHandle


class ContainerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.container = ContainerConfig("agents", "image:local", 2.0, 4096, 512, 3600, 3600, 168)
        self.config = cast(
            AgentsConfig,
            SimpleNamespace(
                root=self.root,
                state_dir=self.root / ".agents",
                db_path=self.root / ".agents/agents.db",
                execution=SimpleNamespace(container=self.container, isolation=IsolationMode.CONTAINER),
            ),
        )
        self.inner = MagicMock()
        self.runtime = MagicMock()
        with patch("agents.container_runtime._instance_id", return_value="instance"):
            self.backend = ContainerizedHerdrBackend(self.config, self.inner, self.runtime)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _identity(self) -> tuple[dict[str, object], dict[str, object]]:
        cwd = str((self.root / "clone").resolve())
        runtime_dir = str((self.root / ".agents/runtime/auth").resolve())
        labels = {
            "dev.agents.instance": "instance",
            "dev.agents.execution": "execution",
            "dev.agents.run_id": "1",
            "dev.agents.generation": "2",
            "dev.agents.cwd_sha256": hashlib.sha256(cwd.encode()).hexdigest(),
            "dev.agents.image_id": "sha256:image",
            "dev.agents.retention": "ephemeral",
        }
        manifest = {
            "execution_name": "execution",
            "container_name": "agents-instance-r1-g2",
            "image_id": "sha256:image",
            "cwd": cwd,
            "runtime_dir": runtime_dir,
            "user": f"{os.getuid()}:{os.getgid()}",
            "labels": labels,
        }
        inspect = {
            "Id": "sha256:container",
            "Image": "sha256:image",
            "Config": {"User": f"{os.getuid()}:{os.getgid()}", "WorkingDir": cwd, "Labels": labels},
            "HostConfig": {
                "ReadonlyRootfs": True,
                "PidsLimit": 512,
                "NanoCpus": 2_000_000_000,
                "Memory": 4096 * 1024 * 1024,
                "NetworkMode": "agents-runs",
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev", "/run": "rw,noexec,nosuid,nodev"},
                "Privileged": False,
                "Devices": [],
                "DeviceRequests": [],
            },
            "Mounts": [
                {"Type": "bind", "Destination": cwd, "Source": cwd, "RW": True},
                {"Type": "bind", "Destination": runtime_dir, "Source": runtime_dir, "RW": True},
                {"Type": "tmpfs", "Destination": "/tmp", "Source": "", "RW": True},
                {"Type": "tmpfs", "Destination": "/run", "Source": "", "RW": True},
            ],
        }
        return manifest, inspect

    def test_colima_version_is_exact_and_missing_binary_is_actionable(self) -> None:
        runtime = ContainerRuntime(self.container)
        with patch(
            "agents.container_runtime._completed",
            return_value=SimpleNamespace(stdout="colima version 0.10.3\ngit commit: fixture\n"),
        ):
            runtime.validate_colima_version()
        with (
            patch(
                "agents.container_runtime._completed",
                return_value=SimpleNamespace(stdout="colima version 0.10.2\n"),
            ),
            self.assertRaisesRegex(ContainerRuntimeError, "Colima 0.10.3.*mise install"),
        ):
            runtime.validate_colima_version()
        with (
            patch("agents.container_runtime.subprocess.run", side_effect=FileNotFoundError(2, "missing")),
            self.assertRaisesRegex(ContainerRuntimeError, "colima is unavailable"),
        ):
            _completed(("colima", "version"))

    def test_wrapper_directory_symlink_is_rejected(self) -> None:
        runtime_root = self.config.state_dir / "runtime"
        runtime_root.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (runtime_root / "bin").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ExecutionConflict, "runtime root is unsafe|wrapper directory is unsafe"):
            self.backend._prepare_wrappers()

    def test_docker_environment_ignores_active_context(self) -> None:
        runtime = ContainerRuntime(self.container)
        with (
            patch.object(runtime, "status", return_value={"docker_socket": "/tmp/agents.sock"}),
            patch.dict(os.environ, {"DOCKER_CONTEXT": "desktop-linux", "UNCHANGED": "yes"}, clear=True),
        ):
            environment = runtime.docker_environment()
        self.assertNotIn("DOCKER_CONTEXT", environment)
        self.assertEqual(environment["DOCKER_HOST"], "unix:///tmp/agents.sock")
        self.assertEqual(environment["UNCHANGED"], "yes")

    def test_initialize_starts_existing_stopped_profile_before_validation(self) -> None:
        runtime = ContainerRuntime(self.container)
        status = {
            "driver": "macOS Virtualization.Framework",
            "arch": "aarch64",
            "runtime": "docker",
            "mount_type": "virtiofs",
            "kubernetes": False,
            "docker_socket": "/tmp/agents.sock",
        }
        with (
            patch.object(runtime, "validate_colima_version"),
            patch.object(runtime, "profile_state", return_value="Stopped"),
            patch.object(runtime, "status", return_value=status),
            patch.object(runtime, "_ssh", return_value=SimpleNamespace(returncode=1, stdout="")),
            patch("agents.container_runtime._completed") as completed,
            self.assertRaisesRegex(ContainerRuntimeError, "does not mount"),
        ):
            runtime.initialize(self.root, "instance", 9890)
        completed.assert_called_once_with(
            (
                "colima",
                "--profile",
                "agents",
                "start",
                "--activate=false",
                "--ssh-agent=false",
                "--network-address=false",
                "--save-config=false",
            )
        )

    def test_secret_writer_rejects_dangling_symlink(self) -> None:
        provider = self.root / "provider"
        provider.mkdir()
        destination = self.root / "outside-secret"
        secret_path = provider / "auth.json"
        secret_path.symlink_to(destination)
        with self.assertRaisesRegex(ContainerRuntimeError, "unsafe credential path"):
            _write_secret(secret_path, "secret")
        self.assertFalse(destination.exists())

    def test_identity_verification_accepts_only_exact_hardened_container(self) -> None:
        manifest, inspect = self._identity()
        self.runtime.inspect_container.return_value = inspect
        self.assertIs(self.backend._verify(manifest), inspect)

        mutations = (
            ("Image", "sha256:wrong"),
            ("Config.User", "0:0"),
            ("Config.WorkingDir", "/wrong"),
            ("HostConfig.ReadonlyRootfs", False),
            ("HostConfig.PidsLimit", 1),
            ("HostConfig.NanoCpus", 1),
            ("HostConfig.Memory", 1),
            ("HostConfig.NetworkMode", "bridge"),
            ("HostConfig.CapDrop", []),
            ("HostConfig.SecurityOpt", []),
            ("HostConfig.SecurityOpt", ["no-new-privileges", "seccomp=/custom/profile"]),
            ("HostConfig.Tmpfs", {}),
            ("HostConfig.Privileged", True),
            ("HostConfig.Devices", [{"PathOnHost": "/dev/null"}]),
            ("Config.Labels", {}),
            ("Mounts", []),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                changed = json.loads(json.dumps(inspect))
                target = changed
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                self.runtime.inspect_container.return_value = changed
                with self.assertRaises(ExecutionConflict):
                    self.backend._verify(manifest)
        changed = json.loads(json.dumps(inspect))
        changed["Mounts"].append(
            {"Type": "bind", "Destination": "/var/run/docker.sock", "Source": "/var/run/docker.sock", "RW": True}
        )
        self.runtime.inspect_container.return_value = changed
        with self.assertRaises(ExecutionConflict):
            self.backend._verify(manifest)
        for key, value in (
            ("container_name", "agents-instance-r9-g2"),
            ("execution_name", "different"),
            ("image_id", "sha256:different"),
            ("user", "0:0"),
        ):
            with self.subTest(manifest_key=key):
                changed_manifest = dict(manifest)
                changed_manifest[key] = value
                with self.assertRaises(ExecutionConflict):
                    self.backend._verify(changed_manifest)
        for key, value in (
            ("dev.agents.instance", "other-instance"),
            ("dev.agents.execution", "different"),
            ("dev.agents.run_id", "9"),
            ("dev.agents.generation", "9"),
            ("dev.agents.cwd_sha256", "wrong"),
            ("dev.agents.image_id", "sha256:different"),
            ("dev.agents.retention", "persistent"),
        ):
            with self.subTest(manifest_label=key):
                changed_manifest = json.loads(json.dumps(manifest))
                changed_manifest["labels"][key] = value
                with self.assertRaises(ExecutionConflict):
                    self.backend._verify(changed_manifest)

    def test_secret_exec_identity_must_match_database_run_and_generation(self) -> None:
        manifest, inspect = self._identity()
        path = self.backend._manifest_path("execution")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest))
        self.runtime.inspect_container.return_value = inspect
        self.assertEqual(self.backend.verified_container_name("execution", 1, 2), "agents-instance-r1-g2")
        for run_id, generation in ((9, 2), (1, 9)):
            with self.subTest(run_id=run_id, generation=generation), self.assertRaises(ExecutionConflict):
                self.backend.verified_container_name("execution", run_id, generation)

    def test_delete_rejects_missing_manifest_without_closing_herdr(self) -> None:
        with self.assertRaisesRegex(ExecutionConflict, "manifest.*absent"):
            self.backend.delete_run(RunHandle("execution", "workspace", "pane"))
        self.runtime.remove_container.assert_not_called()
        self.inner.delete_run.assert_not_called()

    def test_delete_does_not_close_herdr_when_verified_container_removal_fails(self) -> None:
        manifest, inspect = self._identity()
        path = self.backend._manifest_path("execution")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest))
        self.runtime.inspect_container.return_value = inspect
        self.runtime.remove_container.side_effect = RuntimeError("busy")
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.backend.delete_run(RunHandle("execution", "workspace", "pane"))
        self.inner.delete_run.assert_not_called()

    def test_delete_rejects_runtime_directory_outside_managed_root_without_side_effects(self) -> None:
        manifest, inspect = self._identity()
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("keep")
        manifest["runtime_dir"] = str(outside)
        mounts = cast(list[dict[str, object]], inspect["Mounts"])
        mounts[1]["Destination"] = str(outside)
        mounts[1]["Source"] = str(outside)
        path = self.backend._manifest_path("execution")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest))
        self.runtime.inspect_container.return_value = inspect
        with self.assertRaisesRegex(ExecutionConflict, "managed root"):
            self.backend.delete_run(RunHandle("execution", "workspace", "pane"))
        self.runtime.remove_container.assert_not_called()
        self.inner.delete_run.assert_not_called()
        self.assertEqual(marker.read_text(), "keep")
        self.assertTrue(path.is_file())

    def test_gc_removes_only_stale_owned_ephemeral_resources_and_trim_is_best_effort(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE terminal_runs("
            "id INTEGER,execution_name TEXT,state TEXT,agent_auth_id TEXT,generation INTEGER)"
        )
        connection.execute("CREATE TABLE launch_attempts(terminal_run_id INTEGER,state TEXT)")
        connection.execute("CREATE TABLE assignments(terminal_run_id INTEGER,execution_id INTEGER)")
        connection.execute("CREATE TABLE executions(id INTEGER,worktree_path TEXT,base_sha TEXT,state TEXT)")
        connection.execute("CREATE TABLE submissions(id INTEGER,execution_id INTEGER,commit_sha TEXT)")
        connection.execute("INSERT INTO terminal_runs VALUES(1,'active','live','active',1)")
        connection.execute("INSERT INTO terminal_runs VALUES(2,'uncertain','failed','uncertain-auth',1)")
        connection.execute("INSERT INTO launch_attempts VALUES(2,'uncertain')")
        runtime = MagicMock()
        runtime.resolve_image_id.side_effect = lambda image: image if image.startswith("sha256:") else "sha256:image"
        runtime.docker.side_effect = [
            "\n".join(
                (
                    json.dumps({"Names": "stale"}),
                    json.dumps({"Names": "stale-running"}),
                    json.dumps({"Names": "wrong-instance"}),
                    json.dumps({"Names": "active"}),
                    json.dumps({"Names": "compose-init"}),
                    json.dumps({"Names": "uncertain"}),
                )
            ),
            "ephemeral-volume\nactive-volume",
            json.dumps(
                [
                    {
                        "CreatedAt": "2000-01-01T00:00:00Z",
                        "Labels": {
                            "dev.agents.instance": "instance",
                            "dev.agents.execution": "old",
                            "dev.agents.retention": "ephemeral",
                        },
                    }
                ]
            ),
            "",
            json.dumps(
                [
                    {
                        "CreatedAt": "2000-01-01T00:00:00Z",
                        "Labels": {
                            "dev.agents.instance": "instance",
                            "dev.agents.execution": "active",
                            "dev.agents.retention": "ephemeral",
                        },
                    }
                ]
            ),
            "",
            "",
        ]
        stale = {
            "Id": "sha256:stale",
            "Config": {
                "Labels": {
                    "dev.agents.instance": "instance",
                    "dev.agents.execution": "old",
                    "dev.agents.retention": "ephemeral",
                }
            },
            "State": {"Running": False, "FinishedAt": "2000-01-01T00:00:00Z"},
        }
        stale_running = {
            "Id": "sha256:stale-running",
            "Created": "2000-01-01T00:00:00Z",
            "Config": {
                "Labels": {
                    "dev.agents.instance": "instance",
                    "dev.agents.execution": "old-running",
                    "dev.agents.retention": "ephemeral",
                }
            },
            "State": {"Running": True, "FinishedAt": ""},
        }
        wrong_instance = {
            "Created": "2000-01-01T00:00:00Z",
            "Config": {
                "Labels": {
                    "dev.agents.instance": "other",
                    "dev.agents.execution": "old-other",
                    "dev.agents.retention": "ephemeral",
                }
            },
            "State": {"Running": True, "FinishedAt": ""},
        }
        active = {
            "Config": {
                "Labels": {
                    "dev.agents.instance": "instance",
                    "dev.agents.execution": "active",
                    "dev.agents.retention": "ephemeral",
                }
            },
            "State": {"Running": False, "FinishedAt": "2000-01-01T00:00:00Z"},
        }
        uncertain = {
            "Config": {
                "Labels": {
                    "dev.agents.instance": "instance",
                    "dev.agents.execution": "uncertain",
                    "dev.agents.retention": "ephemeral",
                }
            },
            "State": {"Running": False, "FinishedAt": "2000-01-01T00:00:00Z"},
        }
        compose = {
            "Config": {
                "Labels": {
                    "dev.agents.instance": "instance",
                    "dev.agents.topology": "topology",
                    "dev.agents.retention": "ephemeral",
                }
            },
            "State": {"Running": False, "FinishedAt": "2000-01-01T00:00:00Z"},
        }
        runtime.inspect_container.side_effect = [stale, stale_running, wrong_instance, active, compose, uncertain]
        runtime.trim.side_effect = ContainerRuntimeError("trim unavailable")
        with patch("agents.container_runtime._instance_id", return_value="instance"):
            collector = ContainerGarbageCollector(self.config, connection, runtime)
        result = collector.collect()
        runtime.remove_container.assert_has_calls(
            [call("stale", "sha256:stale"), call("stale-running", "sha256:stale-running")]
        )
        self.assertEqual(runtime.remove_container.call_count, 2)
        self.assertNotIn(call("compose-init", ANY), runtime.remove_container.call_args_list)
        self.assertNotIn(call("wrong-instance", ANY), runtime.remove_container.call_args_list)
        self.assertNotIn(call("uncertain"), runtime.remove_container.call_args_list)
        self.assertEqual(result["volumes"], ["ephemeral-volume"])
        runtime.docker.assert_any_call(
            "volume",
            "ls",
            "--filter",
            "label=dev.agents.instance=instance",
            "--filter",
            "label=dev.agents.retention=ephemeral",
            "--format",
            "{{.Name}}",
        )
        runtime.docker.assert_any_call("volume", "rm", "ephemeral-volume")
        self.assertNotIn(call("volume", "rm", "active-volume"), runtime.docker.call_args_list)
        self.assertIn("trim unavailable", result["trim_error"])
        connection.close()

    def test_runner_passes_only_secret_names_to_docker(self) -> None:
        cwd = self.root / "clone"
        runtime_dir = self.root / "runtime"
        cwd.mkdir()
        runtime_dir.mkdir()
        runtime = MagicMock()
        runtime.docker_environment.return_value = {"DOCKER_HOST": "unix:///socket"}
        environment = {
            "AGENTS_CONTAINER_NAME": "agents-instance-r1-g2",
            "AGENTS_CONTAINER_IMAGE_ID": "sha256:image",
            "AGENTS_CONTAINER_CWD": str(cwd),
            "AGENTS_CONTAINER_RUNTIME": str(runtime_dir),
            "AGENTS_CONTAINER_USER": "501:20",
            "AGENTS_CONTAINER_CPUS": "2",
            "AGENTS_CONTAINER_MEMORY_MB": "4096",
            "AGENTS_CONTAINER_PIDS": "512",
            "AGENTS_CONTAINER_NETWORK": "agents-runs",
            "OPENCODE_AUTH_JSON": '{"token":"super-secret"}',
            "AGENTS_API_TOKEN": "run-token",
        }
        metadata = {
            "labels": {"dev.agents.instance": "instance"},
            "env_names": ["OPENCODE_AUTH_JSON", "AGENTS_API_TOKEN"],
        }
        completed = SimpleNamespace(returncode=0)
        with (
            patch.dict("os.environ", environment, clear=True),
            patch("agents.container_runner._metadata", return_value=(runtime, metadata)),
            patch("agents.container_runner.subprocess.run", return_value=completed) as launched,
        ):
            self.assertEqual(run_container(("opencode", "--model", "test")), 0)
        arguments = launched.call_args.args[0]
        process_environment = launched.call_args.kwargs["env"]
        self.assertNotIn("super-secret", repr(arguments))
        self.assertNotIn("super-secret", repr(process_environment))
        self.assertIn(("--env", "AGENTS_API_TOKEN"), tuple(zip(arguments, arguments[1:], strict=False)))
        self.assertEqual(process_environment["AGENTS_API_TOKEN"], "run-token")
        self.assertIn("--read-only", arguments)
        self.assertIn("--cap-drop=ALL", arguments)
        self.assertEqual(
            (runtime_dir / "home/.local/share/opencode/auth.json").read_text(),
            '{"token":"super-secret"}',
        )


if __name__ == "__main__":
    unittest.main()
