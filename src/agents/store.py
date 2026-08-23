from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from .config import AgentsConfig
from .db import initialize_project, utc_now
from .messages import create_work_conversation, seed_conversations
from .policy import (
    DomainError,
    authorize_reopen,
    authorize_scope_mutation,
    authorize_transition,
    validate_scope,
    validate_text,
    validate_title,
)


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize(self, config: AgentsConfig) -> sqlite3.Row:
        project = initialize_project(self.connection, config)
        now = utc_now()
        with self.connection:
            for actor in config.actors:
                self.connection.execute(
                    "INSERT OR IGNORE INTO actors(slug,kind,reports_to,profile_template,specialty,persistent,capacity,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        actor["slug"],
                        actor["kind"],
                        actor.get("reports_to") or None,
                        actor.get("profile_template") or None,
                        actor.get("specialty") or None,
                        int(bool(actor.get("persistent"))),
                        int(actor.get("capacity", 1)),
                        now,
                        now,
                    ),
                )
            seed_conversations(self.connection)
        return project

    def get_work(self, item_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", f"work item {item_id} does not exist")
        return row

    def create_work(
        self, *, actor: str, parent_id: str | None, kind: str, title: str, problem: str, outcome: str
    ) -> dict[str, Any]:
        if actor not in {"human", "manager", "system"}:
            raise DomainError("unauthorized", "only the human, manager, or scheduler may create work")
        validate_title(title)
        validate_text(problem, "problem")
        validate_text(outcome, "outcome")
        if kind not in {"story", "bug", "task", "spike"}:
            raise DomainError("validation_failed", "invalid work kind")
        if parent_id is not None:
            self.get_work(parent_id)
        project = self.connection.execute("SELECT next_work_seq FROM project WHERE id=1").fetchone()
        if project is None:
            raise DomainError("not_initialized", "Agents project is not initialized")
        seq = int(project["next_work_seq"])
        item_id = f"AGENT-{seq:04d}"
        now = utc_now()
        self.connection.execute("UPDATE project SET next_work_seq=next_work_seq+1,updated_at=? WHERE id=1", (now,))
        self.connection.execute(
            "INSERT INTO work_items(id,seq,parent_id,kind,title,problem,outcome,status,priority,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'intake','normal',1,?,?)",
            (item_id, seq, parent_id, kind, title, problem, outcome, now, now),
        )
        create_work_conversation(self.connection, item_id)
        return {"id": item_id, "version": 1, "status": "intake"}

    def transition(
        self, *, actor: str, item_id: str, expected_version: int, target: str, assigned_actor: str | None = None
    ) -> dict[str, Any]:
        row = self.get_work(item_id)
        self._expect_version(row, expected_version)
        source = str(row["status"])
        authorize_transition(actor, source, target, assigned_actor=assigned_actor)
        if target == "ready":
            self.assert_ready(item_id)
        blocked_from = source if target == "blocked" else (None if source == "blocked" else row["blocked_from"])
        self.connection.execute(
            "UPDATE work_items SET status=?,blocked_from=?,version=version+1,updated_at=? WHERE id=?",
            (target, blocked_from, utc_now(), item_id),
        )
        return {"id": item_id, "status": target, "version": expected_version + 1}

    def refine(
        self,
        *,
        actor: str,
        item_id: str,
        expected_version: int,
        kind: str,
        title: str,
        problem: str,
        outcome: str,
        priority: str,
        specialty: str,
        criteria: list[str],
        dependencies: list[str],
        gates: list[str],
    ) -> dict[str, Any]:
        row = self.get_work(item_id)
        self._expect_version(row, expected_version)
        authorize_scope_mutation(actor, str(row["status"]))
        validate_scope(
            kind=kind,
            title=title,
            problem=problem,
            outcome=outcome,
            priority=priority,
            specialty=specialty,
            criteria=criteria,
            dependencies=dependencies,
            gates=gates,
        )
        for dependency in dependencies:
            self.get_work(dependency)
        self._assert_acyclic(item_id, dependencies)
        now = utc_now()
        self.connection.execute(
            "UPDATE work_items SET kind=?,title=?,problem=?,outcome=?,priority=?,specialty=?,version=version+1,updated_at=? WHERE id=?",
            (kind, title, problem, outcome, priority, specialty, now, item_id),
        )
        self.connection.execute("DELETE FROM acceptance_criteria WHERE work_id=?", (item_id,))
        self.connection.executemany(
            "INSERT INTO acceptance_criteria(work_id,position,body) VALUES (?,?,?)",
            ((item_id, position, body) for position, body in enumerate(criteria, 1)),
        )
        self.connection.execute("DELETE FROM dependencies WHERE work_id=?", (item_id,))
        self.connection.executemany(
            "INSERT INTO dependencies(work_id,depends_on_id) VALUES (?,?)",
            ((item_id, dependency) for dependency in dependencies),
        )
        required_gates = list(dict.fromkeys(["research", *gates]))
        self.connection.execute("DELETE FROM review_requirements WHERE work_id=?", (item_id,))
        self.connection.executemany(
            "INSERT INTO review_requirements(work_id,gate) VALUES (?,?)", ((item_id, gate) for gate in required_gates)
        )
        return {"id": item_id, "status": "refining", "version": expected_version + 1}

    def assert_ready(self, item_id: str) -> None:
        row = self.get_work(item_id)
        if not row["problem"] or not row["outcome"] or row["specialty"] is None:
            raise DomainError("not_ready", "problem, outcome, and specialty are required")
        if (
            self.connection.execute("SELECT COUNT(*) FROM acceptance_criteria WHERE work_id=?", (item_id,)).fetchone()[
                0
            ]
            < 1
        ):
            raise DomainError("not_ready", "acceptance criteria are required")
        if self.connection.execute("SELECT 1 FROM decisions WHERE work_id=? AND state='open'", (item_id,)).fetchone():
            raise DomainError("not_ready", "an open decision blocks readiness")
        if not self.connection.execute(
            "SELECT 1 FROM consultations WHERE work_id=? AND specialty=? AND state='completed'",
            (item_id, row["specialty"]),
        ).fetchone():
            raise DomainError("not_ready", "a completed matching-specialty consultation is required")
        dependencies = [
            str(value[0])
            for value in self.connection.execute("SELECT depends_on_id FROM dependencies WHERE work_id=?", (item_id,))
        ]
        self._assert_acyclic(item_id, dependencies)

    def dependencies_delivered(self, item_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM dependencies d JOIN work_items w ON w.id=d.depends_on_id WHERE d.work_id=? AND w.status<>'delivered'",
                (item_id,),
            ).fetchone()
            is None
        )

    def reopen(self, *, actor: str, item_id: str, expected_version: int, reason: str) -> dict[str, Any]:
        validate_text(reason, "reason")
        row = self.get_work(item_id)
        self._expect_version(row, expected_version)
        authorize_reopen(actor, str(row["status"]), row["active_execution_id"] is not None)
        self._fence_work(item_id, "reopened")
        self.connection.execute(
            "UPDATE work_items SET status='refining',blocked_from=NULL,active_execution_id=NULL,accepted_submission_id=NULL,integration_sha=NULL,version=version+1,updated_at=? WHERE id=?",
            (utc_now(), item_id),
        )
        return {"id": item_id, "status": "refining", "version": expected_version + 1}

    def cancel(self, *, actor: str, item_id: str, expected_version: int, reason: str) -> dict[str, Any]:
        validate_text(reason, "reason")
        row = self.get_work(item_id)
        self._expect_version(row, expected_version)
        authorize_transition(actor, str(row["status"]), "cancelled")
        self._fence_work(item_id, reason)
        self.connection.execute(
            "UPDATE work_items SET status='cancelled',blocked_from=NULL,active_execution_id=NULL,accepted_submission_id=NULL,integration_sha=NULL,version=version+1,updated_at=? WHERE id=?",
            (utc_now(), item_id),
        )
        return {"id": item_id, "status": "cancelled", "version": expected_version + 1}

    def _fence_work(self, item_id: str, reason: str) -> None:
        now = utc_now()
        execution_ids = [
            int(row[0])
            for row in self.connection.execute(
                "SELECT id FROM executions WHERE work_id=? AND state IN ('preparing','active')", (item_id,)
            )
        ]
        if execution_ids:
            marks = ",".join("?" for _ in execution_ids)
            self.connection.execute(
                f"UPDATE submissions SET state='superseded',updated_at=? WHERE execution_id IN ({marks}) AND state<>'accepted'",
                (now, *execution_ids),
            )
            self.connection.execute(
                f"UPDATE approvals SET state='superseded',updated_at=? WHERE submission_id IN (SELECT id FROM submissions WHERE execution_id IN ({marks})) AND state='pending'",
                (now, *execution_ids),
            )
        self.connection.execute(
            "UPDATE assignments SET state='closed',reason=?,updated_at=? WHERE work_id=? AND state='open'",
            (reason, now, item_id),
        )
        self.connection.execute(
            "UPDATE executions SET state='superseded',updated_at=? WHERE work_id=? AND state IN ('preparing','active')",
            (now, item_id),
        )
        self.connection.execute(
            "UPDATE actor_leases SET released_at=? WHERE purpose_kind='work' AND purpose_id=? AND released_at IS NULL",
            (now, item_id),
        )
        run_ids = [
            int(row[0])
            for row in self.connection.execute(
                "SELECT id FROM terminal_runs WHERE purpose_kind='work' AND purpose_id=? "
                "AND state IN ('reserved','creating','live','retained')",
                (item_id,),
            )
        ]
        self.connection.execute(
            "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),"
            "state=CASE WHEN state IN ('reserved','creating','live','retained') THEN 'ending' ELSE state END,"
            "error=COALESCE(error,?),updated_at=? WHERE purpose_kind='work' AND purpose_id=? "
            "AND state IN ('reserved','creating','live','retained','ending')",
            (now, reason, now, item_id),
        )
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            self.connection.execute(
                f"UPDATE launch_attempts SET state='aborted',error=?,updated_at=? "
                f"WHERE terminal_run_id IN ({marks}) AND state='reserved'",
                (reason, now, *run_ids),
            )
        self.connection.execute(
            "UPDATE blockers SET state='resolved',resolution=?,updated_at=? WHERE work_id=? AND state IN ('open','escalated')",
            (reason, now, item_id),
        )

    def _expect_version(self, row: sqlite3.Row, expected: int) -> None:
        if int(row["version"]) != expected:
            raise DomainError("stale_version", "work item version changed", dict(row))

    def _assert_acyclic(self, item_id: str, proposed: Iterable[str]) -> None:
        graph: dict[str, set[str]] = {}
        for row in self.connection.execute(
            "SELECT work_id,depends_on_id FROM dependencies WHERE work_id<>?", (item_id,)
        ):
            graph.setdefault(str(row[0]), set()).add(str(row[1]))
        graph[item_id] = set(proposed)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise DomainError("dependency_cycle", "dependencies must be acyclic")
            if node in visited:
                return
            visiting.add(node)
            for neighbor in graph.get(node, set()):
                visit(neighbor)
            visiting.remove(node)
            visited.add(node)

        visit(item_id)


def migration_versions(connection: sqlite3.Connection) -> list[int]:
    return [int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
