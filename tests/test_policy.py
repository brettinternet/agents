from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import connect, migrate, utc_now
from agents.policy import DomainError, authorize_reopen, authorize_transition, validate_scope
from agents.store import Store
from agents.workflow import Workflow


class PolicyTests(unittest.TestCase):
    def test_authority_matrix_rejects_impersonation(self) -> None:
        authorize_transition("elder", "intake", "refining")
        authorize_transition("system:dispatcher", "ready", "in_progress")
        authorize_transition("explorer", "in_progress", "verifying", assigned_actor="explorer")
        authorize_transition("human", "awaiting_approval", "accepted")
        authorize_transition("system:reconciler", "accepted", "delivered")
        with self.assertRaises(DomainError):
            authorize_transition("elder", "awaiting_approval", "accepted")
        with self.assertRaises(DomainError):
            authorize_transition("writer", "in_progress", "verifying", assigned_actor="explorer")
        with self.assertRaises(DomainError):
            authorize_reopen("elder", "in_progress", True)

    def test_scope_bounds_and_mandatory_values(self) -> None:
        validate_scope(
            kind="story",
            title="Title",
            problem="P",
            outcome="O",
            priority="normal",
            specialty="research",
            criteria=["observable"],
            dependencies=[],
            gates=[],
        )
        with self.assertRaises(DomainError):
            validate_scope(
                kind="story",
                title="x" * 201,
                problem="P",
                outcome="O",
                priority="normal",
                specialty="research",
                criteria=["ok"],
                dependencies=[],
                gates=[],
            )
        with self.assertRaises(DomainError):
            validate_scope(
                kind="story",
                title="x",
                problem="P",
                outcome="O",
                priority="normal",
                specialty="research",
                criteria=[],
                dependencies=[],
                gates=[],
            )


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        repo = root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        self.connection = connect(root / "agents.db")
        migrate(self.connection)
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
            (
                {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
                {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
                {
                    "slug": "elder",
                    "kind": "agent",
                    "reports_to": "human",
                    "specialty": "",
                    "persistent": True,
                    "capacity": 1,
                },
                {
                    "slug": "explorer",
                    "kind": "agent",
                    "reports_to": "elder",
                    "specialty": "research",
                    "persistent": True,
                    "capacity": 1,
                },
                {
                    "slug": "writer",
                    "kind": "agent",
                    "reports_to": "elder",
                    "specialty": "publishing",
                    "persistent": True,
                    "capacity": 1,
                },
            ),
        )
        Store(self.connection).initialize(config)
        self.workflow = Workflow(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _create_refining(self, request: str, title: str) -> tuple[str, int]:
        created = self.workflow.create_work(
            request, "elder", parent_id=None, kind="story", title=title, problem="Problem", outcome="Outcome"
        )
        started = self.workflow.start_refinement(request + "-start", "elder", created["id"], created["version"])
        return created["id"], started["version"]

    def test_work_ids_versions_replay_and_events_are_atomic(self) -> None:
        first = self.workflow.create_work(
            "create-1", "human", parent_id=None, kind="task", title="First", problem="P", outcome="O"
        )
        replay = self.workflow.create_work(
            "create-1", "human", parent_id=None, kind="task", title="First", problem="P", outcome="O"
        )
        second = self.workflow.create_work(
            "create-2", "human", parent_id=None, kind="task", title="Second", problem="P", outcome="O"
        )
        self.assertEqual(first, replay)
        self.assertEqual((first["id"], second["id"]), ("AGENT-0001", "AGENT-0002"))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM events WHERE kind='work.created'").fetchone()[0], 2
        )

    def test_readiness_requires_matching_consultation_and_freezes_scope(self) -> None:
        item_id, version = self._create_refining("ready", "Ready")
        refined = self.workflow.refine(
            "ready-refine",
            "elder",
            item_id,
            version,
            kind="story",
            title="Ready",
            problem="P",
            outcome="O",
            priority="high",
            specialty="research",
            criteria=["passes"],
            dependencies=[],
            gates=["coordination"],
        )
        with self.assertRaises(DomainError):
            self.workflow.mark_ready("ready-early", "elder", item_id, refined["version"])
        now = utc_now()
        self.connection.execute(
            "INSERT INTO consultations(work_id,specialty,question,requester,state,response,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, "research", "Review", "elder", "completed", "Looks good", 1, now, now),
        )
        ready = self.workflow.mark_ready("ready-final", "elder", item_id, refined["version"])
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(
            {
                row[0]
                for row in self.connection.execute("SELECT gate FROM review_requirements WHERE work_id=?", (item_id,))
            },
            {"research", "coordination"},
        )
        with self.assertRaises(DomainError):
            self.workflow.refine(
                "ready-change",
                "elder",
                item_id,
                ready["version"],
                kind="story",
                title="Changed",
                problem="P",
                outcome="O",
                priority="high",
                specialty="research",
                criteria=["passes"],
                dependencies=[],
                gates=[],
            )

    def test_dependency_cycle_is_rejected(self) -> None:
        first, first_version = self._create_refining("a", "A")
        second, second_version = self._create_refining("b", "B")
        self.workflow.refine(
            "a-refine",
            "elder",
            first,
            first_version,
            kind="story",
            title="A",
            problem="P",
            outcome="O",
            priority="normal",
            specialty="research",
            criteria=["a"],
            dependencies=[second],
            gates=[],
        )
        with self.assertRaises(DomainError):
            self.workflow.refine(
                "b-refine",
                "elder",
                second,
                second_version,
                kind="story",
                title="B",
                problem="P",
                outcome="O",
                priority="normal",
                specialty="research",
                criteria=["b"],
                dependencies=[first],
                gates=[],
            )


if __name__ == "__main__":
    unittest.main()
