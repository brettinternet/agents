from __future__ import annotations

import sqlite3
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
[execution]
backend='herdr'
version='0.8.2'
provider='mock'
[web]
host='127.0.0.1'
port=9890
[[actors]]
slug='human'
"""


class ConfigTests(unittest.TestCase):
    def test_loads_execution_provider_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG)
            config = load(path, {})
            self.assertEqual(config.project.verify, (("task", "check"),))
            self.assertEqual(config.execution.backend, "herdr")
            self.assertEqual(config.execution.version, "0.8.2")
            self.assertIsNone(config.execution.session)
            self.assertEqual(config.execution.provider_id, "mock_cli")

    def test_omitted_session_derives_from_persisted_project_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "agents.toml"
            path.write_text(CONFIG)
            state = root / ".agents"
            state.mkdir(mode=0o700)
            database = state / "agents.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE project(id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL)")
                connection.execute("INSERT INTO project(id,instance_id) VALUES(1,'1234abcd')")
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(load(path, {}).execution_session, "agents-1234abcd")

    def test_explicit_session_wins_over_project_identity(self) -> None:
        configured = CONFIG.replace("version='0.8.2'", "version='0.8.2'\nsession='agents-fixed'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            self.assertEqual(load(path, {}).execution_session, "agents-fixed")

    def test_loads_structured_model_choices(self) -> None:
        configured = CONFIG.replace(
            "provider='mock'",
            """provider='opencode'
models=[
  {id='openai/gpt-5', effort='high'},
  {id='anthropic/claude-sonnet-4-6'},
]""",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            choices = load(path, {}).execution.models
            self.assertEqual(
                [(choice.id, choice.effort) for choice in choices],
                [("openai/gpt-5", "high"), ("anthropic/claude-sonnet-4-6", "")],
            )

    def test_actor_model_choices_override_global_choices(self) -> None:
        configured = CONFIG.replace("provider='mock'", "provider='opencode'\nmodel='openai/gpt-5-mini'").replace(
            "slug='human'",
            """slug='manager'
