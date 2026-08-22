from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents.config import ConfigError, load

CONFIG = """[project]
name='x'
path='.'
default_branch='main'
verify=[['task','check']]
[runtime]
poll_seconds=5
stall_seconds=1800
launch_budget_per_hour=12
max_agents=4
max_consultations=3
worker_grace_seconds=86400
[cao]
version='2.4.1'
provider='mock'
api_port=9889
[web]
host='127.0.0.1'
port=9890
[[actors]]
slug='human'
"""


class ConfigTests(unittest.TestCase):
    def test_loads_structured_verification_and_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG)
            config = load(path, {})
            self.assertEqual(config.project.verify, (("task", "check"),))
            self.assertEqual(config.cao.provider_id, "mock_cli")

    def test_rejects_codex_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG)
            with self.assertRaisesRegex(ConfigError, "unsupported provider: codex"):
                load(path, {"AGENTS_PROVIDER": "codex"})

    def test_rejects_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG.replace("['task','check']", "['task','check;bad']"))
            with self.assertRaises(ConfigError):
                load(path, {})

    def test_refuses_non_loopback_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG.replace("host='127.0.0.1'", "host='0.0.0.0'"))
            with self.assertRaises(ConfigError):
                load(path, {})
