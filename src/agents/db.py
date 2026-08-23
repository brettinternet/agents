from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import AgentsConfig
from .git_worktree import identity as git_identity

_REQUEST_ID = re.compile(r"[\x20-\x7e]{1,128}\Z")


class MigrationError(RuntimeError):
    pass


class ProjectIdentityError(RuntimeError):
    pass


class MutationConflict(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration contains an incomplete SQL statement")
    return statements


def migrate(connection: sqlite3.Connection, migrations_dir: Path | None = None) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    directory = migrations_dir or Path(__file__).with_name("migrations")
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    versions = [int(path.name[:3]) for path in files]
    if versions != list(range(1, len(files) + 1)):
        raise MigrationError("migration files must be contiguous from 001")
    applied = {
        int(row["version"]): str(row["sha256"])
        for row in connection.execute("SELECT version, sha256 FROM schema_migrations")
    }
    if any(version not in versions for version in applied):
        raise MigrationError("database contains an unknown/newer migration")
    for version, path in zip(versions, files, strict=True):
        sql = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if version in applied:
            if applied[version] != digest:
                raise MigrationError(f"migration checksum drift: {path.name}")
            continue
        foreign_keys_off = sql.startswith("-- migrate: foreign-keys-off")
        try:
            if foreign_keys_off:
                connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _sql_statements(sql):
                connection.execute(statement)
            if foreign_keys_off:
                violations = list(connection.execute("PRAGMA foreign_key_check"))
                if violations:
                    raise MigrationError(f"migration violates foreign keys: {path.name}")
            connection.execute(
                "INSERT INTO schema_migrations(version,sha256,applied_at) VALUES (?,?,?)",
                (version, digest, utc_now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if foreign_keys_off:
                connection.execute("PRAGMA foreign_keys=ON")


def initialize_project(connection: sqlite3.Connection, config: AgentsConfig) -> sqlite3.Row:
    canonical_path, common_dir = git_identity(config.project.path)
    verify_json = canonical_json([list(argv) for argv in config.project.verify])
    current = connection.execute("SELECT * FROM project WHERE id=1").fetchone()
    if current is None:
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO project(id,instance_id,name,canonical_path,git_common_dir,default_branch,verify_json,"
                "next_work_seq,created_at,updated_at) VALUES (1,?,?,?,?,?,?,1,?,?)",
                (
                    secrets.token_hex(4),
                    config.project.name,
                    str(canonical_path),
                    str(common_dir),
                    config.project.default_branch,
                    verify_json,
                    now,
                    now,
                ),
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise
        current = connection.execute("SELECT * FROM project WHERE id=1").fetchone()
        assert current is not None
        return current
    expected = (
        config.project.name,
        str(canonical_path),
        str(common_dir),
        config.project.default_branch,
        verify_json,
    )
    actual = tuple(
        current[key] for key in ("name", "canonical_path", "git_common_dir", "default_branch", "verify_json")
    )
    if actual != expected:
        raise ProjectIdentityError("configured project identity differs from initialized Agents state")
    return current


def _event_actor(identity: str) -> str:
    if identity == "human":
        return "human"
    if identity.startswith("agent:"):
        return identity.removeprefix("agent:")
    return "system"


def _system_entries(
    connection: sqlite3.Connection,
    kind: str,
    entity: str,
    response: object,
    now: str,
) -> None:
    if kind.startswith(("message.", "inbox.")):
        return
    work_id = ""
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        work_id = response["id"]
    elif entity.startswith("work:"):
        work_id = entity.removeprefix("work:")
    addresses: list[str] = []
    if work_id and work_id != "new":
        addresses.append(f"work:{work_id}")
    if kind.startswith("decision."):
        addresses.extend(("#findings", "#coordination"))
    elif kind.startswith(("work.created", "work.refinement", "work.refined", "work.ready")):
        addresses.append("#findings")
    elif kind.startswith(("work.", "check.", "review.", "blocker.", "integration.")):
        addresses.append("#publishing")
    if kind.endswith((".failed", ".error")):
        addresses.append("#incidents")
    body = f"{kind}: {canonical_json(response)}"
    for address in dict.fromkeys(addresses):
        conversation = connection.execute("SELECT id FROM conversations WHERE address=?", (address,)).fetchone()
        if conversation is None:
            continue
        message_id = connection.execute(
            "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at) VALUES (?,?,?,'normal',?)",
            (conversation["id"], "system", body, now),
        ).lastrowid
        recipients = connection.execute(
            "SELECT actor_slug FROM conversation_members WHERE conversation_id=? AND notify=1 AND actor_slug<>'system'",
            (conversation["id"],),
        )
        connection.executemany(
            "INSERT INTO deliveries(message_id,actor_slug,state,attempts,next_attempt_at) VALUES (?,?,'pending',0,?)",
            ((message_id, row["actor_slug"], now) for row in recipients),
        )


def mutation[T](
    connection: sqlite3.Connection,
    identity: str,
    request_id: str,
    kind: str,
    entity: str,
    body_hash: str,
    fn: Callable[[sqlite3.Connection], T],
) -> T:
    if not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id must be 1-128 printable ASCII characters")
    connection.execute("BEGIN IMMEDIATE")
    try:
        prior = connection.execute(
            "SELECT kind,entity,body_hash,response_json FROM mutation_requests WHERE identity=? AND request_id=?",
            (identity, request_id),
        ).fetchone()
        if prior is not None:
            if (prior["kind"], prior["entity"], prior["body_hash"]) != (kind, entity, body_hash):
                raise MutationConflict("idempotency key was reused for a different request")
            response = json.loads(str(prior["response_json"]))
            connection.commit()
            return response  # type: ignore[return-value]
        response = fn(connection)
        now = utc_now()
        response_json = canonical_json(response)
        connection.execute(
            "INSERT INTO events(actor_slug,kind,entity_kind,entity_id,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
            (_event_actor(identity), kind, entity.split(":", 1)[0], entity, response_json, now),
        )
        _system_entries(connection, kind, entity, response, now)
        connection.execute(
            "INSERT INTO mutation_requests(identity,request_id,kind,entity,body_hash,response_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (identity, request_id, kind, entity, body_hash, response_json, now),
        )
        connection.commit()
        return response
    except BaseException:
        connection.rollback()
        raise
