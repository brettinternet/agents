from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from agents.config import AgentsConfig, CaoConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import (
    MigrationError,
    MutationConflict,
    ProjectIdentityError,
    connect,
    initialize_project,
    migrate,
    mutation,
    utc_now,
)
from agents.store import Store


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-b", "main", str(self.root / "repo")], check=True, capture_output=True)
        self.connection = connect(self.root / "agents.db")
        migrate(self.connection)
        self.config = AgentsConfig(
            self.root / "agents.toml",
            self.root,
            ProjectConfig("test", self.root / "repo", "main", (("task", "check"),)),
            RuntimeConfig(5, 1800, 12, 4, 3, 86400),
            CaoConfig("2.4.1", "mock", "mock_cli", 9889, (ModelChoice(""),)),
            WebConfig("127.0.0.1", 9890),
            (
                {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
                {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
                {"slug": "elder", "kind": "agent", "reports_to": "human", "persistent": True, "capacity": 1},
                {
                    "slug": "explorer",
                    "kind": "agent",
                    "reports_to": "elder",
                    "specialty": "research",
                    "persistent": True,
                    "capacity": 3,
                },
                {
                    "slug": "yapper",
                    "kind": "agent",
                    "reports_to": "elder",
                    "specialty": "publishing",
                    "persistent": True,
                    "capacity": 1,
                },
            ),
        )
        Store(self.connection).initialize(self.config)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_production_migration_creates_core_tables_and_pragmas(self) -> None:
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "project",
            "actors",
            "terminal_runs",
            "work_items",
            "acceptance_criteria",
            "dependencies",
            "consultations",
            "decisions",
            "executions",
            "assignments",
            "submissions",
            "checks",
            "reviews",
            "blockers",
            "approvals",
            "conversations",
            "messages",
            "deliveries",
            "events",
            "mutation_requests",
        }
        self.assertTrue(required <= tables)
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT version FROM schema_migrations ORDER BY version")],
            [1, 2],
        )
        terminal_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(terminal_runs)")}
        self.assertIn("reasoning_effort", terminal_columns)

    def test_project_identity_is_immutable(self) -> None:
        first = initialize_project(self.connection, self.config)
        self.assertRegex(first["instance_id"], r"^[0-9a-f]{8}$")
        changed = AgentsConfig(
            self.config.source,
            self.config.root,
            ProjectConfig("other", self.root / "repo", "main", self.config.project.verify),
            self.config.runtime,
            self.config.cao,
            self.config.web,
            self.config.actors,
        )
        with self.assertRaises(ProjectIdentityError):
            initialize_project(self.connection, changed)

    def test_checksum_and_unknown_versions_fail(self) -> None:
        migrations = self.root / "migrations"
        migrations.mkdir()
        source = Path(__file__).parents[1] / "src/agents/migrations/001_initial.sql"
        copied = migrations / source.name
        shutil.copy(source, copied)
        other = connect(self.root / "other.db")
        migrate(other, migrations)
        copied.write_text(copied.read_text() + "\n-- drift\n")
        with self.assertRaises(MigrationError):
            migrate(other, migrations)
        other.execute("DELETE FROM schema_migrations")
        other.execute("INSERT INTO schema_migrations VALUES (999,?,?)", (hashlib.sha256(b"x").hexdigest(), utc_now()))
        with self.assertRaises(MigrationError):
            migrate(other, migrations)
        other.close()

    def test_migration_failure_rolls_back_schema_and_metadata(self) -> None:
        migrations = self.root / "bad"
        migrations.mkdir()
        (migrations / "001_good.sql").write_text("CREATE TABLE good(id INTEGER PRIMARY KEY);\n")
        (migrations / "002_bad.sql").write_text(
            "CREATE TABLE should_rollback(id INTEGER);\nINSERT INTO missing VALUES (1);\n"
        )
        other = connect(self.root / "rollback.db")
        with self.assertRaises(sqlite3.OperationalError):
            migrate(other, migrations)
        self.assertIsNone(other.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone())
        self.assertEqual([row[0] for row in other.execute("SELECT version FROM schema_migrations")], [1])
        other.close()

    def test_mutation_replays_once_and_rejects_key_reuse(self) -> None:
        calls = 0

        def apply(connection):
            nonlocal calls
            calls += 1
            connection.execute("UPDATE project SET next_work_seq=next_work_seq+1 WHERE id=1")
            return {"seq": 1}

        first = mutation(self.connection, "human", "request-1", "work.created", "work:1", "hash", apply)
        second = mutation(self.connection, "human", "request-1", "work.created", "work:1", "hash", apply)
        self.assertEqual(first, second)
        self.assertEqual(calls, 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        with self.assertRaises(MutationConflict):
            mutation(self.connection, "human", "request-1", "work.created", "work:1", "different", apply)

    def test_failed_mutation_rolls_back_domain_event_and_request(self) -> None:
        before = self.connection.execute("SELECT next_work_seq FROM project").fetchone()[0]

        def fail(connection):
            connection.execute("UPDATE project SET next_work_seq=next_work_seq+1 WHERE id=1")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            mutation(self.connection, "human", "request-fail", "work.failed", "work:1", "hash", fail)
        self.assertEqual(self.connection.execute("SELECT next_work_seq FROM project").fetchone()[0], before)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM events WHERE kind='work.failed'").fetchone()[0], 0
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM mutation_requests WHERE request_id='request-fail'"
            ).fetchone()[0],
            0,
        )

    def test_mutation_routes_system_entries_to_shared_channels(self) -> None:
        expected = {
            "decision.proposed": {"#findings", "#coordination"},
            "work.created": {"#findings"},
            "check.completed": {"#publishing"},
            "review.submitted": {"#publishing"},
            "work.failed": {"#publishing", "#incidents"},
        }
        for index, (kind, addresses) in enumerate(expected.items()):
            mutation(
                self.connection,
                "human",
                f"routing-{index}",
                kind,
                "work:AGENT-0001",
                kind,
                lambda _connection: {"id": "AGENT-0001"},
            )
            actual = {
                row[0]
                for row in self.connection.execute(
                    "SELECT c.address FROM messages m "
                    "JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE m.body LIKE ?",
                    (f"{kind}:%",),
                )
            }
            self.assertEqual(actual, addresses)


if __name__ == "__main__":
    unittest.main()
