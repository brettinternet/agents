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
    install_profile,
    materialize_profile,
    mcp_name,
    merge_owned_json,
    profile_name,
    provider_lock_path,
    validate_templates,
)


class ProfileTests(unittest.TestCase):
    def test_repository_templates_validate(self):
        validate_templates(Path(__file__).resolve().parents[1])

    def test_packaged_templates_fallback_when_editable_templates_are_absent(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "editable"
            root.mkdir()
            package = Path(d) / "installed_package"
            package.mkdir()
            for name in ("elder", "explorer", "yapper"):
                (package / f"{name}.md").write_bytes((repository / "agents" / f"{name}.md").read_bytes())
            with patch.object(profiles_module, "__file__", str(package / "profiles.py")):
                validate_templates(root)
                materialized = materialize_profile(
                    root,
                    Path(d) / "state",
                    template="elder",
                    instance="deadbeef",
                    run_id=1,
                    generation=1,
                    provider="opencode_cli",
                    purpose_kind="persistent",
                    specialty="",
                    token="secret-token",
                    api_url="http://127.0.0.1:9890",
                )
            self.assertIn("You are the elder", materialized.path.read_text())

    def test_codex_provider_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as d,
            self.assertRaisesRegex(ProfileError, "unsupported CAO provider capability"),
        ):
            materialize_profile(
                Path(__file__).parents[1],
                Path(d),
                template="elder",
                instance="deadbeef",
                run_id=1,
                generation=1,
                provider="codex",
                purpose_kind="persistent",
                specialty="",
                token="secret-token",
                api_url="http://127.0.0.1:9890",
            )

    def test_secret_creation_is_private_and_not_rotated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "key"
            ensure_secret(p, existing_state=False)
            first = p.read_bytes()
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            ensure_secret(p, existing_state=True)
            self.assertEqual(p.read_bytes(), first)

    def test_missing_key_with_existing_state_is_fatal(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ProfileError):
            ensure_secret(Path(d) / "key", existing_state=True)

    def test_fixed_width_names_do_not_prefix_collide(self):
        self.assertEqual(profile_name("deadbeef", 1, 1), "agents-deadbeef-r0000000001-g0001")
        self.assertEqual(mcp_name("deadbeef", 10, 1), "agents-deadbeef-r0000000010-g0001")
        self.assertFalse(mcp_name("deadbeef", 10, 1).startswith(mcp_name("deadbeef", 1, 1)))

    def test_materialization_clamps_tools_env_and_permissions(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            p = materialize_profile(
                root,
                state,
                template="explorer",
                instance="deadbeef",
                run_id=1,
                generation=1,
                provider="opencode_cli",
                purpose_kind="work",
                specialty="research",
                token="secret-token",
                api_url="http://127.0.0.1:9890",
            )
            text = p.path.read_text()
            self.assertEqual(p.allowed_tools, ("fs_*", "execute_bash"))
            self.assertIn('"fs_*"', text)
            self.assertIn('"execute_bash"', text)
            self.assertNotIn("@builtin", text)
            self.assertIn(f'"@{p.mcp_name}"', text)
            self.assertIn("agents-mcp-server", text)
            self.assertIn("env:", text)
            self.assertIn('AGENTS_AGENT_TOKEN: "secret-token"', text)
            self.assertNotIn("env_vars:", text)
            self.assertEqual(p.path.stat().st_mode & 0o777, 0o600)
            self.assertIn("only through `task secrets:*`", text)
            self.assertIn("Never read, copy, stage, or pass `.env.sops-age`", text)
            self.assertIn("Never read Agents state or human routes", text)
            claude = materialize_profile(
                root,
                state,
                template="elder",
                instance="deadbeef",
                run_id=2,
                generation=1,
                provider="claude_code",
                purpose_kind="persistent",
                specialty="",
                token="secret-token",
                api_url="http://127.0.0.1:9890",
            )
            claude_text = claude.path.read_text()
            self.assertEqual(claude.allowed_tools, ())
            self.assertIn('AGENTS_AGENT_TOKEN: "secret-token"', claude_text)
            self.assertNotIn("@builtin", claude_text)
            self.assertNotIn('"fs_read"', claude_text)
            self.assertNotIn('"fs_list"', claude_text)
            self.assertIn("only through `task secrets:*`", claude_text)
            self.assertIn("Never read, copy, stage, or pass `.env.sops-age`", claude_text)
            self.assertIn(f'"@{claude.mcp_name}"', claude_text)
            self.assertIn("agents-mcp-server", claude_text)

    def test_opencode_install_restores_reasoning_dropped_by_cao_adapter(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            materialized = materialize_profile(
                root,
                state,
                template="elder",
                instance="deadbeef",
                run_id=4,
                generation=1,
                provider="opencode_cli",
                purpose_kind="persistent",
                specialty="",
                token="reasoning-token",
                api_url="http://127.0.0.1:9890",
                reasoning_effort="high",
            )
            cao = state / "cao"
            cao.write_text(
                f"""#!/usr/bin/env python3
import json,os,pathlib,sys
name={materialized.name!r}
mcp={materialized.mcp_name!r}
if sys.argv[1]=="install":
 root=pathlib.Path.home()/".aws/opencode"
 (root/"agents").mkdir(parents=True)
 (root/"agents"/f"{{name}}.md").write_text("---\\ndescription: staged\\nmode: all\\n---\\nbody\\n")
 (root/"opencode.json").write_text(json.dumps({{"mcp":{{mcp:{{"env":{{"AGENTS_AGENT_TOKEN":"reasoning-token"}}}}}}}}))
 print("Successfully installed agent profile: "+name)
else:
 print(json.dumps({{"name":name}}))
"""
            )
            cao.chmod(0o700)
            provider_home = state / "provider-home"
            provider_home.mkdir()
            with patch.dict(
                os.environ,
                {"HOME": str(provider_home), "XDG_STATE_HOME": str(state / "xdg")},
            ):
                artifacts = install_profile(
                    cao,
                    state / "cao-home",
                    materialized,
                    "opencode_cli",
                    state / "profiles.lock",
                )
            installed = provider_home / ".aws/opencode/agents" / f"{materialized.name}.md"
            self.assertIn('reasoningEffort: "high"', installed.read_text())
            agent_artifact = next(artifact for artifact in artifacts if artifact["kind"] == "agent")
            self.assertEqual(agent_artifact["sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())

    def test_provider_merge_preserves_unrelated_and_rejects_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"mcp": {"unrelated": {"x": 1}}}))
            merge_owned_json(path, "mcp", "agents-run", {"command": "x"})
            self.assertIn("unrelated", json.loads(path.read_text())["mcp"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ProfileError):
                merge_owned_json(path, "mcp", "agents", {"command": "x"})

    def test_staged_home_install_accepts_cao_paths_and_requires_exact_success_line(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            p = materialize_profile(
                root,
                state,
                template="elder",
                instance="deadbeef",
                run_id=3,
                generation=1,
                provider="mock_cli",
                purpose_kind="persistent",
                specialty="",
                token="token",
                api_url="http://127.0.0.1:9890",
            )
            cao = state / "cao"
            cao.write_text(
                f'#!/usr/bin/env python3\nimport json,os,sys\nif sys.argv[1]=="install":\n print("Successfully installed agent profile: {p.name}")\n print("Context file: "+os.environ["HOME"]+"/agent-context/{p.name}.md")\nelse: print(json.dumps({{"name":"{p.name}"}}))\n'
            )
            cao.chmod(0o700)
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "xdg")}):
                artifacts = install_profile(cao, state / "cao-home", p, "mock_cli", state / "profiles.lock")
            self.assertIsInstance(artifacts, list)
            self.assertTrue(all(Path(artifact["path"]).is_file() for artifact in artifacts))
            self.assertTrue(all(Path(artifact["path"]).stat().st_mode & 0o777 == 0o600 for artifact in artifacts))
            self.assertEqual(
                provider_lock_path({"XDG_STATE_HOME": str(state / "xdg")}),
                state / "xdg/agents/provider-config.lock",
            )
            cao.write_text("#!/usr/bin/env python3\n")
            cao.chmod(0o700)
            with (
                patch.dict(os.environ, {"XDG_STATE_HOME": str(state / "other")}),
                self.assertRaises(ProfileError),
            ):
                install_profile(cao, state / "cao-home", p, "mock_cli", state / "profiles.lock")
