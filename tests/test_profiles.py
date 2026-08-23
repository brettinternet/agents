from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents.profiles as profiles_module
from agents.profiles import (
    ProfileError,
    ensure_secret,
    execution_name,
    install_profile,
    materialize_profile,
    mcp_name,
    merge_owned_json,
    profile_name,
    provider_lock_path,
    purpose_tools,
    remove_profile,
    validate_templates,
)


class ProfileTests(unittest.TestCase):
    def _materialize(
        self,
        root: Path,
        state: Path,
        *,
        provider: str = "opencode_cli",
        purpose_kind: str = "persistent",
        run_id: int = 1,
        reasoning_effort: str = "",
    ):
        return materialize_profile(
            root,
            state,
            template="manager",
            instance="deadbeef",
            run_id=run_id,
            generation=1,
            provider=provider,
            purpose_kind=purpose_kind,
            specialty="research" if purpose_kind == "work" else "",
            token="secret-token",
            api_url="http://127.0.0.1:9890",
            reasoning_effort=reasoning_effort,
        )

    def test_repository_templates_validate(self):
        validate_templates(Path(__file__).resolve().parents[1])

    def test_persistent_profiles_have_no_generic_filesystem_or_command_tools(self):
        self.assertEqual(purpose_tools("persistent"), ())

    def test_materialized_profiles_explain_repository_interaction_methods(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            profile = self._materialize(root, Path(d))
            text = profile.path.read_text()
        self.assertEqual(profile.allowed_tools, ())
        self.assertIn("`repository_list` and `repository_read`", text)
        self.assertIn("not native filesystem tools or browser `file://` URLs", text)
        self.assertIn("Use Agent MCP backlog tools", text)
        self.assertIn("durable memory changes", text)
        self.assertIn("request an execute-capable work item for any secret operation", text)

    def test_packaged_templates_fallback_when_editable_templates_are_absent(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "editable"
            root.mkdir()
            package = Path(d) / "installed_package"
            package.mkdir()
            for name in ("manager", "researcher", "executor", "writer"):
                (package / f"{name}.md").write_bytes((repository / "agents" / f"{name}.md").read_bytes())
            with patch.object(profiles_module, "__file__", str(package / "profiles.py")):
                validate_templates(root)
                materialized = materialize_profile(
                    root,
                    Path(d) / "state",
                    template="manager",
                    instance="deadbeef",
                    run_id=1,
                    generation=1,
                    provider="opencode_cli",
                    purpose_kind="persistent",
                    specialty="",
                    token="secret-token",
                    api_url="http://127.0.0.1:9890",
                )
            self.assertIn("You are the manager", materialized.path.read_text())

    def test_unsupported_provider_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as d,
            self.assertRaisesRegex(ProfileError, "unsupported provider capability"),
        ):
            self._materialize(Path(__file__).parents[1], Path(d), provider="codex")

    def test_secret_creation_is_private_and_not_rotated(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "key"
            ensure_secret(path, existing_state=False)
            first = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            ensure_secret(path, existing_state=True)
            self.assertEqual(path.read_bytes(), first)

    def test_missing_key_with_existing_state_is_fatal(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ProfileError):
            ensure_secret(Path(d) / "key", existing_state=True)

    def test_fixed_width_names_and_execution_labels_do_not_prefix_collide(self):
        self.assertEqual(profile_name("deadbeef", 1, 1), "agents-deadbeef-r0000000001-g0001")
        self.assertEqual(mcp_name("deadbeef", 10, 1), "agents-deadbeef-r0000000010-g0001")
        self.assertFalse(mcp_name("deadbeef", 10, 1).startswith(mcp_name("deadbeef", 1, 1)))
        self.assertEqual(
            execution_name("deadbeef", "persistent", "manager", "manager", 1),
            "agents-deadbeef-p-manager-g0001",
        )
        self.assertEqual(
            execution_name("deadbeef", "work", "42", "researcher", 2),
            "agents-deadbeef-w-42-researcher-g0002",
        )

    def test_materialization_clamps_tools_env_and_permissions(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state, purpose_kind="work")
            text = profile.path.read_text()
            self.assertEqual(profile.allowed_tools, ("fs_*", "execute_bash"))
            self.assertIn('"fs_*"', text)
            self.assertIn('"execute_bash"', text)
            self.assertIn(f'"@{profile.mcp_name}"', text)
            self.assertIn("agents-mcp-server", text)
            self.assertIn('AGENTS_AGENT_TOKEN: "secret-token"', text)
            self.assertNotIn("env_vars:", text)
            self.assertEqual(profile.path.stat().st_mode & 0o777, 0o600)
            self.assertIn("only through `task secrets:*`", text)
            self.assertIn("Never read, copy, stage, or pass `.env.sops-age`", text)

    def test_opencode_install_is_native_and_deterministic(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state, purpose_kind="work", reasoning_effort="high")
            provider_home = state / "provider-home"
            config = provider_home / ".aws/opencode/opencode.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"mcp": {"unrelated": {"command": ["keep"]}}}))
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "xdg")}, clear=False):
                launch = install_profile(
                    profile,
                    "opencode_cli",
                    state / "profiles.lock",
                    provider_home=provider_home,
                    agent_auth_id=profile.name,
                    model="openai/gpt-5",
                )
            self.assertEqual(
                launch.argv,
                ("opencode", "--agent", profile.name, "--model", "openai/gpt-5"),
            )
            environment = dict(launch.env)
            self.assertEqual(environment["AGENTS_EXECUTION_ID"], profile.name)
            self.assertEqual(environment["AGENTS_AGENT_TOKEN"], "secret-token")
            self.assertEqual(environment["OPENCODE_CONFIG"], str(config))
            self.assertEqual(environment["OPENCODE_CONFIG_DIR"], str(config.parent))
            self.assertEqual(environment["OPENCODE_DISABLE_AUTOUPDATE"], "1")
            self.assertEqual(environment["OPENCODE_DISABLE_MOUSE"], "1")
            self.assertEqual(environment["OPENCODE_DISABLE_TERMINAL_TITLE"], "1")
            self.assertEqual(environment["OPENCODE_CLIENT"], "agents")
            self.assertEqual(environment["TERM"], "xterm-256color")
            data = json.loads(config.read_text())
            mcp = data["mcp"][profile.mcp_name]
            self.assertEqual(mcp["type"], "local")
            self.assertEqual(mcp["command"], [profile.mcp_command])
            self.assertEqual(mcp["environment"]["AGENTS_EXECUTION_ID"], profile.name)
            self.assertFalse(data["tools"][f"{profile.mcp_name}*"])
            self.assertTrue(data["agent"][profile.name]["tools"][f"{profile.mcp_name}*"])
            agent = provider_home / ".aws/opencode/agents" / f"{profile.name}.md"
            agent_text = agent.read_text()
            self.assertIn('reasoningEffort: "high"', agent_text)
            self.assertIn("permission:", agent_text)
            self.assertIn("  bash: allow", agent_text)
            self.assertIn("  skill: deny", agent_text)
            self.assertEqual(agent.stat().st_mode & 0o777, 0o600)
            self.assertTrue(all(Path(item["path"]).stat().st_mode & 0o777 == 0o600 for item in launch.artifacts))
            agent_record = next(item for item in launch.artifacts if item["kind"] == "agent")
            self.assertEqual(agent_record["sha256"], hashlib.sha256(agent.read_bytes()).hexdigest())
            self.assertEqual(
                provider_lock_path({"XDG_STATE_HOME": str(state / "xdg")}), state / "xdg/agents/provider-config.lock"
            )

    def test_opencode_permission_denies_skill_tool_always(self):
        restricted = profiles_module._tools_to_opencode_permission(("fs_*", "execute_bash"))
        self.assertEqual(restricted["skill"], "deny")
        self.assertEqual(restricted["bash"], "allow")
        wildcard = profiles_module._tools_to_opencode_permission(("*",))
        self.assertEqual(wildcard["skill"], "deny")
        self.assertEqual(wildcard["task"], "allow")
        self.assertEqual(wildcard["bash"], "allow")

    def test_claude_install_uses_manifest_owned_runtime_files_and_filtered_env(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state, provider="claude_code", purpose_kind="work")
            runtime = state / "runtime"
            inherited = {
                "XDG_STATE_HOME": str(state / "xdg"),
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
                "CLAUDE_CODE_EFFORT_LEVEL": "high",
                "CLAUDE_INTERNAL_SESSION": "must-not-leak",
            }
            with patch.dict(os.environ, inherited, clear=False):
                launch = install_profile(
                    profile,
                    "claude_code",
                    state / "profiles.lock",
                    runtime_dir=runtime,
                    agent_auth_id=profile.name,
                    model="claude-sonnet",
                )
            prompt = runtime / f"{profile.name}.prompt"
            mcp = runtime / f"{profile.name}.mcp.json"
            self.assertEqual(
                launch.argv[:7],
                (
                    "claude",
                    "--dangerously-skip-permissions",
                    "--model",
                    "claude-sonnet",
                    "--append-system-prompt-file",
                    str(prompt),
                    "--mcp-config",
                ),
            )
            self.assertIn("--strict-mcp-config", launch.argv)
            self.assertIn("--disallowedTools", launch.argv)
            self.assertNotIn("secret-token", prompt.read_text())
            data = json.loads(mcp.read_text())
            config = data["mcpServers"][profile.mcp_name]
            self.assertEqual(config["env"]["AGENTS_EXECUTION_ID"], profile.name)
            self.assertEqual(config["env"]["AGENTS_AGENT_TOKEN"], "secret-token")
            environment = dict(launch.env)
            self.assertEqual(environment["CLAUDE_CODE_USE_BEDROCK"], "1")
            self.assertEqual(environment["CLAUDE_CODE_SKIP_BEDROCK_AUTH"], "1")
            self.assertEqual(environment["CLAUDE_CODE_EFFORT_LEVEL"], "high")
            self.assertNotIn("CLAUDE_INTERNAL_SESSION", environment)
            self.assertTrue(all(Path(item["path"]).stat().st_mode & 0o777 == 0o600 for item in launch.artifacts))

    def test_mock_install_has_no_user_provider_artifacts(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state, provider="mock_cli")
            provider_home = state / "provider-home"
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "xdg")}, clear=False):
                launch = install_profile(
                    profile,
                    "mock_cli",
                    state / "profiles.lock",
                    provider_home=provider_home,
                    agent_auth_id=profile.name,
                )
            self.assertEqual(launch.argv, ("mock_cli",))
            self.assertEqual(dict(launch.env)["AGENTS_EXECUTION_ID"], profile.name)
            self.assertEqual(tuple(item["kind"] for item in launch.artifacts), ("source",))
            self.assertFalse(provider_home.exists())

    def test_provider_merge_preserves_unrelated_and_rejects_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            merge_owned_json(path, "mcp", "agents-run", {"command": "x"})
            data = json.loads(path.read_text())
            self.assertIn("agents-run", data["mcp"])
            merge_owned_json(path, "mcp", "unrelated", {"x": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ProfileError):
                merge_owned_json(path, "mcp", "agents", {"command": "x"})

    def test_remove_profile_is_tamper_safe_and_preserves_unrelated_config(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state)
            provider_home = state / "provider-home"
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "xdg")}, clear=False):
                launch = install_profile(
                    profile,
                    "opencode_cli",
                    state / "profiles.lock",
                    provider_home=provider_home,
                    agent_auth_id=profile.name,
                )
                config = provider_home / ".aws/opencode/opencode.json"
                data = json.loads(config.read_text())
                data["mcp"]["unrelated"] = {"command": ["keep"]}
                config.write_text(json.dumps(data))
                config.chmod(0o600)
                remove_profile(
                    profile.name,
                    profile.path,
                    list(launch.artifacts),
                    state / "profiles.lock",
                    provider_home=provider_home,
                )
            self.assertFalse(profile.path.exists())
            self.assertFalse((provider_home / ".aws/opencode/agents" / f"{profile.name}.md").exists())
            remaining = json.loads(config.read_text())
            self.assertEqual(remaining["mcp"], {"unrelated": {"command": ["keep"]}})

    def test_remove_profile_rejects_tampered_provider_fragment(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state)
            provider_home = state / "provider-home"
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "xdg")}, clear=False):
                launch = install_profile(
                    profile,
                    "opencode_cli",
                    state / "profiles.lock",
                    provider_home=provider_home,
                    agent_auth_id=profile.name,
                )
                config = provider_home / ".aws/opencode/opencode.json"
                data = json.loads(config.read_text())
                data["mcp"][profile.mcp_name]["environment"]["AGENTS_EXECUTION_ID"] = "wrong"
                config.write_text(json.dumps(data))
                config.chmod(0o600)
                with self.assertRaises(ProfileError):
                    remove_profile(
                        profile.name,
                        profile.path,
                        list(launch.artifacts),
                        state / "profiles.lock",
                        provider_home=provider_home,
                    )
            self.assertTrue(profile.path.exists())

    def test_claude_runtime_artifacts_are_removed(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            profile = self._materialize(root, state, provider="claude_code")
            runtime = state / "runtime"
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "xdg")}, clear=False):
                launch = install_profile(
                    profile,
                    "claude_code",
                    state / "profiles.lock",
                    runtime_dir=runtime,
                    agent_auth_id=profile.name,
                )
                remove_profile(
                    profile.name,
                    profile.path,
                    list(launch.artifacts),
                    state / "profiles.lock",
                    runtime_dir=runtime,
                )
            self.assertFalse(profile.path.exists())
            self.assertFalse((runtime / f"{profile.name}.prompt").exists())
            self.assertFalse((runtime / f"{profile.name}.mcp.json").exists())

    def test_project_opencode_config_denies_skill_permission(self):
        root = Path(__file__).parents[1]
        data = json.loads((root / "opencode.json").read_text())
        self.assertEqual(data["permission"]["skill"], {"*": "deny"})


if __name__ == "__main__":
    unittest.main()
