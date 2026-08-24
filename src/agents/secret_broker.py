from __future__ import annotations

import ctypes
import os
import sqlite3
from pathlib import Path

import uvicorn

from .config import load
from .web import create_secret_broker_app


def main() -> None:
    if os.environ.get("AGENTS_TOPOLOGY") != "compose" or os.environ.get("AGENTS_SYSTEM_CONTAINER") != "1":
        raise RuntimeError("secret broker requires the marked whole-system Compose topology")
    config = load()
    db_path = Path(os.environ.get("AGENTS_BROKER_DB_PATH", config.db_path))
    auth_source = Path(os.environ.get("AGENTS_BROKER_AUTH_KEY_PATH", config.state_dir / "agent-auth-key"))
    secret_root = Path(os.environ.get("AGENTS_BROKER_SECRETS_ROOT", ""))
    state_root = Path(os.environ.get("AGENTS_BROKER_STATE_ROOT", ""))
    private_roots = (secret_root, state_root)
    for private_root in private_roots:
        if (
            not private_root.is_absolute()
            or private_root.is_symlink()
            or not private_root.is_dir()
            or private_root.stat().st_uid != 0
        ):
            raise RuntimeError("unsafe broker private mount root")
        private_root.chmod(0o700)
    if auth_source.is_symlink() or not auth_source.is_file() or auth_source.stat().st_mode & 0o077:
        raise RuntimeError("unsafe broker authentication key")
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_target = config.state_dir / "agent-auth-key"
    descriptor = os.open(auth_target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, auth_source.read_bytes())
    finally:
        os.close(descriptor)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:
        raise RuntimeError("unable to make the secret broker non-dumpable")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    host = os.environ.get("AGENTS_BROKER_HOST", "172.30.1.3")
    port = int(os.environ.get("AGENTS_BROKER_PORT", "9891"))
    try:
        uvicorn.run(create_secret_broker_app(config, connection), host=host, port=port, workers=1, access_log=False)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
