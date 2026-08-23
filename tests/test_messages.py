from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import MutationConflict, connect, migrate, utc_now
from agents.messages import Messages, Messaging
from agents.policy import DomainError
from agents.store import Store
from agents.workflow import Workflow


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        repo = root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        self.connection = connect(root / "agents.db")
        migrate(self.connection)
        actors = (
            {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
            {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
            {
                "slug": "manager",
                "kind": "agent",
                "reports_to": "human",
                "specialty": "coordination",
                "persistent": True,
                "capacity": 1,
            },
            {
                "slug": "researcher",
                "kind": "agent",
                "reports_to": "manager",
                "specialty": "research",
                "persistent": True,
                "capacity": 1,
            },
            {
                "slug": "executor",
                "kind": "agent",
                "reports_to": "manager",
                "specialty": "implementation",
                "persistent": True,
                "capacity": 1,
            },
            {
                "slug": "writer",
                "kind": "agent",
                "reports_to": "manager",
                "specialty": "publishing",
                "persistent": True,
                "capacity": 1,
            },
        )
        config = AgentsConfig(
            root / "agents.toml",
            root,
            ProjectConfig("test", repo, "main", (("task", "check"),)),
            RuntimeConfig(
                poll_seconds=5,
                stall_seconds=1800,
                launch_budget_per_hour=12,
                max_agents=4,
                max_consultations=3,
                worker_grace_seconds=86400,
            ),
            ExecutionConfig("herdr", "0.8.2", None, "mock", "mock_cli", (ModelChoice(""),)),
            WebConfig("127.0.0.1", 9890),
            actors,
        )
        Store(self.connection).initialize(config)
        self.messaging = Messaging(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def _terminal_run(self, purpose_kind: str, purpose_id: str, execution_name: str, actor: str = "researcher") -> int:
        now = utc_now()
        run_id = self.connection.execute(
            "INSERT INTO terminal_runs("
            "execution_name,profile_name,mcp_name,profile_sha256,provider,model,generation,"
            "actor_slug,purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                execution_name,
                "profile",
                "mcp",
                "sha",
                "mock",
                "model",
                1,
                actor,
                purpose_kind,
                purpose_id,
                ".",
                "digest",
                "installed",
                "live",
                now,
                now,
            ),
        ).lastrowid
        assert run_id is not None
        return int(run_id)

    def _ensure_runner(self) -> None:
        now = utc_now()
        self.connection.execute(
            "INSERT INTO actors(slug,kind,reports_to,specialty,persistent,capacity,created_at,updated_at)"
            "VALUES('runner','agent','manager','research',0,1,?,?)",
            (now, now),
        )

    def test_seeded_members_and_notification_policy(self):
        expected = {
            "#general": (
                {"human", "system", "manager", "researcher", "executor", "writer"},
                {"manager", "researcher", "executor", "writer"},
            ),
            "#findings": (
                {"human", "system", "manager", "researcher"},
                {"manager", "researcher"},
            ),
            "#publishing": (
                {"human", "system", "manager", "writer"},
                {"manager", "writer"},
            ),
            "#coordination": ({"human", "system", "manager"}, {"manager"}),
            "#incidents": ({"human", "system", "manager"}, {"manager"}),
        }
        for address, (members, notified) in expected.items():
            conversation = self.connection.execute(
                "SELECT id FROM conversations WHERE address=?", (address,)
            ).fetchone()
            self.assertIsNotNone(conversation)
            rows = list(
                self.connection.execute(
                    "SELECT actor_slug,notify FROM conversation_members WHERE conversation_id=?",
                    (conversation["id"],),
                )
            )
            self.assertEqual({row[0] for row in rows}, members)
            self.assertEqual({row[0] for row in rows if row[1]}, notified)
        hierarchy = {
            row[0]: row[1]
            for row in self.connection.execute("SELECT slug,reports_to FROM actors WHERE kind='agent' ORDER BY slug")
        }
        self.assertEqual(
            hierarchy,
            {"executor": "manager", "manager": "human", "researcher": "manager", "writer": "manager"},
        )

    def test_post_is_atomic_replayable_and_delivered(self):
        first = self.messaging.post("post-1", "human", "#coordination", "Please refine")
        replay = self.messaging.post("post-1", "human", "#coordination", "Please refine")
        self.assertEqual(first, replay)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM messages WHERE sender_slug='human'").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT actor_slug FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.sender_slug='human'"
            ).fetchone()[0],
            "manager",
        )
        with self.assertRaises(MutationConflict):
            self.messaging.post("post-1", "human", "#coordination", "Changed")
        publishing = self.messaging.post("post-publishing", "human", "#publishing", "Please publish")
        self.assertEqual(publishing["deliveries"], 2)
        self.assertEqual(
            {
                row[0]
                for row in self.connection.execute(
                    "SELECT actor_slug FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.id=?",
                    (publishing["id"],),
                )
            },
            {"manager", "writer"},
        )

    def test_general_broadcasts_to_agents_and_ack_is_actor_scoped(self):
        posted = self.messaging.post("mention-1", "human", "#general", "@researcher inspect")
        self.assertEqual(posted["deliveries"], 4)
        terminal_run_id = self._terminal_run("persistent", "researcher", "researcher-persistent")
        inbox = Messages(self.connection).inbox("researcher", terminal_run_id, True)
        self.assertEqual(inbox[0]["body"], "@researcher inspect")
        acked = self.messaging.ack("ack-1", "researcher", terminal_run_id, True, [inbox[0]["id"]])
        self.assertEqual(acked["acknowledged"], [inbox[0]["id"]])
        direct = self.messaging.post("direct-actor", "human", "@researcher", "private")
        with self.assertRaises(DomainError):
            self.messaging.ack("ack-2", "manager", terminal_run_id, True, [direct["id"]])

    def test_delivery_visibility_is_terminal_scoped(self):
        persistent_run_id = self._terminal_run("persistent", "researcher", "researcher-persistent")
        work_run_id = self._terminal_run("work", "AGENT-0001", "researcher-work")
        targeted = self.messaging.post("targeted-1", "human", "@researcher", "work assignment")
        self.connection.execute(
            "UPDATE deliveries SET terminal_run_id=? WHERE message_id=? AND actor_slug='researcher'",
            (work_run_id, targeted["id"]),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT terminal_run_id FROM deliveries WHERE message_id=? AND actor_slug='researcher'",
                (targeted["id"],),
            ).fetchone()[0],
            work_run_id,
        )
        generic = self.messaging.post("generic-1", "human", "@researcher", "persistent notice")

        persistent_inbox = Messages(self.connection).inbox("researcher", persistent_run_id, True)
        self.assertEqual([row["body"] for row in persistent_inbox], ["persistent notice"])
        with self.assertRaises(DomainError):
            self.messaging.ack("targeted-persistent", "researcher", persistent_run_id, True, [targeted["id"]])

        work_inbox = Messages(self.connection).inbox("researcher", work_run_id, False)
        self.assertEqual([row["body"] for row in work_inbox], ["work assignment"])
        self.assertEqual(
            self.messaging.ack("targeted-work", "researcher", work_run_id, False, [targeted["id"]])["acknowledged"],
            [targeted["id"]],
        )
        self.assertEqual(Messages(self.connection).inbox("researcher", work_run_id, False), [])
        with self.assertRaises(DomainError):
            self.messaging.ack("generic-work", "researcher", work_run_id, False, [generic["id"]])
        self.assertEqual(
            self.messaging.ack("generic-persistent", "researcher", persistent_run_id, True, [generic["id"]])[
                "acknowledged"
            ],
            [generic["id"]],
        )

    def test_work_post_targets_open_assignment_terminal(self):
        created = Workflow(self.connection).create_work(
            "work-post-create", "human", parent_id=None, kind="story", title="Assigned", problem="P", outcome="O"
        )
        work_id = created["id"]
        now = utc_now()
        execution_id = self.connection.execute(
            "INSERT INTO executions("
            "work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (work_id, 1, "base", "work-branch", "worktree", "active", now, now),
        ).lastrowid
        self.assertIsNotNone(execution_id)
        work_run_id = self._terminal_run("work", work_id, "researcher-work-assigned")
        self.connection.execute(
            "INSERT INTO assignments("
            "work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (work_id, execution_id, "researcher", work_run_id, "open", now, now),
        )
        persistent_run_id = self._terminal_run("persistent", "researcher", "researcher-persistent-assigned")

        posted = self.messaging.post("work-post-1", "human", f"work:{work_id}", "assigned work")
        self.assertEqual(posted["deliveries"], 4)
        deliveries = {
            row["actor_slug"]: row["terminal_run_id"]
            for row in self.connection.execute(
                "SELECT actor_slug,terminal_run_id FROM deliveries WHERE message_id=?",
                (posted["id"],),
            )
        }
        self.assertEqual(deliveries["researcher"], work_run_id)
        self.assertIsNone(deliveries["manager"])
        self.assertIsNone(deliveries["writer"])
        self.assertIsNone(deliveries["executor"])

        persistent_inbox = Messages(self.connection).inbox("researcher", persistent_run_id, True)
        self.assertNotIn(posted["id"], {row["id"] for row in persistent_inbox})
        work_inbox = Messages(self.connection).inbox("researcher", work_run_id, False)
        self.assertEqual([row["body"] for row in work_inbox], ["assigned work"])

    def test_dm_availability_and_human_escalation(self):
        with self.assertRaises(DomainError):
            self.messaging.post("dm-1", "human", "@unknown", "hello")
        dm = self.messaging.post("dm-2", "human", "@manager", "hello")
        self.assertEqual(dm["address"], "dm:human:manager")
        self.assertIsNone(
            self.connection.execute(
                "SELECT terminal_run_id FROM deliveries WHERE message_id=?", (dm["id"],)
            ).fetchone()[0]
        )
        escalation = self.messaging.post("escalate-1", "manager", "!human", "Need a decision")
        self.assertEqual(escalation["deliveries"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM incidents WHERE kind='human_escalation'").fetchone()[0], 1
        )

    def test_nonpersistent_dm_binds_delivery_and_scopes_inbox_to_terminal(self):
        self._ensure_runner()
        terminal_run_id = self._terminal_run("work", "AGENT-DM", "runner-dm", actor="runner")
        self.connection.execute(
            "UPDATE terminal_runs SET backend_terminal_id=?,agent_auth_id=? WHERE id=?",
            ("runner-terminal", "runner-auth", terminal_run_id),
        )

        posted = self.messaging.post("dm-runner", "human", "@runner", "hello runner")
        delivery = self.connection.execute(
            "SELECT actor_slug,terminal_run_id FROM deliveries WHERE message_id=?", (posted["id"],)
        ).fetchone()
        self.assertEqual((delivery["actor_slug"], delivery["terminal_run_id"]), ("runner", terminal_run_id))

        inbox = Messages(self.connection).inbox("runner", terminal_run_id, False)
        self.assertEqual([row["body"] for row in inbox], ["hello runner"])
        self.assertEqual(Messages(self.connection).inbox("runner", terminal_run_id + 1000, False), [])
        with self.assertRaises(DomainError):
            self.messaging.ack("dm-runner-wrong-terminal", "runner", terminal_run_id + 1000, False, [posted["id"]])
        self.assertEqual(
            self.messaging.ack("dm-runner-ack", "runner", terminal_run_id, False, [posted["id"]])["acknowledged"],
            [posted["id"]],
        )
        self.assertEqual(Messages(self.connection).inbox("runner", terminal_run_id, False), [])

    def test_nonpersistent_dm_rejects_ambiguous_live_terminals(self):
        self._ensure_runner()
        self._terminal_run("work", "AGENT-DM-1", "runner-dm-1", actor="runner")
        self._terminal_run("review", "AGENT-DM-2", "runner-dm-2", actor="runner")

        with self.assertRaises(DomainError):
            self.messaging.post("dm-runner-ambiguous", "human", "@runner", "choose a terminal")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM messages WHERE sender_slug='human' AND body='choose a terminal'"
            ).fetchone()
        )

    def test_work_conversation_and_system_entry_commit_with_intake(self):
        created = Workflow(self.connection).create_work(
            "create-1", "human", parent_id=None, kind="story", title="Story", problem="P", outcome="O"
        )
        address = f"work:{created['id']}"
        self.assertIsNotNone(
            self.connection.execute("SELECT 1 FROM conversations WHERE address=?", (address,)).fetchone()
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.address=? AND m.sender_slug='system'",
                (address,),
            ).fetchone()
        )
