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

    def test_loads_structured_model_choices(self) -> None:
        configured = CONFIG.replace(
            "api_port=9889",
            """api_port=9889
models=[
  {id='openai/gpt-5', reasoning_effort='high'},
  {id='anthropic/claude-sonnet-4-6'},
]""",
        ).replace("provider='mock'", "provider='opencode'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            choices = load(path, {}).cao.models
            self.assertEqual(
                [(choice.id, choice.reasoning_effort) for choice in choices],
                [("openai/gpt-5", "high"), ("anthropic/claude-sonnet-4-6", "")],
            )

    def test_environment_model_and_reasoning_override_toml_choices(self) -> None:
        configured = CONFIG.replace(
            "api_port=9889",
            "api_port=9889\nmodels=[{id='openai/gpt-5', reasoning_effort='low'}]",
        ).replace("provider='mock'", "provider='opencode'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            choices = load(
                path,
                {
                    "AGENTS_MODEL": "openai/gpt-5-mini",
                    "AGENTS_REASONING_EFFORT": "medium",
                },
            ).cao.models
            self.assertEqual(
                [(choice.id, choice.reasoning_effort) for choice in choices], [("openai/gpt-5-mini", "medium")]
            )

    def test_empty_environment_model_does_not_override_toml(self) -> None:
        configured = CONFIG.replace("api_port=9889", "api_port=9889\nmodel='mock/model'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            self.assertEqual(load(path, {"AGENTS_MODEL": ""}).cao.models[0].id, "mock/model")

    def test_rejects_ambiguous_or_invalid_model_choices(self) -> None:
        invalid_sections = (
            "model='openai/gpt-5'\nmodels=[{id='openai/gpt-5'}]",
            "models=[]",
            "models=[{id='openai/gpt-5'},{id='openai/gpt-5'}]",
            "models=[{id='bad model'}]",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            for section in invalid_sections:
                with self.subTest(section=section), self.assertRaises(ConfigError):
                    path.write_text(CONFIG.replace("api_port=9889", f"api_port=9889\n{section}"))
                    load(path, {})

    def test_rejects_reasoning_without_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(
                CONFIG.replace("api_port=9889", "api_port=9889\nmodel='mock/model'\nreasoning_effort='high'")
            )
            with self.assertRaisesRegex(ConfigError, "only by the opencode provider"):
                load(path, {})

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
