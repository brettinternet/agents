from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_SECONDS = 12 * 60 * 60


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentContext:
    terminal_run_id: int
    actor_slug: str
    purpose_kind: str
    purpose_id: str
    persistent: bool


@dataclass(frozen=True)
class HumanSession:
    expires_at: int
    csrf: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def read_private_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise AuthenticationError(f"unsafe secret file: {path}")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (UnicodeDecodeError, OSError) as exc:
        raise AuthenticationError(f"malformed secret file: {path}") from exc
    if len(value) < 32:
        raise AuthenticationError(f"malformed secret file: {path}")
    if path.name == "agent-auth-key" and (len(value) != 64 or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None):
        raise AuthenticationError(f"malformed agent-auth key: {path}")
    return value


def read_agent_auth_key(path: Path) -> bytes:
    try:
        return bytes.fromhex(read_private_secret(path))
    except (AuthenticationError, ValueError) as exc:
        raise AuthenticationError(f"malformed agent-auth key: {path}") from exc


def derive_agent_token(key: bytes, instance_id: str, terminal_run_id: int, generation: int) -> str:
    return _b64(
        hmac.new(key, f"{instance_id}:terminal-run:{terminal_run_id}:{generation}".encode(), hashlib.sha256).digest()
    )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate_agent(
    connection: sqlite3.Connection, agent_auth_id: str, bearer: str, key: bytes, instance_id: str
) -> AgentContext:
    row = connection.execute(
        "SELECT tr.* FROM terminal_runs tr WHERE tr.agent_auth_id=? AND tr.state IN ('creating','live','retained')",
        (agent_auth_id,),
    ).fetchone()
    if row is None or row["token_revoked_at"] is not None:
        raise AuthenticationError("unknown or revoked terminal")
    expected = derive_agent_token(key, instance_id, int(row["id"]), int(row["generation"]))
    if not hmac.compare_digest(expected, bearer) or not hmac.compare_digest(
        token_digest(expected), str(row["token_digest"])
    ):
        raise AuthenticationError("invalid agent token")
    lease = connection.execute(
        "SELECT 1 FROM actor_leases WHERE actor_slug=? AND terminal_run_id=? AND purpose_kind=? AND purpose_id=? AND released_at IS NULL",
        (row["actor_slug"], row["id"], row["purpose_kind"], row["purpose_id"]),
    ).fetchone()
    if lease is None:
        raise AuthenticationError("terminal has no current actor lease")
    return AgentContext(
        int(row["id"]),
        str(row["actor_slug"]),
        str(row["purpose_kind"]),
        str(row["purpose_id"]),
        str(row["purpose_kind"]) == "persistent",
    )


def constant_time_token(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def _session_key(web_token: str) -> bytes:
    return hmac.new(web_token.encode(), b"agents:web-session:v1", hashlib.sha256).digest()


def issue_human_session(web_token: str, now: int | None = None) -> tuple[str, HumanSession]:
    issued = int(time.time()) if now is None else now
    session = HumanSession(issued + SESSION_SECONDS, secrets.token_urlsafe(24))
    payload = _b64(
        json.dumps({"exp": session.expires_at, "csrf": session.csrf}, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _b64(hmac.new(_session_key(web_token), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}", session


def verify_human_session(cookie: str, web_token: str, now: int | None = None) -> HumanSession:
    try:
        payload, signature = cookie.split(".", 1)
        expected = _b64(hmac.new(_session_key(web_token), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("invalid session signature")
        decoded: Any = json.loads(_unb64(payload))
        expires = int(decoded["exp"])
        csrf = str(decoded["csrf"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("invalid session") from exc
    current = int(time.time()) if now is None else now
    if expires <= current or not csrf:
        raise AuthenticationError("expired session")
    return HumanSession(expires, csrf)
