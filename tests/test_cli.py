from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.cli import _missing_openapi_methods, main, preflight
from agents.config import AgentsConfig, CaoConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.git_worktree import GitError


def _config(root: Path, provider: str = "opencode", provider_id: str = "opencode_cli") -> AgentsConfig:
    return AgentsConfig(
        source=root / "agents.toml",
        root=root,
        project=ProjectConfig("test", root, "main", (("task", "check"),)),
        runtime=RuntimeConfig(5, 1800, 12, 4, 3, 86400),
        cao=CaoConfig("2.4.1", provider, provider_id, 9889, (ModelChoice(""),)),
        web=WebConfig("127.0.0.1", 9890),
        actors=(),
    )


class CliTests(unittest.TestCase):
    def test_openapi_accepts_equivalent_template_parameter_names(self) -> None:
        document = {
            "paths": {
                "/sessions": {"post": {}},
                "/sessions/{session_name}": {"get": {}, "delete": {}},
                "/sessions/{session_name}/terminals": {"get": {}},
                "/terminals/{terminal_id}": {"get": {}},
                "/terminals/{terminal_id}/working-directory": {"get": {}},
                "/terminals/{terminal_id}/input": {"post": {}},
                "/terminals/{terminal_id}/output": {"get": {}},
                "/terminals/{receiver_id}/inbox/messages": {"post": {}},
            }
        }

        self.assertEqual(_missing_openapi_methods(document), [])

    def test_openapi_still_rejects_wrong_method_and_path_shape(self) -> None:
        document = {
            "paths": {
                "/terminals/{receiver_id}/inbox/messages": {"get": {}},
                "/widgets/{receiver_id}/inbox/messages": {"post": {}},
            }
        }

        missing = _missing_openapi_methods(document)
        self.assertIn("/terminals/{terminal_id}/inbox/messages post", missing)

    def test_preflight_reports_exact_provider_and_git_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.cli.shutil.which", return_value=None),
                patch("agents.cli.git", side_effect=GitError("unset")),
            ):
                errors = preflight(config)
        self.assertIn(
            "CAO provider executable `opencode` is missing; operator action: run `brew install anomalyco/tap/opencode`",
            errors,
        )
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

    def test_init_rerun_prepares_reuses_services_and_runs_doctor_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            with (
                patch("agents.cli._config", return_value=config),
                patch("agents.cli._prepare") as prepare,
                patch("agents.cli.preflight", return_value=[]) as check_preflight,
                patch("agents.cli.service.start") as start_services,
                patch("agents.cli.doctor", return_value=[]) as run_doctor,
            ):
                main(["init"])
                main(["init"])

        for operation in (prepare, check_preflight, start_services, run_doctor):
            self.assertEqual(operation.call_count, 2)
            operation.assert_called_with(config)


if __name__ == "__main__":
    unittest.main()
