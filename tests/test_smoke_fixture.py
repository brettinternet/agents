from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.config import load
from tests.smoke_e2e import FIXTURE_BIN, ROOT, _config_file, _isolated_environment


class SmokeFixtureTests(unittest.TestCase):
    def test_mock_provider_keeps_cao_markers_without_credentials(self) -> None:
        provider = FIXTURE_BIN / "mock_cli"
        result = subprocess.run(
            [str(provider), "--delay-ms", "0"],
            input="wake\n",
            text=True,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", "")},
            timeout=5,
            check=True,
        )
        self.assertIn("mock_cli 2.4.1", result.stdout)
        self.assertIn("❯", result.stdout)
        self.assertIn("> MOCK:", result.stdout)

    def test_smoke_config_is_mock_and_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.toml"
            _config_file(ROOT / "agents.toml", path, 29889, 29890)
            config = load(path, env={"AGENTS_PROVIDER": "mock"})
            self.assertEqual(config.cao.provider_id, "mock_cli")
            self.assertEqual(config.project.verify, (("git", "status", "--porcelain"),))
            self.assertEqual(config.cao.api_port, 29889)
            self.assertEqual(config.web.port, 29890)

    def test_isolated_environment_discards_inherited_agent_overrides(self) -> None:
        names = (
            "AGENTS_CONFIG",
            "AGENTS_PROVIDER",
            "AGENTS_MODEL",
            "AGENTS_REASONING_EFFORT",
            "AGENTS_CAO_PORT",
            "AGENTS_WEB_PORT",
            "AGENTS_WEB_TOKEN",
            "AGENTS_AGENT_TOKEN",
            "AGENTS_API_URL",
        )
        overrides = {
            "AGENTS_CONFIG": "/tmp/inherited-agents.toml",
            "AGENTS_PROVIDER": "claude",
            "AGENTS_MODEL": "inherited-model",
            "AGENTS_REASONING_EFFORT": "inherited-reasoning",
            "AGENTS_CAO_PORT": "1",
            "AGENTS_WEB_PORT": "2",
            "AGENTS_WEB_TOKEN": "inherited-web-token",
            "AGENTS_AGENT_TOKEN": "inherited-agent-token",
            "AGENTS_API_URL": "https://inherited.example.invalid",
        }
        original = os.environ.copy()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agents.toml"
            home = root / "home"
            xdg = root / "xdg"
            with patch.dict(os.environ, overrides, clear=False):
                inherited = os.environ.copy()
                with _isolated_environment(config_path, home, xdg):
                    self.assertEqual(os.environ["AGENTS_CONFIG"], str(config_path))
                    self.assertEqual(os.environ["AGENTS_PROVIDER"], "mock")
                    for name in names[2:]:
                        self.assertNotIn(name, os.environ)
                self.assertEqual(os.environ, inherited)
        self.assertEqual(os.environ, original)


if __name__ == "__main__":
    unittest.main()
