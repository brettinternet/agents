from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            "# @sensitive\n"
            "OTHER_TOKEN=\n"
            "# Public test value.\n"
            "NOT_SENSITIVE=\n"
        )
        (root / ".env.local").touch()

    def make_paths(self, root: Path) -> secret_store.Paths:
        return secret_store.Paths(
            worktree=root,
            common_root=root,
            config=root / ".sops.yaml",
            store=root / "agent-secrets.sops.json",
            key=root / ".env.sops-age",
            isolated_home=root / ".sops-isolated-home",
            lock=root / "secret-store.lock",
        )

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

            unsafe_lock = root / "unsafe.lock"
            unsafe_lock.touch(mode=0o644)
            unsafe_lock.chmod(0o644)
            with (
                self.assertRaisesRegex(secret_store.SecretStoreError, "unsafe"),
                secret_store._exclusive_lock(unsafe_lock),
            ):
                self.fail("unsafe lock was acquired")

            lock_target = root / "lock-target"
            lock_target.touch(mode=0o600)
            symlink_lock = root / "symlink.lock"
            symlink_lock.symlink_to(lock_target)
            with (
                self.assertRaisesRegex(secret_store.SecretStoreError, "unsafe"),
                secret_store._exclusive_lock(symlink_lock),
            ):
                self.fail("symlink lock was acquired")

            safe_lock = root / "safe.lock"
            acquired = threading.Event()

            def acquire_same_lock() -> None:
                with secret_store._exclusive_lock(safe_lock):
                    acquired.set()

            with secret_store._exclusive_lock(safe_lock):
                waiter = threading.Thread(target=acquire_same_lock)
                waiter.start()
                self.assertFalse(acquired.wait(0.1))
                self.assertEqual(safe_lock.read_bytes(), b"")
            waiter.join(timeout=1)
            self.assertTrue(acquired.is_set())
            self.assertFalse(waiter.is_alive())

    def test_linked_worktrees_resolve_the_same_common_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            linked = Path(directory) / "linked"
            self.make_repository(root)
            (root / "tracked").touch()
            subprocess.run(["git", "add", "tracked"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Synthetic Test",
                    "-c",
                    "user.email=test.invalid@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(linked)],
                cwd=root,
                check=True,
                capture_output=True,
            )

            root_paths = secret_store.resolve_paths(root)
            linked_paths = secret_store.resolve_paths(linked)

            self.assertEqual(root_paths.lock, linked_paths.lock)
            self.assertEqual(root_paths.lock, (root / ".git").resolve() / secret_store.LOCK_NAME)

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

    def test_constrained_sensitive_schema_rejects_real_value_without_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            with (root / ".env.schema").open("a") as schema:
                schema.write("# @sensitive @type=port\nPORT_TOKEN=\n")
            self.assertEqual(self.cli(root, "init").returncode, 0)

            rejected = self.cli(root, "set", "PORT_TOKEN", input_bytes=b"not-a-port")

            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn(b"not-a-port", rejected.stderr)
            self.assertNotIn(b"PORT_TOKEN", self.cli(root, "list").stdout)

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
                "and 'OTHER_TOKEN' not in os.environ "
                "and '__VARLOCK_ENV' not in os.environ "
                "and 'SOPS_AGE_KEY_FILE' not in os.environ "
                "and 'SOPS_KMS_ARN' not in os.environ "
                f"and os.environ.get('HOME') == {str(normal_home)!r} "
                f"and os.environ.get('XDG_CONFIG_HOME') == {str(normal_xdg)!r}); "
                "print('ok' if ok else 'bad')"
            )
            ran = self.cli(root, "run", "DEMO_TOKEN", "--", sys.executable, "-c", program, env=env)
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

    def test_protocol_set_preserves_exact_binary_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            self.assertEqual(self.cli(root, "init").returncode, 0)
            paths = self.make_paths(root)
            value = b"\xffalpha\0omega"

            secret_store.set_secret_value(paths, "DEMO_TOKEN", value)

            self.assertEqual(secret_store.broker_byte_values(paths, ["DEMO_TOKEN"]), {"DEMO_TOKEN": value})
            with self.assertRaisesRegex(secret_store.SecretStoreError, "environment value"):
                secret_store.broker_values(paths, ["DEMO_TOKEN"])

    def test_run_selection_filtering_argv_validation_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            self.assertEqual(self.cli(root, "init").returncode, 0)
            self.assertEqual(
                self.cli(root, "set", "DEMO_TOKEN", input_bytes=b"selected-value").returncode,
                0,
            )
            self.assertEqual(
                self.cli(root, "set", "OTHER_TOKEN", input_bytes=b"unselected-value").returncode,
                0,
            )

            environment_check = (
                "import os,sys; "
                "sys.exit(0 if (os.environ.get('DEMO_TOKEN') == 'selected-value' "
                "and 'OTHER_TOKEN' not in os.environ "
                "and '__VARLOCK_ENV' not in os.environ) else 3)"
            )
            selected = self.cli(
                root,
                "run",
                "DEMO_TOKEN",
                "--",
                sys.executable,
                "-c",
                environment_check,
                env={**os.environ, "__VARLOCK_ENV": "synthetic-aggregate"},
            )
            self.assertEqual(selected.returncode, 0, selected.stderr.decode())

            expected_argv = ["semi;colon", 'embedded"quote', "--tail"]
            argv_result = self.cli(
                root,
                "run",
                "DEMO_TOKEN",
                "--",
                sys.executable,
                "-c",
                "import json,sys; print(json.dumps(sys.argv[1:]))",
                *expected_argv,
            )
            self.assertEqual(argv_result.returncode, 0, argv_result.stderr.decode())
            self.assertEqual(json.loads(argv_result.stdout), expected_argv)

            redacted = self.cli(
                root,
                "run",
                "DEMO_TOKEN",
                "--",
                sys.executable,
                "-c",
                "import os,sys; print(os.environ['DEMO_TOKEN']); print(os.environ['DEMO_TOKEN'], file=sys.stderr)",
            )
            self.assertEqual(redacted.returncode, 0)
            self.assertNotIn(b"selected-value", redacted.stdout)
            self.assertNotIn(b"selected-value", redacted.stderr)

            rejected = (
                self.cli(root, "run", "--", sys.executable, "-c", "pass"),
                self.cli(root, "run", "DEMO_TOKEN"),
                self.cli(root, "run", "UNKNOWN_TOKEN", "--", sys.executable, "-c", "pass"),
                self.cli(
                    root,
                    "run",
                    "DEMO_TOKEN",
                    "DEMO_TOKEN",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ),
            )
            for result in rejected:
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"selected-value", result.stderr)
                self.assertNotIn(b"unselected-value", result.stderr)

    def test_set_rejects_invalid_complete_proposed_store_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(Path(directory))
            validated: list[dict[str, str]] = []

            def reject_invalid(_paths: secret_store.Paths, values: dict[str, str]) -> None:
                validated.append(dict(values))
                if values.get("OTHER_TOKEN") == "invalid":
                    raise secret_store.SecretStoreError("schema rejected proposed store")

            with (
                patch.object(secret_store, "_decrypt", return_value={"OTHER_TOKEN": "invalid"}),
                patch.object(secret_store, "_validate_schema", side_effect=reject_invalid),
                patch.object(secret_store, "_read_value", return_value="new-value"),
                patch.object(secret_store, "_run") as mutation,
                self.assertRaisesRegex(secret_store.SecretStoreError, "proposed store"),
            ):
                secret_store.set_secret(paths, "DEMO_TOKEN")

            self.assertEqual(validated, [{"OTHER_TOKEN": "invalid", "DEMO_TOKEN": "new-value"}])
            mutation.assert_not_called()

    def test_set_validates_complete_persisted_store_after_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(Path(directory))
            validated: list[dict[str, str]] = []
            persisted = {"DEMO_TOKEN": "new-value", "OTHER_TOKEN": "persisted-value"}

            with (
                patch.object(secret_store, "_decrypt", side_effect=[{"OTHER_TOKEN": "old-value"}, persisted]),
                patch.object(
                    secret_store,
                    "_validate_schema",
                    side_effect=lambda _paths, values: validated.append(dict(values)),
                ),
                patch.object(secret_store, "_read_value", return_value="new-value"),
                patch.object(secret_store, "_run"),
            ):
                secret_store.set_secret(paths, "DEMO_TOKEN")

            self.assertEqual(
                validated,
                [
                    {"OTHER_TOKEN": "old-value", "DEMO_TOKEN": "new-value"},
                    persisted,
                ],
            )

    def test_unset_validates_remaining_proposed_and_persisted_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(Path(directory))
            existing = {"DEMO_TOKEN": "remove", "OTHER_TOKEN": "keep"}
            persisted = {"OTHER_TOKEN": "persisted"}
            validated: list[dict[str, str]] = []

            with (
                patch.object(secret_store, "_decrypt", side_effect=[existing, persisted]),
                patch.object(
                    secret_store,
                    "_validate_schema",
                    side_effect=lambda _paths, values: validated.append(dict(values)),
                ),
                patch.object(secret_store, "_run"),
            ):
                secret_store.unset_secret(paths, "DEMO_TOKEN")

            self.assertEqual(validated, [{"OTHER_TOKEN": "keep"}, persisted])

            with (
                patch.object(secret_store, "_decrypt", return_value=existing),
                patch.object(
                    secret_store,
                    "_validate_schema",
                    side_effect=secret_store.SecretStoreError("invalid remaining store"),
                ),
                patch.object(secret_store, "_run") as mutation,
                self.assertRaisesRegex(secret_store.SecretStoreError, "remaining store"),
            ):
                secret_store.unset_secret(paths, "DEMO_TOKEN")
            mutation.assert_not_called()

    def test_concurrent_writers_preserve_both_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(Path(directory))
            state: dict[str, str] = {}
            state_guard = threading.Lock()
            start = threading.Barrier(3)
            errors: list[Exception] = []
            values = {"writer-one": "first-value", "writer-two": "second-value"}

            def decrypt(_paths: secret_store.Paths) -> dict[str, str]:
                with state_guard:
                    return dict(state)

            def mutate(
                argv: list[str],
                *,
                cwd: Path,
                env: dict[str, str] | None = None,
                input_bytes: bytes | None = None,
                failure: str,
            ) -> bytes:
                del cwd, env, failure
                name = json.loads(argv[-1])[0]
                value = json.loads((input_bytes or b"").decode())
                with state_guard:
                    proposed = dict(state)
                time.sleep(0.05)
                proposed[name] = value
                with state_guard:
                    state.clear()
                    state.update(proposed)
                return b""

            def write_secret(name: str) -> None:
                try:
                    start.wait()
                    secret_store.set_secret(paths, name)
                except Exception as exc:
                    errors.append(exc)

            with (
                patch.object(secret_store, "_decrypt", side_effect=decrypt),
                patch.object(secret_store, "_validate_schema"),
                patch.object(
                    secret_store,
                    "_read_value",
                    side_effect=lambda: values[threading.current_thread().name],
                ),
                patch.object(secret_store, "_run", side_effect=mutate),
            ):
                writers = [
                    threading.Thread(target=write_secret, args=("DEMO_TOKEN",), name="writer-one"),
                    threading.Thread(target=write_secret, args=("OTHER_TOKEN",), name="writer-two"),
                ]
                for writer in writers:
                    writer.start()
                start.wait()
                for writer in writers:
                    writer.join(timeout=2)

            self.assertEqual(errors, [])
            self.assertTrue(all(not writer.is_alive() for writer in writers))
            self.assertEqual(
                state,
                {"DEMO_TOKEN": "first-value", "OTHER_TOKEN": "second-value"},
            )

    def test_set_value_uses_stdin_and_never_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.make_paths(root)
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
            paths = self.make_paths(root)
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

    def test_agent_api_transport_authenticates_and_keeps_secret_out_of_url_and_headers(self):
        response = MagicMock()
        response.json.return_value = {"ok": True, "data": {}}
        environment = {
            "AGENTS_SECRETS_TRANSPORT": "agent-api",
            "AGENTS_API_URL": "http://host.docker.internal:9890",
            "AGENTS_AGENT_TOKEN": "run-token",
            "AGENTS_EXECUTION_ID": "execution-id",
        }
        secret = b"exact-secret-bytes"
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(secret_store.httpx, "post", return_value=response) as post,
        ):
            secret_store._agent_api_request(
                "set",
                {"name": "DEMO_TOKEN", "value_base64": secret_store.base64.b64encode(secret).decode()},
            )
        call = post.call_args
        self.assertEqual(call.args[0], "http://host.docker.internal:9890/agent/v1/secrets/set")
        self.assertEqual(
            call.kwargs["headers"],
            {"Authorization": "Bearer run-token", "X-Agents-Execution-ID": "execution-id"},
        )
        self.assertNotIn(secret.decode(), call.args[0])
        self.assertNotIn(secret.decode(), json.dumps(call.kwargs["headers"]))
        self.assertEqual(
            secret_store.base64.b64decode(call.kwargs["json"]["value_base64"]),
            secret,
        )


if __name__ == "__main__":
    unittest.main()
