from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents.auth import (
    AuthenticationError,
    authenticate_agent,
    constant_time_token,
    derive_agent_token,
    issue_human_session,
    read_private_secret,
    token_digest,
    verify_human_session,
)
from agents.db import migrate


class AuthTests(unittest.TestCase):
    def test_agent_token_is_deterministic_and_generation_fenced(self) -> None:
        key = b"k" * 32
        first = derive_agent_token(key, "deadbeef", 1, 1)
        self.assertEqual(first, derive_agent_token(key, "deadbeef", 1, 1))
        self.assertNotEqual(first, derive_agent_token(key, "deadbeef", 1, 2))
        self.assertEqual(len(token_digest(first)), 64)

    def test_agent_context_scopes_persistence_to_terminal_purpose(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            migrate(connection)
            key = b"k" * 32
            instance_id = "deadbeef"
            now = "2026-01-01T00:00:00Z"
            connection.execute(
                "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                ("persistent-agent", "agent", 1, 1, now, now),
            )

            def add_terminal(terminal_id: str, purpose_kind: str, purpose_id: str) -> tuple[int, str]:
                cursor = connection.execute(
                    "INSERT INTO terminal_runs("
                    "session_name,profile_name,mcp_name,profile_sha256,provider,model,"
                    "generation,actor_slug,purpose_kind,purpose_id,working_directory,token_digest,"
                    "profile_state,state,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"session-{terminal_id}",
                        "profile",
                        "mcp",
                        "sha",
                        "mock",
                        "model",
                        1,
                        "persistent-agent",
                        purpose_kind,
                        purpose_id,
                        ".",
                        "",
                        "installed",
                        "live",
                        now,
                        now,
                    ),
                )
                lastrowid = cursor.lastrowid
                assert lastrowid is not None
                run_id = int(lastrowid)
                token = derive_agent_token(key, instance_id, run_id, 1)
                connection.execute(
                    "UPDATE terminal_runs SET terminal_id=?,token_digest=? WHERE id=?",
                    (terminal_id, token_digest(token), run_id),
                )
                connection.execute(
                    "INSERT INTO actor_leases("
                    "actor_slug,purpose_kind,purpose_id,terminal_run_id,acquired_at"
                    ") VALUES (?,?,?,?,?)",
                    ("persistent-agent", purpose_kind, purpose_id, run_id, now),
                )
                return run_id, token

            persistent_run_id, persistent_token = add_terminal("persistent-terminal", "persistent", "persistent-agent")
            work_run_id, work_token = add_terminal("work-terminal", "work", "work-1")

            persistent_context = authenticate_agent(
                connection, "persistent-terminal", persistent_token, key, instance_id
            )
            work_context = authenticate_agent(connection, "work-terminal", work_token, key, instance_id)
            self.assertEqual(persistent_context.terminal_run_id, persistent_run_id)
            self.assertEqual(persistent_context.purpose_kind, "persistent")
            self.assertTrue(persistent_context.persistent)
            self.assertEqual(work_context.terminal_run_id, work_run_id)
            self.assertEqual(work_context.purpose_kind, "work")
            self.assertFalse(work_context.persistent)

            with self.assertRaises(AuthenticationError):
                authenticate_agent(connection, "work-terminal", persistent_token, key, instance_id)

            connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=?",
                (now, work_run_id),
            )
            with self.assertRaises(AuthenticationError):
                authenticate_agent(connection, "work-terminal", work_token, key, instance_id)

            connection.execute(
                "UPDATE terminal_runs SET token_revoked_at=? WHERE id=?",
                (now, persistent_run_id),
            )
            with self.assertRaises(AuthenticationError):
                authenticate_agent(connection, "persistent-terminal", persistent_token, key, instance_id)
        finally:
            connection.close()

    def test_signed_session_expires_and_carries_csrf(self) -> None:
        cookie, session = issue_human_session("w" * 64, now=100)
        self.assertEqual(verify_human_session(cookie, "w" * 64, now=101), session)
        with self.assertRaises(AuthenticationError):
            verify_human_session(cookie, "w" * 64, now=session.expires_at)
        with self.assertRaises(AuthenticationError):
            verify_human_session(cookie, "x" * 64, now=101)

    def test_private_secret_permissions_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text("x" * 64)
            path.chmod(0o600)
            self.assertEqual(read_private_secret(path), "x" * 64)
            path.chmod(0o644)
            with self.assertRaises(AuthenticationError):
                read_private_secret(path)

    def test_agent_auth_key_requires_exact_hex_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-auth-key"
            path.write_text("x" * 64)
            path.chmod(0o600)
            with self.assertRaises(AuthenticationError):
                read_private_secret(path)

    def test_constant_time_token_contract(self) -> None:
        self.assertTrue(constant_time_token("same", "same"))
        self.assertFalse(constant_time_token("same", "different"))
