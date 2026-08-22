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
  {id='openai/gpt-5', effort='high'},
  {id='anthropic/claude-sonnet-4-6'},
]""",
        ).replace("provider='mock'", "provider='opencode'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            choices = load(path, {}).cao.models
            self.assertEqual(
                [(choice.id, choice.effort) for choice in choices],
                [("openai/gpt-5", "high"), ("anthropic/claude-sonnet-4-6", "")],
            )

    def test_actor_model_choices_override_global_choices(self) -> None:
        configured = (
            CONFIG.replace(
                "api_port=9889",
                "api_port=9889\nmodel='openai/gpt-5-mini'",
            )
            .replace(
                "slug='human'",
                """slug='elder'
kind='agent'
models=[
  {id='openai/gpt-5', effort='high'},
  {id='anthropic/claude-sonnet-4-6'},
]""",
            )
            .replace("provider='mock'", "provider='opencode'")
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            config = load(path, {})
            self.assertEqual(
                [(choice.id, choice.effort) for choice in config.models_for("elder")],
                [("openai/gpt-5", "high"), ("anthropic/claude-sonnet-4-6", "")],
            )
            self.assertEqual(config.models_for("unknown")[0].id, "openai/gpt-5-mini")

    def test_environment_model_overrides_actor_choices(self) -> None:
        configured = CONFIG.replace(
            "slug='human'",
            "slug='elder'\nkind='agent'\nmodels=[{id='actor/model'}]",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            config = load(path, {"AGENTS_MODEL": "override/model"})
            self.assertEqual(config.models_for("elder"), config.cao.models)
            self.assertEqual(config.models_for("elder")[0].id, "override/model")

    def test_rejects_actor_choices_for_non_agents(self) -> None:
        configured = CONFIG.replace("slug='human'", "slug='human'\nmodels=[{id='mock/model'}]")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            for env in ({}, {"AGENTS_MODEL": "override/model"}):
                with self.subTest(env=env), self.assertRaisesRegex(ConfigError, "require kind='agent'"):
                    load(path, env)

    def test_rejects_invalid_actor_model_pool(self) -> None:
        configured = CONFIG.replace(
            "slug='human'",
            "slug='elder'\nkind='agent'\nmodels=[]",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            for env in ({}, {"AGENTS_MODEL": "override/model"}):
                with (
                    self.subTest(env=env),
                    self.assertRaisesRegex(ConfigError, "actor elder.models must be a nonempty"),
                ):
                    load(path, env)

    def test_environment_model_and_effort_override_toml_choices(self) -> None:
        configured = CONFIG.replace(
            "api_port=9889",
            "api_port=9889\nmodels=[{id='openai/gpt-5', effort='low'}]",
        ).replace("provider='mock'", "provider='opencode'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            choices = load(
                path,
                {
                    "AGENTS_MODEL": "openai/gpt-5-mini",
                    "AGENTS_EFFORT": "medium",
                },
            ).cao.models
            self.assertEqual([(choice.id, choice.effort) for choice in choices], [("openai/gpt-5-mini", "medium")])

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

    def test_rejects_effort_without_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG.replace("api_port=9889", "api_port=9889\nmodel='mock/model'\neffort='high'"))
            with self.assertRaisesRegex(ConfigError, "only by the opencode provider"):
                load(path, {})

    def test_rejects_renamed_reasoning_effort_keys(self) -> None:
        configured = CONFIG.replace("provider='mock'", "provider='opencode'").replace(
            "api_port=9889", "api_port=9889\nmodel='openai/gpt-5'\nreasoning_effort='high'"
        )
        nested_configured = CONFIG.replace(
            "api_port=9889",
            "api_port=9889\nmodels=[{id='openai/gpt-5', reasoning_effort='high'}]",
        ).replace("provider='mock'", "provider='opencode'")
        actor_configured = CONFIG.replace(
            "slug='human'",
            "slug='elder'\nkind='agent'\nmodel='openai/gpt-5'\nreasoning_effort='high'",
        ).replace("provider='mock'", "provider='opencode'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            for content, env in (
                (configured, {}),
                (nested_configured, {}),
                (nested_configured, {"AGENTS_MODEL": "openai/gpt-5-mini"}),
                (actor_configured, {}),
                (CONFIG, {"AGENTS_MODEL": "openai/gpt-5", "AGENTS_REASONING_EFFORT": "high"}),
            ):
                with self.subTest(content=content, env=env), self.assertRaisesRegex(ConfigError, "renamed"):
                    path.write_text(content)
                    load(path, env)

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

    def test_loads_cron_and_interval_schedules(self) -> None:
        configured = CONFIG.replace(
            "slug='human'",
            """slug='explorer'
kind='agent'
persistent=true
[[schedules]]
slug='daily-scout'
cron='0 9 * * *'
timezone='America/Los_Angeles'
to='@explorer'
message='Explore and commit a public-safe memory.'
[[schedules]]
slug='hourly-scout'
every='1h'
to='#findings'
message='Report meaningful changes only.'
[[schedules]]
slug='weekly-memory'
cron='0 9 * * 1'
[schedules.work]
kind='spike'
title='Weekly exploration'
problem='Find useful public developments.'
outcome='Commit a dated public-safe memory with sources and recommendations.'""",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            schedules = load(path, {}).schedules
            self.assertEqual((schedules[0].cron, schedules[0].timezone), ("0 9 * * *", "America/Los_Angeles"))
            self.assertEqual(schedules[1].every_seconds, 3600)
            work = schedules[2].work
            self.assertIsNotNone(work)
            assert work is not None
            self.assertEqual(work.kind, "spike")
            self.assertIn("public-safe memory", work.outcome)

    def test_rejects_invalid_schedule_configuration(self) -> None:
        base = CONFIG.replace("slug='human'", "slug='explorer'\nkind='agent'\npersistent=true")
        invalid = (
            "slug='bad'\nto='@explorer'\nmessage='x'",
            "slug='bad'\ncron='bad'\nto='@explorer'\nmessage='x'",
            "slug='bad'\nevery='1h'\nto='@missing'\nmessage='x'",
            "slug='bad'\nevery='1h'\ntimezone='Mars/Olympus'\nto='@explorer'\nmessage='x'",
            "slug='bad'\nevery='1h'\nto='@explorer'\nmessage='x'\noverlap='queue'",
            "slug='bad'\nevery='1h'\nto='@explorer'\nmessage='x'\nwork={kind='spike',title='x',problem='x',outcome='x'}",
            "slug='bad'\nevery='1h'\nwork={kind='invalid',title='x',problem='x',outcome='x'}",
            "slug='bad'\nevery='366d'\nto='@explorer'\nmessage='x'",
            "slug='bad'\ncron=0\nevery='1h'\nto='@explorer'\nmessage='x'",
            "slug='bad'\ncron=''\nto='@explorer'\nmessage='x'",
            "slug='bad'\nevery='1h'\nwork={kind=[],title='x',problem='x',outcome='x'}",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            for schedule in invalid:
                with self.subTest(schedule=schedule), self.assertRaises(ConfigError):
                    path.write_text(base + "\n[[schedules]]\n" + schedule + "\n")
                    load(path, {})
