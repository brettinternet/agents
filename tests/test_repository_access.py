from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agents.repository_access import RepositoryAccessError, list_repository, read_repository


class RepositoryAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._git("init", "-q")
        self._git("config", "user.name", "Agent Test")
        self._git("config", "user.email", "agent@example.test")
        self._write("README.md", "public\n")
        self._write("memory/note.md", "remembered\n")
        self._write(".agents/state", "private state\n")
        self._write(".env.local", "TOKEN=private\n")
        self._write(".env.sops-age", "private identity\n")
        self._write(".env.example", "TOKEN=example\n")
        self._write(".env.schema", "TOKEN=\n")
        self._write(".env.production", "TOKEN=private\n")
        self._write(".git-credentials", "private credentials\n")
        self._write(".env.staging/secret.txt", "private\n")
        self._write("credentials.toml/secret.txt", "private\n")
        self._write("private.key/secret.txt", "private\n")
        self._write("agent-secrets.sops.json", "ciphertext\n")
        self._write(".ssh/id_rsa", "private key\n")
        for path in (
            ".envrc",
            ".envrc.local",
            ".htpasswd",
            ".pgpass",
            "auth_config.toml",
            "aws_credentials",
            "client_secret.json",
            "client-secrets.json",
            "config/credentials_prod",
            "google-credentials.json",
            "id_ecdsa_sk",
            "id_sk",
            "id_x25519",
            "oauth-config.yaml",
            "oauth_config.yaml",
            "passwords.yaml",
            "private.asc",
            "private.jks",
            "private.p8",
            "private.sops.yaml",
            "private_token.txt",
            "service-account.json",
            "service_account.json",
            "authorized_keys2",
            "ssh_host_ecdsa_key",
            "ssh_host_ed25519_key",
            "ssh_host_rsa_key",
        ):
            self._write(path, "private\n")
        self._write(".ssh/id_ecdsa", "private key\n")
        self._write(".aws/credentials.json", "private credentials\n")
        self._write("PRIVATE.PEM", "private key\n")
        self._write("certificate.pem", "private certificate\n")
        (self.root / "outside.txt").write_text("outside\n")
        (self.root / "linked.md").symlink_to("outside.txt")
        self._git("add", "-A")
        self._git("commit", "-qm", "fixture")
        self._write("untracked.md", "not committed\n")

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def _write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def test_lists_only_committed_public_regular_files(self):
        self.assertEqual(
            list_repository(self.root),
            [".env.example", ".env.schema", "README.md", "memory/note.md", "outside.txt"],
        )
        self.assertEqual(list_repository(self.root, "memory"), ["memory/note.md"])

    def test_reads_committed_content_instead_of_worktree_changes(self):
        self._write("README.md", "uncommitted private change\n")
        self.assertEqual(read_repository(self.root, "README.md"), "public\n")
        self.assertEqual(read_repository(self.root, "memory/note.md"), "remembered\n")

    def test_denies_sensitive_untracked_traversal_and_symlink_paths(self):
        denied = (
            ".agents/state",
            ".env.local",
            ".env.sops-age",
            ".env.production",
            ".git-credentials",
            ".env.staging/secret.txt",
            "credentials.toml/secret.txt",
            "private.key/secret.txt",
            "agent-secrets.sops.json",
            ".ssh/id_rsa",
            "certificate.pem",
            ".ssh/id_ecdsa",
            ".aws/credentials.json",
            ".envrc",
            ".htpasswd",
            ".pgpass",
            "client_secret.json",
            "config/credentials_prod",
            "id_ecdsa_sk",
            "id_sk",
            "id_x25519",
            "private.asc",
            "private.jks",
            "private.p8",
            "private.sops.yaml",
            "service-account.json",
            ".envrc.local",
            "auth_config.toml",
            "aws_credentials",
            "client-secrets.json",
            "google-credentials.json",
            "oauth-config.yaml",
            "oauth_config.yaml",
            "passwords.yaml",
            "private_token.txt",
            "service_account.json",
            "authorized_keys2",
            "ssh_host_ecdsa_key",
            "ssh_host_ed25519_key",
            "ssh_host_rsa_key",
            "PRIVATE.PEM",
            "linked.md",
            "untracked.md",
            "../README.md",
        )
        for path in denied:
            with self.subTest(path=path), self.assertRaises(RepositoryAccessError):
                read_repository(self.root, path)

        for path in (".agents", ".ssh", "linked.md", ".."):
            with self.subTest(list_path=path), self.assertRaises(RepositoryAccessError):
                list_repository(self.root, path)


if __name__ == "__main__":
    unittest.main()
