from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import secret_store


class SecretStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = Path(secret_store.__file__).resolve()

    def make_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        (root / ".env.schema").write_text(
            "# @defaultSensitive=false @defaultRequired=false\n"
            "# ---\n"
            "# @sensitive\n"
            "DEMO_TOKEN=\n"
            "# Public test value.\n"
            "NOT_SENSITIVE=\n"
        )
        (root / ".env.local").touch()

    def cli(
        self,
        root: Path,
        *args: str,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            cwd=root,
            env=env,
            input=input_bytes,
            capture_output=True,
            check=False,
        )

    def test_path_permissions_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "identity"
            regular.write_text("not a real identity")
            regular.chmod(0o644)
            with self.assertRaisesRegex(secret_store.SecretStoreError, "unsafe permissions"):
                secret_store._require_file(regular, "age identity", private=True)

            home = root / "home"
            home.mkdir(mode=0o700)
            home.chmod(0o755)
            with self.assertRaisesRegex(secret_store.SecretStoreError, "unsafe permissions"):
                secret_store._ensure_home(home, create=False)

            target = root / "target"
            target.write_text("x")
            symlink = root / "config"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(secret_store.SecretStoreError, "symlink"):
                secret_store._require_file(symlink, "SOPS config")

    def test_missing_identity_with_tracked_artifacts_does_not_rotate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            (root / ".sops.yaml").write_text("tracked config\n")
            (root / "agent-secrets.sops.json").write_text("{}\n")

            result = self.cli(root, "init")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"restore the identity", result.stderr)
            self.assertFalse((root / ".env.sops-age").exists())
            self.assertEqual((root / ".sops.yaml").read_text(), "tracked config\n")
            self.assertEqual((root / "agent-secrets.sops.json").read_text(), "{}\n")

    def test_name_and_sensitive_schema_enforcement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            self.assertEqual(self.cli(root, "init").returncode, 0)

            invalid = self.cli(root, "set", "lower-case", input_bytes=b"not-exposed")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn(b"[A-Z_][A-Z0-9_]*", invalid.stderr)
            self.assertNotIn(b"not-exposed", invalid.stderr)

            marker = self.cli(root, "set", secret_store.FORMAT_KEY, input_bytes=b"not-exposed")
            self.assertNotEqual(marker.returncode, 0)
            self.assertIn(b"format marker", marker.stderr)
            self.assertNotIn(b"not-exposed", marker.stderr)

            nonsensitive = self.cli(root, "set", "NOT_SENSITIVE", input_bytes=b"not-exposed")
            self.assertNotEqual(nonsensitive.returncode, 0)
            self.assertIn(b"not declared sensitive", nonsensitive.stderr)
            self.assertNotIn(b"not-exposed", nonsensitive.stderr)

    def test_set_update_list_reveal_run_and_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            initialized = self.cli(root, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            self.assertEqual(stat.S_IMODE((root / ".env.sops-age").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / ".sops-isolated-home").stat().st_mode), 0o700)
            self.assertEqual(self.cli(root, "check").returncode, 0)

            created = self.cli(root, "set", "DEMO_TOKEN", input_bytes=b"alpha-bravo")
            self.assertEqual(created.returncode, 0, created.stderr.decode())
            self.assertNotIn(b"alpha-bravo", created.stderr)
            self.assertEqual(self.cli(root, "list").stdout, b"DEMO_TOKEN\n")
            self.assertEqual(self.cli(root, "reveal", "DEMO_TOKEN").stdout, b"alpha-bravo")

            updated = self.cli(root, "set", "DEMO_TOKEN", input_bytes=b"charlie-delta")
            self.assertEqual(updated.returncode, 0, updated.stderr.decode())
            self.assertEqual(self.cli(root, "reveal", "DEMO_TOKEN").stdout, b"charlie-delta")

            normal_home = root / "normal-home"
            normal_xdg = root / "normal-xdg"
            normal_home.mkdir()
            normal_xdg.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(normal_home),
                    "XDG_CONFIG_HOME": str(normal_xdg),
                    "SOPS_AGE_KEY_FILE": "/should/not/leak",
                    "SOPS_KMS_ARN": "should-not-leak",
                }
            )
            program = (
                "import os; "
                "ok = (os.environ.get('DEMO_TOKEN') == 'charlie-delta' "
                "and 'SOPS_AGE_KEY_FILE' not in os.environ "
                "and 'SOPS_KMS_ARN' not in os.environ "
                f"and os.environ.get('HOME') == {str(normal_home)!r} "
                f"and os.environ.get('XDG_CONFIG_HOME') == {str(normal_xdg)!r}); "
                "print('ok' if ok else 'bad')"
            )
            ran = self.cli(root, "run", "--", sys.executable, "-c", program, env=env)
            self.assertEqual(ran.returncode, 0, ran.stderr.decode())
            self.assertEqual(ran.stdout, b"ok\n")

            removed = self.cli(root, "unset", "DEMO_TOKEN")
            self.assertEqual(removed.returncode, 0, removed.stderr.decode())
            self.assertEqual(self.cli(root, "list").stdout, b"")
            self.assertEqual(self.cli(root, "check").returncode, 0)
            missing = self.cli(root, "reveal", "DEMO_TOKEN")
            self.assertNotEqual(missing.returncode, 0)
            self.assertNotIn(b"alpha-bravo", missing.stderr)
            self.assertNotIn(b"charlie-delta", missing.stderr)

    def test_set_value_uses_stdin_and_never_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = secret_store.Paths(
                worktree=root,
                common_root=root,
                config=root / ".sops.yaml",
                store=root / "agent-secrets.sops.json",
                key=root / ".env.sops-age",
                isolated_home=root / ".sops-isolated-home",
            )
            calls: list[tuple[list[str], bytes | None, str]] = []

            def capture_run(
                argv: list[str],
                *,
                cwd: Path,
                env: dict[str, str] | None = None,
                input_bytes: bytes | None = None,
                failure: str,
            ) -> bytes:
                del cwd, env
                calls.append((argv, input_bytes, failure))
                return b""

            with (
                patch.object(secret_store, "_decrypt", return_value={}),
                patch.object(secret_store, "_validate_schema"),
                patch.object(secret_store, "_read_value", return_value="alpha-bravo"),
                patch.object(secret_store, "_run", side_effect=capture_run),
            ):
                secret_store.set_secret(paths, "DEMO_TOKEN")

            argv, input_bytes, failure = calls[-1]
            self.assertNotIn("alpha-bravo", argv)
            self.assertNotIn("alpha-bravo", failure)
            self.assertEqual(input_bytes, b'"alpha-bravo"')
            self.assertIn("--value-stdin", argv)

    def test_sops_environment_isolated_from_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = secret_store.Paths(
                worktree=root,
                common_root=root,
                config=root / ".sops.yaml",
                store=root / "agent-secrets.sops.json",
                key=root / ".env.sops-age",
                isolated_home=root / ".sops-isolated-home",
            )
            with patch.dict(
                os.environ,
                {
                    "HOME": "/normal-home",
                    "XDG_CONFIG_HOME": "/normal-xdg",
                    "SOPS_AGE_KEY_FILE": "/host/key",
                    "SOPS_KMS_ARN": "host-kms",
                    "SOPS_GPG_EXEC": "host-gpg",
                },
                clear=True,
            ):
                sops_env = secret_store._sops_env(paths)
                command_env = secret_store._command_env({"DEMO_TOKEN": "value"})

            self.assertEqual(sops_env["SOPS_AGE_KEY_FILE"], str(paths.key))
            self.assertEqual(sops_env["SOPS_DECRYPTION_ORDER"], "age")
            self.assertEqual(sops_env["HOME"], str(paths.isolated_home))
            self.assertEqual(sops_env["XDG_CONFIG_HOME"], str(paths.isolated_home))
            self.assertNotIn("SOPS_KMS_ARN", sops_env)
            self.assertNotIn("SOPS_GPG_EXEC", sops_env)
            self.assertEqual(command_env["HOME"], "/normal-home")
            self.assertEqual(command_env["XDG_CONFIG_HOME"], "/normal-xdg")
            self.assertNotIn("SOPS_AGE_KEY_FILE", command_env)
            self.assertNotIn("SOPS_KMS_ARN", command_env)
            self.assertEqual(command_env["DEMO_TOKEN"], "value")


if __name__ == "__main__":
    unittest.main()