kind='agent'
models=[
  {id='openai/gpt-5', effort='high'},
  {id='anthropic/claude-sonnet-4-6'},
]""",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            config = load(path, {})
            self.assertEqual(
                [(choice.id, choice.effort) for choice in config.models_for("manager")],
                [("openai/gpt-5", "high"), ("anthropic/claude-sonnet-4-6", "")],
            )
            self.assertEqual(config.models_for("unknown")[0].id, "openai/gpt-5-mini")

    def test_environment_model_overrides_actor_choices(self) -> None:
        configured = CONFIG.replace("slug='human'", "slug='manager'\nkind='agent'\nmodels=[{id='actor/model'}]")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            config = load(path, {"AGENTS_MODEL": "override/model"})
            self.assertEqual(config.models_for("manager"), config.execution.models)
            self.assertEqual(config.models_for("manager")[0].id, "override/model")

    def test_rejects_actor_choices_for_non_agents(self) -> None:
        configured = CONFIG.replace("slug='human'", "slug='human'\nmodels=[{id='mock/model'}]")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            with self.assertRaisesRegex(ConfigError, "require kind='agent'"):
                load(path, {})

    def test_rejects_invalid_actor_model_pool(self) -> None:
        configured = CONFIG.replace("slug='human'", "slug='manager'\nkind='agent'\nmodels=[]")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            with self.assertRaisesRegex(ConfigError, "actor manager.models must be a nonempty"):
                load(path, {})

    def test_environment_model_and_effort_override_toml_choices(self) -> None:
        configured = CONFIG.replace(
            "provider='mock'",
            "provider='opencode'\nmodels=[{id='openai/gpt-5', effort='low'}]",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            choices = load(path, {"AGENTS_MODEL": "openai/gpt-5-mini", "AGENTS_EFFORT": "medium"}).execution.models
            self.assertEqual([(choice.id, choice.effort) for choice in choices], [("openai/gpt-5-mini", "medium")])

    def test_empty_environment_model_does_not_override_toml(self) -> None:
        configured = CONFIG.replace("provider='mock'", "provider='mock'\nmodel='mock/model'")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            self.assertEqual(load(path, {"AGENTS_MODEL": ""}).execution.models[0].id, "mock/model")

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
                    path.write_text(CONFIG.replace("provider='mock'", f"provider='mock'\n{section}"))
                    load(path, {})

    def test_rejects_effort_without_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG.replace("provider='mock'", "provider='mock'\nmodel='mock/model'\neffort='high'"))
            with self.assertRaisesRegex(ConfigError, "only by the opencode provider"):
                load(path, {})

    def test_rejects_invalid_backend_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG.replace("backend='herdr'", "backend='remote'"))
            with self.assertRaisesRegex(ConfigError, "unsupported execution backend"):
                load(path, {})
            path.write_text(CONFIG.replace("version='0.8.2'", "version='0.8.2'\nsession='Bad Session'"))
            with self.assertRaisesRegex(ConfigError, "execution.session"):
                load(path, {})

    def test_rejects_renamed_reasoning_effort_keys(self) -> None:
        configured = CONFIG.replace(
            "provider='mock'", "provider='opencode'\nmodel='openai/gpt-5'\nreasoning_effort='high'"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(configured)
            with self.assertRaisesRegex(ConfigError, "renamed"):
                load(path, {})

    def test_rejects_codex_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG)
            with self.assertRaisesRegex(ConfigError, "unsupported provider: codex"):
                load(path, {"AGENTS_PROVIDER": "codex"})

    def test_rejects_shell_syntax_and_non_loopback_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            path.write_text(CONFIG.replace("['task','check']", "['task','check;bad']"))
            with self.assertRaises(ConfigError):
                load(path, {})
            path.write_text(CONFIG.replace("host='127.0.0.1'", "host='0.0.0.0'"))
            with self.assertRaises(ConfigError):
                load(path, {})

    def test_loads_cron_and_interval_schedules(self) -> None:
        configured = CONFIG.replace(
            "slug='human'",
            """slug='researcher'
kind='agent'
persistent=true
[[schedules]]
slug='daily-scout'
cron='0 9 * * *'
timezone='America/Los_Angeles'
to='@researcher'
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
            self.assertEqual(schedules[2].work.kind if schedules[2].work else None, "spike")

    def test_rejects_invalid_schedule_configuration(self) -> None:
        base = CONFIG.replace("slug='human'", "slug='researcher'\nkind='agent'\npersistent=true")
        invalid = (
            "slug='bad'\nto='@researcher'\nmessage='x'",
            "slug='bad'\ncron='bad'\nto='@researcher'\nmessage='x'",
            "slug='bad'\nevery='1h'\nto='@missing'\nmessage='x'",
            "slug='bad'\nevery='1h'\ntimezone='Mars/Olympus'\nto='@researcher'\nmessage='x'",
            "slug='bad'\nevery='1h'\nto='@researcher'\nmessage='x'\noverlap='queue'",
            "slug='bad'\nevery='1h'\nto='@researcher'\nmessage='x'\nwork={kind='spike',title='x',problem='x',outcome='x'}",
            "slug='bad'\nevery='1h'\nwork={kind='invalid',title='x',problem='x',outcome='x'}",
            "slug='bad'\nevery='366d'\nto='@researcher'\nmessage='x'",
            "slug='bad'\ncron=0\nevery='1h'\nto='@researcher'\nmessage='x'",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agents.toml"
            for schedule in invalid:
                with self.subTest(schedule=schedule), self.assertRaises(ConfigError):
                    path.write_text(base + "\n[[schedules]]\n" + schedule + "\n")
                    load(path, {})


if __name__ == "__main__":
    unittest.main()
