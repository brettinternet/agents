from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MAX_FILE_BYTES = 1024 * 1024
_MAX_LIST_ENTRIES = 2000
_DENIED_COMPONENTS = frozenset(
    {".agents", ".aws", ".azure", ".config", ".docker", ".git", ".gnupg", ".kube", ".sops-isolated-home", ".ssh"}
)
_DENIED_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.sops-age",
        ".envrc",
        ".git-credentials",
        ".gitconfig",
        ".htpasswd",
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "agent-secrets.sops.json",
        "api_token",
        "authorized_keys",
        "client_secret",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ecdsa_sk",
        "id_ed25519",
        "id_rsa",
        "id_sk",
        "id_x25519",
        "known_hosts",
        "password",
        "secret",
        "secrets",
        "token",
    }
)
_SENSITIVE_AUTH_TOKENS = frozenset(
    {
        "auth",
        "credential",
        "credentials",
        "oauth",
        "password",
        "passwords",
        "private",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_AUTH_CONFIG_SUFFIXES = frozenset({"", ".json", ".toml", ".txt", ".yaml", ".yml"})
_PUBLIC_ENV_NAMES = frozenset({".env.example", ".env.schema"})
_DENIED_SUFFIXES = (".age", ".asc", ".cer", ".crt", ".der", ".gpg", ".jks", ".key", ".p12", ".p8", ".pem", ".pfx")


class RepositoryAccessError(ValueError):
    pass


@dataclass(frozen=True)
class _Entry:
    mode: str
    object_id: str
    path: str


def _git(project: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RepositoryAccessError("committed repository state is unavailable")
    return result.stdout


def _path(value: str, *, root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RepositoryAccessError("repository path is invalid")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RepositoryAccessError("repository path must stay within the repository")
    normalized = candidate.as_posix()
    if normalized == ".":
        if root:
            return ""
        raise RepositoryAccessError("repository file path is required")
    if any(part in {"", "."} for part in candidate.parts):
        raise RepositoryAccessError("repository path is invalid")
    return normalized


def _denied(path: str) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    for component in parts:
        env_file = (
            component == ".env"
            or component.startswith(".envrc")
            or (component.startswith(".env.") and component not in _PUBLIC_ENV_NAMES)
        )
        credential_file = component.startswith(("credentials.", "credentials_"))
        managed_secret = ".sops." in component and component != ".sops.yaml"
        ssh_artifact = component.startswith("authorized_keys") or (
            component.startswith("ssh_host_") and (component.endswith("_key") or component.endswith("_key.pub"))
        )
        if (
            component in _DENIED_COMPONENTS
            or component in _DENIED_NAMES
            or env_file
            or credential_file
            or managed_secret
            or ssh_artifact
            or component.endswith(_DENIED_SUFFIXES)
        ):
            return True

    filename = PurePosixPath(parts[-1])
    tokens = frozenset(filter(None, re.split(r"[^a-z0-9]+", filename.stem)))
    auth_tokens = bool(tokens & _SENSITIVE_AUTH_TOKENS) or {"service", "account"} <= tokens
    return auth_tokens and filename.suffix in _AUTH_CONFIG_SUFFIXES


def _entries(project: Path) -> tuple[_Entry, ...]:
    output = _git(project, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    entries: list[_Entry] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RepositoryAccessError("committed repository tree is malformed")
        try:
            path = raw_path.decode("utf-8")
            mode = fields[0].decode("ascii")
            object_id = fields[2].decode("ascii")
        except UnicodeDecodeError as exc:
            raise RepositoryAccessError("committed repository path is not UTF-8") from exc
        entries.append(_Entry(mode, object_id, path))
    return tuple(entries)


def list_repository(project: Path, path: str = ".") -> list[str]:
    prefix = _path(path, root=True)
    if prefix and _denied(prefix):
        raise RepositoryAccessError("repository path is prohibited")
    entries = _entries(project)
    requested_entry = next((entry for entry in entries if entry.path == prefix), None)
    if requested_entry is not None and requested_entry.mode == "120000":
        raise RepositoryAccessError("repository symlinks cannot be listed")
    prefix_with_slash = f"{prefix}/" if prefix else ""
    matches = [
        entry.path
        for entry in entries
        if entry.mode != "120000"
        and not _denied(entry.path)
        and (not prefix or entry.path == prefix or entry.path.startswith(prefix_with_slash))
    ]
    if len(matches) > _MAX_LIST_ENTRIES:
        raise RepositoryAccessError("repository listing exceeds 2000 entries; choose a narrower path")
    return matches


def read_repository(project: Path, path: str) -> str:
    normalized = _path(path)
    if _denied(normalized):
        raise RepositoryAccessError("repository path is prohibited")
    entry = next((item for item in _entries(project) if item.path == normalized), None)
    if entry is None:
        raise RepositoryAccessError("repository path is not a committed file")
    if entry.mode == "120000":
        raise RepositoryAccessError("repository symlinks cannot be read")
    if entry.mode not in {"100644", "100755"}:
        raise RepositoryAccessError("repository path is not a regular file")
    content = _git(project, "cat-file", "blob", entry.object_id)
    if len(content) > _MAX_FILE_BYTES:
        raise RepositoryAccessError("repository file exceeds 1 MiB")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepositoryAccessError("repository file is not UTF-8 text") from exc
