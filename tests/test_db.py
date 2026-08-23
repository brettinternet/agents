from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
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
            ExecutionConfig("herdr", "0.8.2", None, "mock", "mock_cli", (ModelChoice(""),)),
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
            "schedule_states",
            "schedule_runs",
        }
        self.assertTrue(required <= tables)
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT version FROM schema_migrations ORDER BY version")],
            [1, 2, 3, 4, 5, 6, 7, 8],
        )
        terminal_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(terminal_runs)")}
        self.assertTrue(
            {
                "reasoning_effort",
                "execution_name",
                "execution_backend",
                "backend_run_id",
                "backend_terminal_id",
                "agent_auth_id",
                "backend_revision",
            }
            <= terminal_columns
        )

    def test_project_identity_is_immutable(self) -> None:
        first = initialize_project(self.connection, self.config)
        self.assertRegex(first["instance_id"], r"^[0-9a-f]{8}$")
        changed = AgentsConfig(
            self.config.source,
            self.config.root,
            ProjectConfig("other", self.root / "repo", "main", self.config.project.verify),
            self.config.runtime,
            self.config.execution,
            self.config.web,
            self.config.actors,
        )
        with self.assertRaises(ProjectIdentityError):
            initialize_project(self.connection, changed)

    def test_migrations_upgrade_existing_database_and_preserve_general_history(self) -> None:
        migrations = self.root / "upgrade-migrations"
        migrations.mkdir()
        source = Path(__file__).parents[1] / "src/agents/migrations"
        for name in ("001_initial.sql", "002_terminal_reasoning.sql", "003_schedules.sql"):
            shutil.copy(source / name, migrations / name)
        other = connect(self.root / "upgrade.db")
        migrate(other, migrations)
        now = utc_now()
        conversation_id = other.execute(
            "INSERT INTO conversations(address,kind,created_at,updated_at) VALUES('#all-hands','channel',?,?)",
            (now, now),
        ).lastrowid
        self.assertIsNotNone(conversation_id)
        other.execute(
            "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at) VALUES('human','human',1,1,?,?)",
            (now, now),
        )
        other.execute(
            "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at) VALUES(?, 'human', 'history', 'normal', ?)",
            (conversation_id, now),
        )
        shutil.copy(source / "004_general_channel.sql", migrations / "004_general_channel.sql")
        migrate(other, migrations)
        upgraded = other.execute(
            "SELECT c.address,m.body FROM conversations c JOIN messages m ON m.conversation_id=c.id"
        ).fetchone()
        self.assertEqual(tuple(upgraded), ("#general", "history"))
        self.assertEqual(
            [row[0] for row in other.execute("SELECT version FROM schema_migrations ORDER BY version")],
            [1, 2, 3, 4],
        )
        other.close()

    def test_execution_backend_migration_backfills_historical_identity(self) -> None:
        migrations = self.root / "execution-upgrade-migrations"
        migrations.mkdir()
        source = Path(__file__).parents[1] / "src/agents/migrations"
        for name in ("001_initial.sql", "002_terminal_reasoning.sql", "003_schedules.sql"):
            shutil.copy(source / name, migrations / name)
        other = connect(self.root / "execution-upgrade.db")
        migrate(other, migrations)
        now = utc_now()
        other.execute(
            "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("worker", "agent", 1, 1, now, now),
        )
        other.execute(
            "INSERT INTO terminal_runs("
            "session_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,"
            "purpose_kind,purpose_id,working_directory,token_digest,terminal_id,profile_state,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cao-agents-deadbeef-p-worker-g0001",
                "agents-deadbeef-r0000000001-g0001",
                "mcp",
                "sha",
                "mock_cli",
                "",
                1,
                "worker",
                "persistent",
                "worker",
                "/repo",
                "digest",
                "cao-terminal",
                "installed",
                "live",
                now,
                now,
            ),
        )
        other.execute(
            "INSERT INTO terminal_runs("
            "session_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,"
            "purpose_kind,purpose_id,working_directory,token_digest,terminal_id,profile_state,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cao-agents-deadbeef-p-worker-g0002",
                "reserved",
                "reserved",
                "",
                "mock_cli",
                "",
                2,
                "worker",
                "persistent",
                "worker-reserved",
                "/repo",
                "digest-2",
                None,
                "reserved",
                "reserved",
                now,
                now,
            ),
        )
        for name in ("004_general_channel.sql", "005_execution_backend.sql"):
            shutil.copy(source / name, migrations / name)
        migrate(other, migrations)
        row = other.execute("SELECT * FROM terminal_runs WHERE state='live'").fetchone()
        self.assertEqual(row["execution_backend"], "cao")
        self.assertEqual(row["backend_run_id"], "cao-agents-deadbeef-p-worker-g0001")
        self.assertEqual(row["backend_terminal_id"], "cao-terminal")
        self.assertEqual(row["agent_auth_id"], "cao-terminal")
        reserved = other.execute("SELECT * FROM terminal_runs WHERE state='reserved'").fetchone()
        self.assertIsNone(reserved["backend_run_id"])
        self.assertIsNone(reserved["backend_terminal_id"])
        self.assertIsNone(reserved["agent_auth_id"])
        other.close()

    def test_cutover_failure_migrations_resolve_persistent_state(self) -> None:
        migrations = self.root / "stale-cao-migrations"
        migrations.mkdir()
        source = Path(__file__).parents[1] / "src/agents/migrations"
        for name in (
            "001_initial.sql",
            "002_terminal_reasoning.sql",
            "003_schedules.sql",
            "004_general_channel.sql",
            "005_execution_backend.sql",
        ):
            shutil.copy(source / name, migrations / name)
        other = connect(self.root / "stale-cao.db")
        migrate(other, migrations)
        now = utc_now()
        other.execute(
            "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("worker", "agent", 1, 1, now, now),
        )
        other.execute(
            "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("other", "agent", 1, 1, now, now),
        )
        other.execute(
            "INSERT INTO actors(slug,kind,persistent,capacity,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("late", "agent", 1, 1, now, now),
        )
        run_id = other.execute(
            "INSERT INTO terminal_runs("
            "execution_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,"
            "purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cao-agents-deadbeef-p-worker-g0001",
                "profile",
                "mcp",
                "sha",
                "mock_cli",
                "",
                1,
                "worker",
                "persistent",
                "worker",
                "/new",
                "digest",
                "installed",
                "failed",
                "CAO terminal working directory mismatch: expected /new, got /old",
                now,
                now,
            ),
        ).lastrowid
        herdr_run_id = other.execute(
            "INSERT INTO terminal_runs("
            "execution_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,"
            "purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "agents-deadbeef-p-other-g0002",
                "profile-2",
                "mcp-2",
                "sha-2",
                "opencode_cli",
                "",
                2,
                "other",
                "persistent",
                "other",
                "/new",
                "digest-2",
                "installed",
                "failed",
                "agent_pane_busy: agent target pane w1:p1 is not an available shell",
                now,
                now,
            ),
        ).lastrowid
        self.assertIsNotNone(herdr_run_id)
        self.assertIsNotNone(run_id)
        other.execute(
            "INSERT INTO blockers(target_kind,target_id,terminal_run_id,kind,reason,requested_role,"
            "actor_slug,state,created_at,updated_at) VALUES('persistent','worker',?,'terminal_failure',"
            "'wrong directory','human','worker','open',?,?)",
            (run_id, now, now),
        )
        other.execute(
            "INSERT INTO blockers(target_kind,target_id,terminal_run_id,kind,reason,requested_role,"
            "actor_slug,state,created_at,updated_at) VALUES('persistent','other',?,'terminal_failure',"
            "'pane busy','human','other','open',?,?)",
            (herdr_run_id, now, now),
        )
        other.execute(
            "INSERT INTO blockers(target_kind,target_id,terminal_run_id,kind,reason,requested_role,"
            "actor_slug,state,created_at,updated_at) VALUES('work','unrelated',?,'terminal_failure',"
            "'pane busy','human','other','open',?,?)",
            (herdr_run_id, now, now),
        )
        other.execute(
            "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,created_at,updated_at)"
            " VALUES('terminal_failed','terminal',?,'error','open','wrong directory','{}',?,?)",
            (str(run_id), now, now),
        )
        other.execute(
            "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,created_at,updated_at)"
            " VALUES('terminal_failed','terminal',?,'error','open','pane busy','{}',?,?)",
            (str(herdr_run_id), now, now),
        )
        other.execute(
            "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,created_at,updated_at)"
            " VALUES('terminal_failed','delivery','unrelated','error','open','pane busy','{}',?,?)",
            (now, now),
        )
        shutil.copy(source / "006_resolve_stale_cao_failures.sql", migrations / "006_resolve_stale_cao_failures.sql")
        shutil.copy(
            source / "007_resolve_transient_herdr_launches.sql",
            migrations / "007_resolve_transient_herdr_launches.sql",
        )
        migrate(other, migrations)
        late_run_id = other.execute(
            "INSERT INTO terminal_runs("
            "execution_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,"
            "purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "agents-deadbeef-p-late-g0001",
                "profile-late",
                "mcp-late",
                "sha-late",
                "opencode_cli",
                "",
                1,
                "late",
                "persistent",
                "late",
                "/new",
                "digest-late",
                "installed",
                "ended",
                "backend run identity, occupant, or cwd mismatch",
                now,
                now,
            ),
        ).lastrowid
        self.assertIsNotNone(late_run_id)
        other.execute(
            "INSERT INTO blockers(target_kind,target_id,terminal_run_id,kind,reason,requested_role,"
            "actor_slug,state,created_at,updated_at) VALUES('persistent','late',?,'terminal_failure',"
            "'identity lag','human','late','open',?,?)",
            (late_run_id, now, now),
        )
        other.execute(
            "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,created_at,updated_at)"
            " VALUES('terminal_failed','terminal',?,'error','open','identity lag','{}',?,?)",
            (str(late_run_id), now, now),
        )
        shutil.copy(
            source / "008_resolve_agent_start_propagation.sql",
            migrations / "008_resolve_agent_start_propagation.sql",
        )
        migrate(other, migrations)
        self.assertEqual(
            dict(other.execute("SELECT state,COUNT(*) FROM blockers GROUP BY state")),
            {"open": 1, "resolved": 3},
        )
        self.assertEqual(
            dict(other.execute("SELECT state,COUNT(*) FROM incidents GROUP BY state")),
            {"open": 1, "resolved": 3},
        )
        self.assertEqual(
            other.execute("SELECT error FROM terminal_runs WHERE id=?", (herdr_run_id,)).fetchone()[0],
            "transient Herdr cutover: agent_pane_busy: agent target pane w1:p1 is not an available shell",
        )
        self.assertEqual(
            other.execute("SELECT error FROM terminal_runs WHERE id=?", (late_run_id,)).fetchone()[0],
            "transient Herdr cutover: backend run identity, occupant, or cwd mismatch",
        )
        self.assertEqual(
            [row[0] for row in other.execute("SELECT version FROM schema_migrations ORDER BY version")],
            [1, 2, 3, 4, 5, 6, 7, 8],
        )
        other.close()

    def test_checksum_and_unknown_versions_fail(self) -> None:
        migrations = self.root / "migrations"
        migrations.mkdir()
        source = Path(__file__).parents[1] / "src/agents/migrations"
        for name in (
            "001_initial.sql",
            "002_terminal_reasoning.sql",
            "003_schedules.sql",
            "004_general_channel.sql",
            "005_execution_backend.sql",
            "006_resolve_stale_cao_failures.sql",
            "007_resolve_transient_herdr_launches.sql",
            "008_resolve_agent_start_propagation.sql",
        ):
            shutil.copy(source / name, migrations / name)
        other = connect(self.root / "other.db")
        migrate(other, migrations)
        copied = migrations / "003_schedules.sql"
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
