from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import connect, migrate, utc_now
from agents.delivery import Delivery
from agents.execution import RunHandle
from agents.git_worktree import branch_sha, head_sha
from agents.policy import DomainError
from agents.reconciler import Reconciler, reserve_terminal
from agents.store import Store
from agents.workflow import Workflow


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "file").write_text("base")
        subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
        state = self.root / ".agents"
        state.mkdir(mode=0o700)
        (state / "agent-auth-key").write_text("6b" * 32)
        (state / "agent-auth-key").chmod(0o600)
        self.connection = connect(state / "agents.db")
        migrate(self.connection)
        actors = (
            {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
            {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
            {
                "slug": "elder",
                "kind": "agent",
                "reports_to": "human",
                "profile_template": "elder",
                "persistent": True,
                "capacity": 1,
            },
            {
                "slug": "explorer",
                "kind": "agent",
                "reports_to": "elder",
                "profile_template": "explorer",
                "specialty": "research",
                "persistent": True,
                "capacity": 3,
            },
            {
                "slug": "writer",
                "kind": "agent",
                "reports_to": "elder",
                "profile_template": "writer",
                "specialty": "publishing",
                "persistent": True,
                "capacity": 3,
            },
        )
        self.config = AgentsConfig(
            self.root / "agents.toml",
            self.root,
            ProjectConfig("test", repo, "main", (("python3", "-c", "print('ok')"),)),
            RuntimeConfig(5, 1800, 12, 4, 3, 86400),
            ExecutionConfig("herdr", "0.8.2", None, "mock", "mock_cli", (ModelChoice(""),)),
            WebConfig("127.0.0.1", 9890),
            actors,
        )
        Store(self.connection).initialize(self.config)
        self.workflow = Workflow(self.connection)
        self.delivery = Delivery(self.config, self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def ready_item(self, gates: list[str] | None = None):
        created = self.workflow.create_work(
            "create", "human", parent_id=None, kind="story", title="Story", problem="P", outcome="O"
        )
        started = self.workflow.start_refinement("start", "elder", created["id"], created["version"])
        refined = self.workflow.refine(
            "refine",
            "elder",
            created["id"],
            started["version"],
            kind="story",
            title="Story",
            problem="P",
            outcome="O",
            priority="normal",
            specialty="publishing",
            criteria=["works"],
            dependencies=[],
            gates=gates or [],
        )
        consult = self.delivery.request_consultation("elder", created["id"], refined["version"], "publishing", "review")
        self.connection.execute(
            "UPDATE consultations SET state='completed',responder='writer',response='good',updated_at=? WHERE id=?",
            (utc_now(), consult["id"]),
        )
        ready = self.workflow.mark_ready("ready", "elder", created["id"], refined["version"])
        return created["id"], ready["version"]

    def test_consultation_assignment_delivery_targets_reserved_terminal(self):
        created = self.workflow.create_work(
            "consult-create", "human", parent_id=None, kind="story", title="Consult", problem="P", outcome="O"
        )
        started = self.workflow.start_refinement("consult-start", "elder", created["id"], created["version"])
        refined = self.workflow.refine(
            "consult-refine",
            "elder",
            created["id"],
            started["version"],
            kind="story",
            title="Consult",
            problem="P",
            outcome="O",
            priority="normal",
            specialty="publishing",
            criteria=["works"],
            dependencies=[],
            gates=[],
        )
        consultation = self.delivery.request_consultation(
            "elder", created["id"], refined["version"], "publishing", "review"
        )
        persistent = reserve_terminal(
            self.connection,
            self.config,
            actor="writer",
            purpose_kind="persistent",
            purpose_id="writer",
            working_directory=self.config.project.path,
            budget_exempt=True,
        )
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            (str(persistent["execution_name"]), "writer-persistent", now, persistent["id"]),
        )

        assigned = self.delivery.dispatch_consultation_next()
        self.assertIsNotNone(assigned)
        assigned = cast(dict[str, Any], assigned)
        self.assertEqual(assigned["terminal_run_id"], persistent["id"])
        delivery = self.connection.execute(
            "SELECT d.actor_slug,d.terminal_run_id,d.state FROM deliveries d "
            "JOIN messages m ON m.id=d.message_id WHERE m.body=?",
            (f"Consultation {consultation['id']} assigned to @writer.",),
        ).fetchone()
        self.assertIsNotNone(delivery)
        self.assertEqual(
            (delivery["actor_slug"], delivery["terminal_run_id"], delivery["state"]),
            ("writer", persistent["id"], "pending"),
        )

    def queue_check(self, command: list[str]) -> int:
        item, version = self.ready_item()
        dispatch = cast(dict[str, Any], self.delivery.dispatch_next())
        worktree = Path(dispatch["worktree"])
        (worktree / "file").write_text("changed")
        subprocess.run(["git", "-C", str(worktree), "commit", "-am", "implement"], check=True, capture_output=True)
        submission = self.delivery.submit_work(
            "writer", item, version + 1, head_sha(worktree), "done", dispatch["terminal_run_id"]
        )
        self.connection.execute(
            "UPDATE checks SET command=? WHERE submission_id=?",
            (json.dumps(command), submission["submission_id"]),
        )
        return int(submission["submission_id"])

    async def test_check_drains_both_streams_into_bounded_tails(self):
        submission_id = self.queue_check(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('o' * 70000); sys.stderr.write('e' * 70000)",
            ]
        )
        result = await self.delivery.run_next_check()
        self.assertEqual(result, {"id": 1, "state": "passed"})
        check = self.connection.execute("SELECT * FROM checks WHERE submission_id=?", (submission_id,)).fetchone()
        self.assertIsNotNone(check)
        self.assertEqual(len(check["stdout_tail"].encode()), 65536)
        self.assertEqual(len(check["stderr_tail"].encode()), 65536)
        self.assertTrue(check["stdout_tail"].endswith("o" * 1024))
        self.assertTrue(check["stderr_tail"].endswith("e" * 1024))
        self.assertEqual(check["stdout_truncated"], 1)
        self.assertEqual(check["stderr_truncated"], 1)

    async def test_timeout_records_evidence_when_process_exits_during_signal(self):
        submission_id = self.queue_check(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stderr.write('before'); sys.stderr.flush(); time.sleep(0.05)",
            ]
        )
        with (
            patch("agents.delivery.CHECK_TIMEOUT_SECONDS", 0.001),
            patch("agents.delivery.os.killpg", side_effect=ProcessLookupError),
        ):
            result = await self.delivery.run_next_check()
        self.assertEqual(result, {"id": 1, "state": "failed"})
        check = self.connection.execute("SELECT * FROM checks WHERE submission_id=?", (submission_id,)).fetchone()
        self.assertIsNotNone(check)
        self.assertEqual(check["state"], "failed")
        self.assertIn("verification timed out", check["stderr_tail"])
        self.assertIsNotNone(check["exit_code"])

    def test_recover_running_check_records_interrupted_evidence_before_requeue(self):
        submission_id = self.queue_check(["python3", "-c", "print('unused')"])
        now = utc_now()
        self.connection.execute(
            "UPDATE checks SET state='running',pid=?,process_started_at=?,stdout_tail=?,stderr_tail=? "
            "WHERE submission_id=?",
            (999999, now, "prior stdout", "prior stderr", submission_id),
        )
        with patch("agents.delivery.os.killpg", side_effect=ProcessLookupError):
            recovered = self.delivery.recover_running_checks()
        self.assertEqual(recovered, 1)
        check = self.connection.execute("SELECT * FROM checks WHERE submission_id=?", (submission_id,)).fetchone()
        self.assertIsNotNone(check)
        self.assertEqual(check["state"], "queued")
        self.assertIsNone(check["pid"])
        self.assertIsNone(check["exit_code"])
        self.assertEqual(check["stdout_tail"], "prior stdout")
        self.assertIn("prior stderr", check["stderr_tail"])
        self.assertIn("verification interrupted after Agents restart", check["stderr_tail"])
        self.assertNotIn(check["state"], {"passed", "failed"})

    def test_expired_cleanup_removes_review_worktree_but_keeps_evidence(self):
        submission_id = self.queue_check(["python3", "-c", "print('unused')"])
        submission = self.connection.execute(
            "SELECT s.commit_sha,e.id execution_id,e.worktree_path,w.id work_id "
            "FROM submissions s JOIN executions e ON e.id=s.execution_id "
            "JOIN work_items w ON w.id=e.work_id WHERE s.id=?",
            (submission_id,),
        ).fetchone()
        self.assertIsNotNone(submission)
        reviewtree = self.root / ".worktrees" / "review" / "expired-research"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.config.project.path),
                "worktree",
                "add",
                "--detach",
                str(reviewtree),
                str(submission["commit_sha"]),
            ],
            check=True,
            capture_output=True,
        )
        old = "2000-01-01T00:00:00.000Z"
        self.connection.execute(
            "UPDATE work_items SET status='delivered',updated_at=? WHERE id=?", (old, submission["work_id"])
        )
        self.connection.execute("UPDATE executions SET updated_at=? WHERE id=?", (old, submission["execution_id"]))
        self.connection.execute("UPDATE submissions SET state='accepted',updated_at=? WHERE id=?", (old, submission_id))
        self.connection.execute(
            "INSERT INTO reviews(submission_id,gate,actor_slug,worktree_path,verdict,created_at,updated_at) "
            "VALUES(?,'research','explorer',?,'pass',?,?)",
            (submission_id, str(reviewtree), old, old),
        )
        Reconciler(self.config, self.connection)._cleanup_expired()
        self.assertFalse(reviewtree.exists())
        self.assertIsNotNone(
            self.connection.execute("SELECT 1 FROM submissions WHERE id=?", (submission_id,)).fetchone()
        )

    async def test_delivery_path(self):
        item, version = self.ready_item()
        dispatch = self.delivery.dispatch_next()
        self.assertIsNotNone(dispatch)
        dispatch = cast(dict[str, Any], dispatch)
        assignment_delivery = self.connection.execute(
            "SELECT d.actor_slug,d.terminal_run_id,d.state FROM deliveries d "
            "JOIN messages m ON m.id=d.message_id WHERE m.body=?",
            ("Implementation assigned to @writer.",),
        ).fetchone()
        self.assertIsNotNone(assignment_delivery)
        self.assertEqual(
            (assignment_delivery["actor_slug"], assignment_delivery["terminal_run_id"], assignment_delivery["state"]),
            ("writer", dispatch["terminal_run_id"], "pending"),
        )
        worktree = Path(dispatch["worktree"])
        (worktree / "file").write_text("changed")
        subprocess.run(["git", "-C", str(worktree), "commit", "-am", "implement"], check=True, capture_output=True)
        submission = self.delivery.submit_work(
            "writer", item, version + 1, head_sha(worktree), "done", dispatch["terminal_run_id"]
        )
        check = await self.delivery.run_next_check()
        self.assertIsNotNone(check)
        self.assertEqual(cast(dict[str, Any], check)["state"], "passed")
        self.assertEqual(self.delivery.advance_submission(submission["submission_id"])["state"], "reviewing")
        research_review = self.connection.execute(
            "SELECT * FROM reviews WHERE submission_id=? AND gate='research'", (submission["submission_id"],)
        ).fetchone()
        self.assertIsNotNone(research_review)
        review_delivery = self.connection.execute(
            "SELECT d.actor_slug,d.terminal_run_id,d.state FROM deliveries d "
            "JOIN messages m ON m.id=d.message_id WHERE m.body=?",
            ("RESEARCH review assigned to @explorer.",),
        ).fetchone()
        self.assertIsNotNone(review_delivery)
        self.assertEqual(
            (review_delivery["actor_slug"], review_delivery["terminal_run_id"], review_delivery["state"]),
            ("explorer", research_review["terminal_run_id"], "pending"),
        )
        reviewed = self.delivery.submit_review(
            "explorer",
            item,
            submission["submission_id"],
            submission["version"],
            "research",
            "pass",
            "ok",
            cast(Any, research_review)["terminal_run_id"],
        )
        accepted = self.delivery.decide_approval(item, reviewed["version"], True)
        self.assertEqual(accepted["state"], "accepted")
        subprocess.run(
            ["git", "-C", str(self.config.project.path), "merge", "--ff-only", dispatch["branch"]],
            check=True,
            capture_output=True,
        )
        self.assertTrue(self.delivery.queue_integration(item))
        integration = await self.delivery.run_next_check()
        self.assertIsNotNone(integration)
        self.assertEqual(cast(dict[str, Any], integration)["state"], "passed")
        self.assertEqual(
            self.delivery.finish_integration(item)["integration_sha"], branch_sha(self.config.project.path, "main")
        )

    async def test_required_publishing_review_retries_after_writer_capacity_frees(self):
        item, version = self.ready_item(gates=["publishing"])
        persistent = reserve_terminal(
            self.connection,
            self.config,
            actor="writer",
            purpose_kind="persistent",
            purpose_id="writer",
            working_directory=self.config.project.path,
            budget_exempt=True,
        )
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            (str(persistent["execution_name"]), "writer-persistent", now, persistent["id"]),
        )
        dispatch = self.delivery.dispatch_next()
        self.assertIsNotNone(dispatch)
        dispatch = cast(dict[str, Any], dispatch)
        worktree = Path(dispatch["worktree"])
        (worktree / "file").write_text("changed")
        subprocess.run(["git", "-C", str(worktree), "commit", "-am", "implement"], check=True, capture_output=True)
        submission = self.delivery.submit_work(
            "writer", item, version + 1, head_sha(worktree), "done", dispatch["terminal_run_id"]
        )
        check = await self.delivery.run_next_check()
        self.assertIsNotNone(check)
        self.assertEqual(cast(dict[str, Any], check)["state"], "passed")

        held = reserve_terminal(
            self.connection,
            self.config,
            actor="writer",
            purpose_kind="review",
            purpose_id=f"{item}-capacity-hold",
            working_directory=self.root / "capacity-hold",
            budget_exempt=True,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM actor_leases WHERE actor_slug='writer' AND released_at IS NULL"
            ).fetchone()[0],
            3,
        )
        self.assertEqual(self.delivery.advance_submission(submission["submission_id"])["state"], "reviewing")
        research_review = self.connection.execute(
            "SELECT * FROM reviews WHERE submission_id=? AND gate='research'", (submission["submission_id"],)
        ).fetchone()
        self.assertIsNotNone(research_review)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM reviews WHERE submission_id=? AND gate='publishing'", (submission["submission_id"],)
            ).fetchone()
        )

        reviewed = self.delivery.submit_review(
            "explorer",
            item,
            submission["submission_id"],
            submission["version"],
            "research",
            "pass",
            "research is good",
            cast(Any, research_review)["terminal_run_id"],
        )
        self.assertEqual(reviewed["state"], "reviewing")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM approvals WHERE submission_id=?", (submission["submission_id"],)
            ).fetchone()
        )

        self.connection.execute(
            "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
            (utc_now(), held["id"]),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM actor_leases WHERE actor_slug='writer' AND released_at IS NULL"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(self.delivery.advance_submission(submission["submission_id"])["state"], "reviewing")
        publishing_review = self.connection.execute(
            "SELECT * FROM reviews WHERE submission_id=? AND gate='publishing'", (submission["submission_id"],)
        ).fetchone()
        self.assertIsNotNone(publishing_review)
        self.assertEqual(cast(Any, publishing_review)["actor_slug"], "writer")
        self.assertEqual(cast(Any, publishing_review)["verdict"], "pending")

        approved = self.delivery.submit_review(
            "writer",
            item,
            submission["submission_id"],
            submission["version"],
            "publishing",
            "pass",
            "publishing is good",
            cast(Any, publishing_review)["terminal_run_id"],
        )
        self.assertEqual(approved["state"], "awaiting_approval")
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM approvals WHERE submission_id=?", (submission["submission_id"],)
            ).fetchone()
        )

    def test_submission_fence_and_decisions(self):
        item, version = self.ready_item()
        dispatch = self.delivery.dispatch_next()
        self.assertIsNotNone(dispatch)
        dispatch = cast(dict[str, Any], dispatch)
        subprocess.run(
            ["git", "-C", str(self.config.project.path), "commit", "--allow-empty", "-m", "advance"],
            check=True,
            capture_output=True,
        )
        with self.assertRaises(DomainError):
            self.delivery.submit_work(
                "writer", item, version + 1, dispatch["base_sha"], "bad", dispatch["terminal_run_id"]
            )
        decision = self.delivery.propose_decision(
            "elder", item_id=item, title="Choose", question="Which?", options=["A", "B"], recommendation="A"
        )
        self.assertEqual(decision["state"], "open")

    def test_dispatch_failure_releases_capacity_and_removes_git_artifacts(self):
        item, _ = self.ready_item()
        from agents.git_worktree import reserve_execution as real_reserve

        def fail_after_git(*args, **kwargs):
            real_reserve(*args, **kwargs)
            raise RuntimeError("injected dispatch failure")

        with (
            patch("agents.delivery.reserve_execution", side_effect=fail_after_git),
            self.assertRaisesRegex(RuntimeError, "injected dispatch failure"),
        ):
            self.delivery.dispatch_next()
        work = self.connection.execute("SELECT * FROM work_items WHERE id=?", (item,)).fetchone()
        self.assertEqual(work["status"], "ready")
        self.assertIsNone(work["active_execution_id"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM assignments WHERE state='open'").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM actor_leases WHERE released_at IS NULL").fetchone()[0],
            0,
        )
        execution = self.connection.execute("SELECT * FROM executions WHERE work_id=?", (item,)).fetchone()
        self.assertEqual(execution["state"], "superseded")
        self.assertFalse(Path(execution["worktree_path"]).exists())
        branch = subprocess.run(
            ["git", "-C", str(self.config.project.path), "show-ref", "--verify", f"refs/heads/{execution['branch']}"],
            check=False,
            capture_output=True,
        )
        self.assertNotEqual(branch.returncode, 0)

    def test_dispatch_adopts_durable_preparing_execution(self):
        item, _ = self.ready_item()
        with (
            patch.object(self.delivery, "_materialize_dispatch", side_effect=SystemExit("crash")),
            self.assertRaisesRegex(SystemExit, "crash"),
        ):
            self.delivery.dispatch_next()
        preparing = self.connection.execute("SELECT * FROM executions WHERE work_id=?", (item,)).fetchone()
        self.assertEqual(preparing["state"], "preparing")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM assignments WHERE work_id=?", (item,)).fetchone()[0],
            1,
        )
        adopted = self.delivery.dispatch_next()
        self.assertIsNotNone(adopted)
        adopted = cast(dict[str, Any], adopted)
        self.assertEqual(adopted["execution_id"], preparing["id"])
        self.assertEqual(
            self.connection.execute("SELECT state FROM executions WHERE id=?", (preparing["id"],)).fetchone()[0],
            "active",
        )

    def test_restart_blocker_deletes_old_terminal_and_dispatches_new_generation(self):
        item, _ = self.ready_item()
        dispatch = cast(dict[str, Any], self.delivery.dispatch_next())
        old_run_id = int(dispatch["terminal_run_id"])
        old = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (old_run_id,)).fetchone()
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',backend_run_id=?,backend_terminal_id=?,updated_at=? WHERE id=?",
            (str(old["execution_name"]), "old-terminal", utc_now(), old_run_id),
        )
        self.connection.execute(
            "UPDATE launch_attempts SET state='succeeded',counted=1,updated_at=? WHERE terminal_run_id=?",
            (utc_now(), old_run_id),
        )
        now = utc_now()
        self.connection.execute(
            "UPDATE work_items SET status='blocked',blocked_from='in_progress',version=version+1,updated_at=? WHERE id=?",
            (now, item),
        )
        blocker = self.connection.execute(
            "INSERT INTO blockers(work_id,target_kind,target_id,terminal_run_id,kind,reason,requested_role,"
            "actor_slug,resume_state,state,created_at,updated_at)VALUES(?,'work',?,?,'missing_session','stale',"
            "'human','system','in_progress','open',?,?) RETURNING id",
            (item, item, old_run_id, now, now),
        ).fetchone()
        self.assertIsNotNone(blocker)

        class FakeBackend:
            def __init__(self):
                self.deleted: list[RunHandle] = []

            def delete_run(self, handle: RunHandle) -> None:
                self.deleted.append(handle)

        fake = FakeBackend()
        delivery = Delivery(self.config, self.connection, backend=cast(Any, fake))
        result = delivery.resolve_blocker("human", int(blocker["id"]), "replace", "restart")
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(
            fake.deleted, [RunHandle(str(old["execution_name"]), str(old["execution_name"]), "old-terminal")]
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (old_run_id,)
            ).fetchone()["state"],
            "ending",
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM actor_leases WHERE terminal_run_id=? AND released_at IS NULL", (old_run_id,)
            ).fetchone()
        )
        replacement = self.connection.execute(
            "SELECT * FROM terminal_runs WHERE purpose_kind='work' AND purpose_id=? AND id<>? ORDER BY id DESC LIMIT 1",
            (item, old_run_id),
        ).fetchone()
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["generation"], 2)
        self.assertIn("-w-1-2-writer-g0002", replacement["execution_name"])
