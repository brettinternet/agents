from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, call, patch

from agents.config import AgentsConfig, ContainerConfig, IsolationMode
from agents.container_runner import run as run_container
from agents.container_runtime import (
    ContainerGarbageCollector,
    ContainerizedHerdrBackend,
    ContainerRuntimeError,
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
            "dev.agents.cwd_sha256": "digest",
            "dev.agents.image_id": "sha256:image",
            "dev.agents.retention": "ephemeral",
        }
        manifest = {
            "container_name": "agents-instance-r1-g2",
            "image_id": "sha256:image",
            "cwd": cwd,
            "runtime_dir": runtime_dir,
            "user": "501:20",
            "labels": labels,
        }
        inspect = {
            "Image": "sha256:image",
            "Config": {"User": "501:20", "Labels": labels},
            "HostConfig": {
                "ReadonlyRootfs": True,
                "PidsLimit": 512,
                "NanoCpus": 2_000_000_000,
                "Memory": 4096 * 1024 * 1024,
                "NetworkMode": "agents-runs",
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
            },
            "Mounts": [
                {"Destination": cwd, "Source": cwd, "RW": True},
                {"Destination": runtime_dir, "Source": runtime_dir, "RW": True},
            ],
        }
        return manifest, inspect

    def test_identity_verification_accepts_only_exact_hardened_container(self) -> None:
        manifest, inspect = self._identity()
        self.runtime.inspect_container.return_value = inspect
        self.assertIs(self.backend._verify(manifest), inspect)

        mutations = (
            ("Image", "sha256:wrong"),
            ("Config.User", "0:0"),
            ("HostConfig.ReadonlyRootfs", False),
            ("HostConfig.PidsLimit", 1),
            ("HostConfig.NanoCpus", 1),
            ("HostConfig.Memory", 1),
            ("HostConfig.NetworkMode", "bridge"),
            ("HostConfig.CapDrop", []),
            ("HostConfig.SecurityOpt", []),
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

    def test_gc_removes_only_stale_owned_ephemeral_resources_and_trim_is_best_effort(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE terminal_runs(execution_name TEXT,state TEXT)")
        connection.execute("INSERT INTO terminal_runs VALUES('active','live')")
        runtime = MagicMock()
        runtime.docker.side_effect = [
            "\n".join((json.dumps({"Names": "stale"}), json.dumps({"Names": "active"}))),
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
            "Config": {"Labels": {"dev.agents.execution": "old", "dev.agents.retention": "ephemeral"}},
            "State": {"Running": False, "FinishedAt": "2000-01-01T00:00:00Z"},
        }
        active = {
            "Config": {"Labels": {"dev.agents.execution": "active", "dev.agents.retention": "ephemeral"}},
            "State": {"Running": False, "FinishedAt": "2000-01-01T00:00:00Z"},
        }
        runtime.inspect_container.side_effect = [stale, active]
        runtime.trim.side_effect = ContainerRuntimeError("trim unavailable")
        with patch("agents.container_runtime._instance_id", return_value="instance"):
            collector = ContainerGarbageCollector(self.config, connection, runtime)
        result = collector.collect()
        runtime.remove_container.assert_called_once_with("stale")
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
