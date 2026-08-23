from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import httpx
from websockets.sync.client import connect as websocket_connect

FORMAT_KEY = "AGENTS_SECRET_STORE_VERSION"
FORMAT_VERSION = "1"
NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
CONFIG_NAME = ".sops.yaml"
STORE_NAME = "agent-secrets.sops.json"
KEY_NAME = ".env.sops-age"
HOME_NAME = ".sops-isolated-home"
LOCK_NAME = "agents-secret-store.lock"


class SecretStoreError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    worktree: Path
    common_root: Path
    config: Path
    store: Path
    key: Path
    isolated_home: Path
    lock: Path


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    failure: str,
) -> bytes:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise SecretStoreError(f"{failure}: {exc.strerror or exc.__class__.__name__}") from None
    if result.returncode != 0:
        raise SecretStoreError(failure)
    return result.stdout


def _git(args: list[str], *, cwd: Path) -> Path:
    output = _run(["git", *args], cwd=cwd, failure="unable to resolve repository paths")
    try:
        return Path(output.decode("utf-8").strip()).resolve(strict=True)
    except UnicodeDecodeError, OSError, ValueError:
        raise SecretStoreError("git returned an invalid repository path") from None


def resolve_paths(cwd: Path | None = None) -> Paths:
    current = (cwd or Path.cwd()).resolve(strict=True)
    broker_root = os.environ.get("AGENTS_BROKER_SECRETS_ROOT")
    if broker_root:
        private = Path(broker_root).resolve(strict=True)
        return Paths(
            worktree=current,
            common_root=private,
            config=private / "sops-config",
            store=private / "store",
            key=private / "age-key",
            isolated_home=private / "sops-home",
            lock=private / "lock",
        )
    worktree = _git(["rev-parse", "--show-toplevel"], cwd=current)
    common_dir = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=current)
    common_root = common_dir.parent
    return Paths(
        worktree=worktree,
        common_root=common_root,
        config=worktree / CONFIG_NAME,
        store=worktree / STORE_NAME,
        key=common_root / KEY_NAME,
        isolated_home=common_root / HOME_NAME,
        lock=common_dir / LOCK_NAME,
    )


def _kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _require_file(path: Path, label: str, *, private: bool = False) -> None:
    kind = _kind(path)
    if kind != "file":
        raise SecretStoreError(f"{label} is {kind}, expected a regular file: {path}")
    if private and path.stat().st_mode & 0o077:
        raise SecretStoreError(f"unsafe permissions on {label}; require mode 0600 or stricter: {path}")


