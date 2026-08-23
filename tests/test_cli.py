from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.cli import _missing_herdr_methods, main, preflight
from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.git_worktree import GitError
from agents.service import ServiceError


def _config(root: Path, provider: str = "opencode", provider_id: str = "opencode_cli") -> AgentsConfig:
    return AgentsConfig(
        source=root / "agents.toml",
        root=root,
        project=ProjectConfig("test", root, "main", (("task", "check"),)),
        runtime=RuntimeConfig(5, 1800, 12, 4, 3, 86400),
        execution=ExecutionConfig("herdr", "0.8.2", "agents-test", provider, provider_id, (ModelChoice(""),)),
        web=WebConfig("127.0.0.1", 9890),
        actors=(),
    )


class CliTests(unittest.TestCase):
    def test_generated_schema_accepts_required_herdr_methods(self) -> None:
        document = {
            "schemas": {
                "request": {
                    "oneOf": [
                        {"properties": {"method": {"const": name, "type": "string"}}}
                        for name in (
                            "ping",
                            "session.snapshot",
                            "workspace.create",
                            "workspace.close",
                            "agent.start",
                            "pane.get",
                            "pane.read",
                            "agent.prompt",
                            "pane.send_input",
                            "events.subscribe",
                        )
                    ]
                }
            }
        }
        self.assertEqual(_missing_herdr_methods(document), [])

    def test_schema_reports_missing_method(self) -> None:
        self.assertIn("workspace.close", _missing_herdr_methods({"methods": [{"name": "ping"}]}))

    def test_preflight_reports_exact_provider_and_git_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.cli.shutil.which", return_value=None),
                patch("agents.cli.herdr_executable", side_effect=RuntimeError("missing")),
                patch("agents.cli.git", side_effect=GitError("unset")),
            ):
                errors = preflight(config)
        self.assertIn(
            "configured provider executable `opencode` is missing; operator action: install OpenCode and ensure `opencode` is on PATH",
            errors,
        )
        self.assertIn("Herdr executable is missing", errors[1])
        name_command = shlex.join(("git", "-C", str(config.project.path), "config", "user.name", "Your Name"))
        email_command = shlex.join(("git", "-C", str(config.project.path), "config", "user.email", "you@example.com"))
        self.assertIn(
            f"Git commit identity `user.name` is missing; operator action: run `{name_command}`",
            errors,
        )
        self.assertIn(
            f"Git commit identity `user.email` is missing; operator action: run `{email_command}`",
            errors,
        )

    def test_init_prepares_installs_integration_reuses_services_and_runs_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.cli._config", return_value=config),
                patch("agents.cli._prepare") as prepare,
                patch("agents.cli.preflight", return_value=[]) as check_preflight,
                patch("agents.cli.install_integration") as install,
                patch("agents.cli.service.start") as start_services,
                patch("agents.cli.doctor", return_value=[]) as run_doctor,
            ):
                main(["init"])
                main(["init"])

        for operation in (prepare, check_preflight, install, start_services, run_doctor):
            self.assertEqual(operation.call_count, 2)
            operation.assert_called_with(config)

    def test_shutdown_renders_service_error_as_clean_exit_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.cli._config", return_value=config),
                patch("agents.cli.service.shutdown", side_effect=ServiceError("agentsd.lock is already locked")),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["shutdown"])
            self.assertEqual(str(raised.exception), "agentsd.lock is already locked")

    def test_shutdown_success_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.cli._config", return_value=config),
                patch("agents.cli.service.shutdown") as shutdown,
            ):
                main(["shutdown"])
            shutdown.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