def _ensure_home(path: Path, *, create: bool) -> None:
    kind = _kind(path)
    if kind == "missing" and create:
        old = os.umask(0o077)
        try:
            path.mkdir(mode=0o700)
        finally:
            os.umask(old)
        kind = _kind(path)
    if kind != "directory":
        raise SecretStoreError(f"isolated SOPS home is {kind}, expected a directory: {path}")
    if path.stat().st_mode & 0o077:
        raise SecretStoreError(f"unsafe permissions on isolated SOPS home; require mode 0700: {path}")


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Generator[None]:
    kind = _kind(path)
    if kind not in {"missing", "file"}:
        raise SecretStoreError(f"secret-store lock path is unsafe ({kind}): {path}")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    old = os.umask(0o077)
    try:
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError:
            raise SecretStoreError(f"unable to open secret-store lock safely: {path}") from None
    finally:
        os.umask(old)
    try:
        status = os.fstat(descriptor)
        try:
            path_status = path.lstat()
        except OSError:
            raise SecretStoreError(f"secret-store lock path changed while opening: {path}") from None
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.getuid()
            or status.st_mode & 0o077
            or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
        ):
            raise SecretStoreError(f"secret-store lock path is unsafe: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _sops_env(paths: Paths) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("SOPS_")}
    env.update(
        {
            "SOPS_AGE_KEY_FILE": str(paths.key),
            "SOPS_DECRYPTION_ORDER": "age",
            "HOME": str(paths.isolated_home),
            "XDG_CONFIG_HOME": str(paths.isolated_home),
        }
    )
    return env


def _command_env(values: dict[str, str]) -> dict[str, str]:
    env = {
        name: value for name, value in os.environ.items() if not name.startswith("SOPS_") and name != "__VARLOCK_ENV"
    }
    env.update(values)
    return env


def _config_text(recipient: str) -> str:
    return (
        "stores:\n"
        "  json:\n"
        "    indent: 2\n"
        "creation_rules:\n"
        "  - path_regex: ^agent-secrets\\.sops\\.json$\n"
        f"    age: {recipient}\n"
    )


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    if _kind(path) != "missing":
        raise SecretStoreError(f"refusing to replace existing path: {path}")
    old = os.umask(0o077)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        os.umask(old)
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def _derive_recipient(paths: Paths) -> str:
    _require_file(paths.key, "age identity", private=True)
    output = _run(
        ["age-keygen", "-y", str(paths.key)],
        cwd=paths.worktree,
        failure="unable to derive the age recipient",
    )
    try:
        recipient = output.decode("ascii").strip()
    except UnicodeDecodeError:
        raise SecretStoreError("age-keygen returned an invalid recipient") from None
    if not re.fullmatch(r"age1[0-9a-z]+", recipient):
        raise SecretStoreError("age-keygen returned an invalid recipient")
    return recipient


def _create_identity(paths: Paths) -> None:
    if _kind(paths.key) != "missing":
        raise SecretStoreError(f"refusing to replace existing age identity: {paths.key}")
    old = os.umask(0o077)
    try:
        _run(
            ["age-keygen", "-o", str(paths.key)],
            cwd=paths.worktree,
            failure="unable to create the age identity",
        )
    finally:
        os.umask(old)
    _require_file(paths.key, "age identity", private=True)
    paths.key.chmod(0o600)


def _parse_json(data: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise SecretStoreError(f"malformed {label}") from None
    if not isinstance(value, dict):
        raise SecretStoreError(f"malformed {label}")
    return value


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def _validate_ciphertext(paths: Paths, recipient: str) -> dict[str, object]:
    _require_file(paths.config, "SOPS config")
    _require_file(paths.store, "encrypted secret store")
    try:
        config = paths.config.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        raise SecretStoreError("unable to read SOPS config") from None
    if config != _config_text(recipient):
        raise SecretStoreError("SOPS config does not match the repository secret-store template")
    try:
        raw_ciphertext = paths.store.read_bytes()
        ciphertext = _parse_json(raw_ciphertext, "encrypted secret store")
    except OSError:
        raise SecretStoreError("unable to read encrypted secret store") from None
    if raw_ciphertext != _json_bytes(ciphertext):
        raise SecretStoreError("encrypted secret store must use two-space JSON formatting")
    metadata = ciphertext.get("sops")
    if not isinstance(metadata, dict):
        raise SecretStoreError("encrypted secret store lacks SOPS metadata")
    age_entries = metadata.get("age")
    if not isinstance(age_entries, list) or len(age_entries) != 1:
        raise SecretStoreError("encrypted secret store must contain exactly one age recipient")
    age_entry = age_entries[0]
    if not isinstance(age_entry, dict) or age_entry.get("recipient") != recipient:
        raise SecretStoreError("encrypted secret store recipient does not match the local age identity")
    for name, value in ciphertext.items():
        if name == "sops":
            continue
        if (
            not isinstance(name, str)
            or not NAME_RE.fullmatch(name)
            or not isinstance(value, str)
            or not value.startswith("ENC[")
        ):
            raise SecretStoreError("encrypted secret store contains an invalid top-level entry")
    if FORMAT_KEY not in ciphertext:
        raise SecretStoreError("encrypted secret store lacks its format marker")
    return ciphertext


def _decrypt(paths: Paths) -> dict[str, str]:
    recipient = _derive_recipient(paths)
    _validate_ciphertext(paths, recipient)
    output = _run(
        ["sops", "decrypt", "--input-type", "json", "--output-type", "json", str(paths.store)],
        cwd=paths.worktree,
        env=_sops_env(paths),
        failure="unable to decrypt the agent secret store",
    )
    plain = _parse_json(output, "decrypted secret store")
    if plain.get(FORMAT_KEY) != FORMAT_VERSION:
        raise SecretStoreError("unsupported agent secret store version")
    values: dict[str, str] = {}
    for name, value in plain.items():
        if name == FORMAT_KEY:
            continue
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not isinstance(value, str):
            raise SecretStoreError("decrypted secret store contains an invalid entry")
        if "\0" in value:
            raise SecretStoreError("decrypted secret store contains a NUL byte")
        values[name] = value
    return values


def _validate_schema(paths: Paths, values: dict[str, str]) -> None:
    env = _command_env(values)
    output = _run(
        [
            "varlock",
            "load",
            "-p",
            ".env.schema",
            "-p",
            ".env.local",
            "--format",
            "json-full",
            "--agent",
            "--show-all",
        ],
        cwd=paths.worktree,
        env=env,
        failure="Varlock rejected the managed secret metadata",
    )
    document = _parse_json(output, "Varlock metadata")
    config = document.get("config")
    if not isinstance(config, dict):
        raise SecretStoreError("malformed Varlock metadata")
    for name in values:
        entry = config.get(name)
        if not isinstance(entry, dict) or entry.get("isSensitive") is not True:
            raise SecretStoreError(f"managed key is not declared sensitive in .env.schema: {name}")


def _existing_state(paths: Paths) -> tuple[str, str, str]:
    return _kind(paths.key), _kind(paths.config), _kind(paths.store)


def init_store(paths: Paths) -> None:
    with _exclusive_lock(paths.lock):
        _ensure_home(paths.isolated_home, create=True)
        key_kind, config_kind, store_kind = _existing_state(paths)
        if any(kind not in {"missing", "file"} for kind in (key_kind, config_kind, store_kind)):
            raise SecretStoreError("secret-store paths must be regular files, not symlinks or special files")
        tracked = (config_kind == "file", store_kind == "file")
        if tracked[0] != tracked[1]:
            raise SecretStoreError(
                "inconsistent secret store: .sops.yaml and agent-secrets.sops.json must exist together"
            )
        if tracked[0] and key_kind == "missing":
            raise SecretStoreError("missing .env.sops-age; restore the identity matching the committed ciphertext")
        if key_kind == "missing":
            _create_identity(paths)
        recipient = _derive_recipient(paths)
        if tracked[0]:
            _validate_ciphertext(paths, recipient)
            _decrypt(paths)
            return
        config = _config_text(recipient).encode("utf-8")
        plaintext = _json_bytes({FORMAT_KEY: FORMAT_VERSION})
        encrypted = _run(
            [
                "sops",
                "encrypt",
                "--age",
                recipient,
                "--input-type",
                "json",
                "--output-type",
                "json",
                "/dev/stdin",
            ],
            cwd=paths.worktree,
            env=_sops_env(paths),
            input_bytes=plaintext,
            failure="unable to initialize the encrypted secret store",
        )
        encrypted = _json_bytes(_parse_json(encrypted, "new encrypted secret store"))
        _atomic_write(paths.config, config)
        try:
            _atomic_write(paths.store, encrypted)
        except Exception:
            with contextlib.suppress(OSError):
                paths.config.unlink()
            raise
        _validate_ciphertext(paths, recipient)
        _decrypt(paths)


def _ready_paths() -> Paths:
    paths = resolve_paths()
    _ensure_home(paths.isolated_home, create=False)
    _require_file(paths.key, "age identity", private=True)
    return paths


def check_store(paths: Paths) -> None:
    values = _decrypt(paths)
    _validate_schema(paths, values)


def _validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise SecretStoreError("secret name must match [A-Z_][A-Z0-9_]*")
    if name == FORMAT_KEY:
        raise SecretStoreError("the secret-store format marker cannot be addressed")


def _read_value() -> str:
    if sys.stdin.isatty():
        value = getpass.getpass("Secret value: ")
    else:
        try:
            value = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            raise SecretStoreError("secret value must be valid UTF-8") from None
    if "\0" in value:
        raise SecretStoreError("secret value cannot contain a NUL byte")
    return value


def set_secret(paths: Paths, name: str) -> None:
    _validate_name(name)
    with _exclusive_lock(paths.lock):
        values = _decrypt(paths)
        value = _read_value()
        proposed = {**values, name: value}
        _validate_schema(paths, proposed)
        encoded = json.dumps(value).encode("utf-8")
        _run(
            ["sops", "set", "--value-stdin", str(paths.store), json.dumps([name])],
            cwd=paths.worktree,
            env=_sops_env(paths),
            input_bytes=encoded,
            failure="unable to update the encrypted secret store",
        )
        _validate_schema(paths, _decrypt(paths))


def unset_secret(paths: Paths, name: str) -> None:
    _validate_name(name)
    with _exclusive_lock(paths.lock):
        values = _decrypt(paths)
        if name not in values:
            raise SecretStoreError(f"managed key does not exist: {name}")
        remaining = dict(values)
        del remaining[name]
        _validate_schema(paths, remaining)
        _run(
            ["sops", "unset", str(paths.store), json.dumps([name])],
            cwd=paths.worktree,
            env=_sops_env(paths),
            failure="unable to update the encrypted secret store",
        )
        _validate_schema(paths, _decrypt(paths))


def reveal_secret(paths: Paths, name: str) -> None:
    _validate_name(name)
    values = _decrypt(paths)
    if name not in values:
        raise SecretStoreError(f"managed key does not exist: {name}")
    _validate_schema(paths, values)
    sys.stdout.write(values[name])
    sys.stdout.flush()


def list_secrets(paths: Paths) -> None:
    values = _decrypt(paths)
    for name in sorted(values):
        print(name)


def broker_values(paths: Paths, names: list[str] | None = None) -> dict[str, str]:
    values = _decrypt(paths)
    _validate_schema(paths, values)
    if names is None:
        return values
    for name in names:
        _validate_name(name)
    unknown = [name for name in names if name not in values]
    if unknown:
        raise SecretStoreError(f"unknown managed secret name: {unknown[0]}")
    return {name: values[name] for name in names}


def set_secret_value(paths: Paths, name: str, value: bytes) -> None:
    _validate_name(name)
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretStoreError("secret value must be valid UTF-8") from None
    if "\0" in decoded:
        raise SecretStoreError("secret value cannot contain a NUL byte")
    with _exclusive_lock(paths.lock):
        values = _decrypt(paths)
        proposed = {**values, name: decoded}
        _validate_schema(paths, proposed)
        _run(
            ["sops", "set", "--value-stdin", str(paths.store), json.dumps([name])],
            cwd=paths.worktree,
            env=_sops_env(paths),
            input_bytes=json.dumps(decoded).encode("utf-8"),
            failure="unable to update the encrypted secret store",
        )
        _validate_schema(paths, _decrypt(paths))


def _parse_run_args(arguments: list[str]) -> tuple[list[str], list[str]]:
    try:
        separator = arguments.index("--")
    except ValueError:
        raise SecretStoreError("run requires managed secret names followed by -- and a command") from None
    names = arguments[:separator]
    command = arguments[separator + 1 :]
    if not names:
        raise SecretStoreError("run requires at least one managed secret name before --")
    if not command:
        raise SecretStoreError("run requires a command after --")
    for name in names:
        _validate_name(name)
    if len(set(names)) != len(names):
        raise SecretStoreError("run does not allow duplicate managed secret names")
    return names, command


def run_command(paths: Paths, names: list[str], command: list[str]) -> NoReturn:
    values = _decrypt(paths)
    unknown = [name for name in names if name not in values]
    if unknown:
        raise SecretStoreError(f"unknown managed secret name: {unknown[0]}")
    _validate_schema(paths, values)
    argv = [
        "varlock",
        "run",
        "--redact-stdout",
        "--inject",
        "vars",
        "--filter",
        ",".join(names),
        "-p",
        ".env.schema",
        "-p",
        ".env.local",
        "--",
        *command,
    ]
    try:
        os.chdir(paths.worktree)
        os.execvpe("varlock", argv, _command_env(values))
    except OSError as exc:
        raise SecretStoreError(f"unable to run command through Varlock: {exc.strerror}") from None


def _agent_api_headers() -> dict[str, str]:
    token = os.environ.get("AGENTS_AGENT_TOKEN")
    execution_id = os.environ.get("AGENTS_EXECUTION_ID")
    if not token or not execution_id:
        raise SecretStoreError("agent API credentials are unavailable")
    return {"Authorization": f"Bearer {token}", "X-Agents-Execution-ID": execution_id}


def _agent_api_request(action: str, body: dict[str, str] | None = None) -> dict[str, object]:
    base = os.environ.get("AGENTS_SECRETS_API_URL") or os.environ.get("AGENTS_API_URL")
    if not base:
        raise SecretStoreError("AGENTS_API_URL is unavailable")
    try:
        response = httpx.post(
            f"{base.rstrip('/')}/agent/v1/secrets/{action}",
            headers=_agent_api_headers(),
            json=body or {},
            timeout=30,
        )
        response.raise_for_status()
        envelope = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SecretStoreError(f"agent secret broker request failed: {exc}") from None
    if not isinstance(envelope, dict) or envelope.get("ok") is not True or not isinstance(envelope.get("data"), dict):
        raise SecretStoreError("agent secret broker returned a malformed response")
    return envelope["data"]


def _agent_api_run(names: list[str], command: list[str]) -> int:
    base = os.environ.get("AGENTS_SECRETS_API_URL") or os.environ.get("AGENTS_API_URL")
    if not base:
        raise SecretStoreError("AGENTS_API_URL is unavailable")
    url = f"{base.rstrip('/')}/agent/v1/secrets/run".replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    try:
        with websocket_connect(url, additional_headers=_agent_api_headers()) as socket:
            socket.send(json.dumps({"names": names, "argv": command, "tty": sys.stdin.isatty()}))
            if not sys.stdin.isatty():
                data = sys.stdin.buffer.read()
                if data:
                    socket.send(data)
            socket.send(json.dumps({"stdin_eof": True}))
            for message in socket:
                if isinstance(message, bytes):
                    if message[:1] == b"\x01":
                        sys.stdout.buffer.write(message[1:])
                        sys.stdout.buffer.flush()
                    elif message[:1] == b"\x02":
                        sys.stderr.buffer.write(message[1:])
                        sys.stderr.buffer.flush()
                    continue
                frame = json.loads(message)
                if isinstance(frame, dict) and isinstance(frame.get("exit_code"), int):
                    return int(frame["exit_code"])
                if isinstance(frame, dict) and frame.get("error"):
                    raise SecretStoreError(str(frame["error"]))
    except OSError as exc:
        raise SecretStoreError(f"agent secret broker connection failed: {exc}") from None
    raise SecretStoreError("agent secret broker closed without an exit status")


def _agent_api_main(args: argparse.Namespace) -> int:
    if args.action == "check":
        _agent_api_request("check")
    elif args.action == "list":
        data = _agent_api_request("list")
        names = data.get("names")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise SecretStoreError("agent secret broker returned malformed names")
        for name in names:
            print(name)
    elif args.action == "reveal":
        data = _agent_api_request("reveal", {"name": args.name})
        encoded = data.get("value_base64")
        if not isinstance(encoded, str):
            raise SecretStoreError("agent secret broker returned a malformed value")
        sys.stdout.buffer.write(base64.b64decode(encoded, validate=True))
        sys.stdout.buffer.flush()
    elif args.action == "set":
        value = sys.stdin.buffer.read()
        _agent_api_request("set", {"name": args.name, "value_base64": base64.b64encode(value).decode()})
    elif args.action == "unset":
        _agent_api_request("unset", {"name": args.name})
    elif args.action == "run":
        names, command = _parse_run_args(args.arguments)
        return _agent_api_run(names, command)
    else:
        raise SecretStoreError("secret store initialization is unavailable through the agent API")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the repository agent secret store")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("check")
    subparsers.add_parser("list")
    for action in ("set", "unset", "reveal"):
        command = subparsers.add_parser(action)
        command.add_argument("name")
    run = subparsers.add_parser("run")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.environ.get("AGENTS_SECRETS_TRANSPORT") == "agent-api":
            return _agent_api_main(args)
        if args.action == "init":
            init_store(resolve_paths())
        else:
            paths = _ready_paths()
            if args.action == "check":
                check_store(paths)
            elif args.action == "list":
                list_secrets(paths)
            elif args.action == "set":
                set_secret(paths, args.name)
            elif args.action == "unset":
                unset_secret(paths, args.name)
            elif args.action == "reveal":
                reveal_secret(paths, args.name)
            elif args.action == "run":
                names, command = _parse_run_args(args.arguments)
                run_command(paths, names, command)
    except SecretStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
