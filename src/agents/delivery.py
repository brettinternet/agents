from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import AgentsConfig
from .db import utc_now
from .execution import (
    ExecutionBackend,
    ExecutionBusy,
    ExecutionError,
    ExecutionNotFound,
    ExecutionTimeout,
    ExecutionUnavailable,
    RunHandle,
)
from .git_worktree import (
    add_detached,
    branch_sha,
    discard_execution_reservation,
    head_sha,
    is_ancestor,
    is_clean,
    remove_recorded_worktree,
    reserve_execution,
)
from .herdr_client import HerdrBackend
from .policy import CONSULTATION_SPECIALTIES, DomainError, validate_text, validate_title
from .reconciler import reserve_terminal
from .store import Store


class _ReservationConflict(RuntimeError):
    pass


_OUTPUT_TAIL_BYTES = 64 * 1024
CHECK_TIMEOUT_SECONDS = 1800
PROCESS_TERM_GRACE_SECONDS = 10


class _TailRing:
    """Bounded byte ring that keeps the final output without stopping readers."""

    def __init__(self, limit: int = _OUTPUT_TAIL_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buffer.extend(chunk)
        if len(self._buffer) > self._limit:
            del self._buffer[: len(self._buffer) - self._limit]
            self.truncated = True

    def text(self) -> str:
        return bytes(self._buffer).decode(errors="replace")


async def _drain_pipe(stream: asyncio.StreamReader | None, ring: _TailRing) -> _TailRing:
    if stream is None:
        return ring
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return ring
        ring.append(chunk)


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Terminate every process in a check-owned process group."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(asyncio.shield(process.wait()), PROCESS_TERM_GRACE_SECONDS)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    await asyncio.sleep(PROCESS_TERM_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(asyncio.shield(process.wait()), PROCESS_TERM_GRACE_SECONDS)


class Delivery:
    def __init__(
        self,
        config: AgentsConfig,
        connection: sqlite3.Connection,
        backend: ExecutionBackend | None = None,
    ) -> None:
        self.config = config
        self.connection = connection
        self.backend = backend or HerdrBackend.from_config(config)

    def request_consultation(
        self, actor: str, item_id: str, expected_version: int, specialty: str, question: str
    ) -> dict[str, Any]:
        work = Store(self.connection).get_work(item_id)
        self._version(work, expected_version)
        if actor not in {"human", "elder"}:
            raise DomainError("unauthorized", "actor cannot request consultation")
        if specialty not in CONSULTATION_SPECIALTIES:
            raise DomainError("validation_failed", "invalid consultation specialty")
        validate_text(question, "consultation question")
        now = utc_now()
        cursor = self.connection.execute(
            "INSERT INTO consultations(work_id,specialty,question,requester,state,version,created_at,updated_at)VALUES(?,?,?,?,'queued',1,?,?)",
            (item_id, specialty, question, actor, now, now),
        )
        return {"id": cursor.lastrowid, "version": 1, "state": "queued"}

    def _elder_available(self) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM consultations WHERE responder='elder' AND state='assigned' "
                "UNION ALL SELECT 1 FROM reviews WHERE actor_slug='elder' AND verdict='pending' LIMIT 1"
            ).fetchone()
            is None
        )

    def _persistent_terminal(self, actor: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT id FROM terminal_runs WHERE actor_slug=? AND purpose_kind='persistent' "
            "AND state='live' AND token_revoked_at IS NULL ORDER BY generation DESC LIMIT 1",
            (actor,),
        ).fetchone()

    def dispatch_consultation_next(self) -> dict[str, Any] | None:
        """Atomically assign one queued consultation and its terminal lease."""
        self.connection.execute("SAVEPOINT dispatch_consultation")
        try:
            result = self._dispatch_consultation_next()
            self.connection.execute("RELEASE SAVEPOINT dispatch_consultation")
            return result
        except _ReservationConflict:
            self.connection.execute("ROLLBACK TO SAVEPOINT dispatch_consultation")
            self.connection.execute("RELEASE SAVEPOINT dispatch_consultation")
            return None
        except BaseException:
            self.connection.execute("ROLLBACK TO SAVEPOINT dispatch_consultation")
            self.connection.execute("RELEASE SAVEPOINT dispatch_consultation")
            raise

    def _dispatch_consultation_next(self) -> dict[str, Any] | None:
        """Assign one queued consultation while respecting the global capacity."""
        active = int(self.connection.execute("SELECT COUNT(*) FROM consultations WHERE state='assigned'").fetchone()[0])
        if active >= self.config.runtime.max_consultations:
            return None
        consultation = self.connection.execute(
            "SELECT * FROM consultations WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if consultation is None:
            return None
        specialty = str(consultation["specialty"])
        actor = None
        terminal_run_id: int
        if specialty == "coordination":
            if not self._elder_available():
                return None
            actor = self.connection.execute(
                "SELECT slug FROM actors WHERE slug='elder' AND kind='agent' AND persistent=1"
            ).fetchone()
            if actor is None:
                return None
            persistent = self._persistent_terminal(str(actor["slug"]))
            if persistent is None:
                return None
            actor_slug = str(actor["slug"])
            terminal_run_id = int(persistent["id"])
        else:
            actor = self.connection.execute(
                "SELECT a.* FROM actors a "
                "WHERE a.specialty=? AND a.kind='agent' "
                "AND NOT EXISTS (SELECT 1 FROM consultations c "
                "WHERE c.responder=a.slug AND c.state='assigned') "
                "AND NOT EXISTS (SELECT 1 FROM reviews r "
                "WHERE r.actor_slug=a.slug AND r.verdict='pending') "
                "AND ("
                "(a.persistent=1 AND EXISTS (SELECT 1 FROM terminal_runs t "
                "WHERE t.actor_slug=a.slug AND t.purpose_kind='persistent' "
                "AND t.state='live' AND t.token_revoked_at IS NULL)) "
                "OR (a.persistent=0 AND (SELECT COUNT(*) FROM actor_leases l "
                "WHERE l.actor_slug=a.slug AND l.released_at IS NULL) < a.capacity)"
                ") ORDER BY a.slug LIMIT 1",
                (specialty,),
            ).fetchone()
            if actor is None:
                return None
            actor_slug = str(actor["slug"])
            if bool(actor["persistent"]):
                persistent = self._persistent_terminal(actor_slug)
                if persistent is None:
                    return None
                terminal_run_id = int(persistent["id"])
            else:
                run = reserve_terminal(
                    self.connection,
                    self.config,
                    actor=actor_slug,
                    purpose_kind="consultation",
                    purpose_id=str(consultation["id"]),
                    working_directory=self.config.project.path,
                )
                terminal_run_id = int(run["id"])
        now = utc_now()
        changed = self.connection.execute(
            "UPDATE consultations SET responder=?,state='assigned',terminal_run_id=?,"
            "version=version+1,updated_at=? WHERE id=? AND state='queued'",
            (actor_slug, terminal_run_id, now, consultation["id"]),
        )
        if changed.rowcount != 1:
            raise _ReservationConflict
        conversation = self.connection.execute(
            "SELECT id FROM conversations WHERE address=?",
            (f"work:{consultation['work_id']}",),
        ).fetchone()
        if conversation is not None:
            message = self.connection.execute(
                "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
                "VALUES(?,'system',?,'normal',?)",
                (
                    conversation["id"],
                    f"Consultation {consultation['id']} assigned to @{actor_slug}.",
                    now,
                ),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
                "VALUES(?,?,?,'pending',0,?)",
                (message.lastrowid, actor_slug, terminal_run_id, now),
            )
        return {
            "id": int(consultation["id"]),
            "work_id": str(consultation["work_id"]),
            "specialty": specialty,
            "actor": actor_slug,
            "terminal_run_id": terminal_run_id,
            "state": "assigned",
        }

    def _abort_reserved_terminal(self, run_id: int) -> None:
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='failed',profile_state='failed',"
            "token_revoked_at=COALESCE(token_revoked_at,?),updated_at=? WHERE id=?",
            (now, now, run_id),
        )
        self.connection.execute(
            "UPDATE launch_attempts SET state='aborted',counted=0,updated_at=? "
            "WHERE terminal_run_id=? AND state='reserved'",
            (now, run_id),
        )
        self.connection.execute(
            "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
            (now, run_id),
        )

    def submit_consultation(
        self, actor: str, consultation_id: int, expected_version: int, response: str, terminal_run_id: int
    ) -> dict[str, Any]:
        validate_text(response, "consultation response", maximum=2048)
        row = self.connection.execute("SELECT * FROM consultations WHERE id=?", (consultation_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "consultation does not exist")
        if (
            int(row["version"]) != expected_version
            or row["state"] != "assigned"
            or row["responder"] != actor
            or row["terminal_run_id"] != terminal_run_id
        ):
            raise DomainError("stale_generation", "consultation assignment changed")
        self.connection.execute(
            "UPDATE consultations SET state='completed',response=?,version=version+1,updated_at=? WHERE id=?",
            (response, utc_now(), consultation_id),
        )
        conversation = self.connection.execute(
            "SELECT id FROM conversations WHERE address=?",
            (f"work:{row['work_id']}",),
        ).fetchone()
        if conversation is not None:
            now = utc_now()
            message = self.connection.execute(
                "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
                "VALUES(?,'system',?,'normal',?)",
                (conversation["id"], f"Consultation {consultation_id} completed.", now),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries(message_id,actor_slug,state,attempts,next_attempt_at)"
                "VALUES(?,?,'pending',0,?)",
                (message.lastrowid, row["requester"], now),
            )
        # Consultation terminals are purpose-scoped; persistent terminals remain
        # available for the actor's hierarchy and are never torn down here.
        purpose = self.connection.execute(
            "SELECT purpose_kind FROM terminal_runs WHERE id=?", (terminal_run_id,)
        ).fetchone()
        if purpose is not None and purpose["purpose_kind"] != "persistent":
            self._end_terminal(terminal_run_id)
        return {"id": consultation_id, "state": "completed", "version": expected_version + 1}

    def propose_decision(
        self, actor: str, *, item_id: str | None, title: str, question: str, options: list[str], recommendation: str
    ) -> dict[str, Any]:
        if actor not in {"elder", "human"}:
            raise DomainError("unauthorized", "actor cannot propose decisions")
        validate_title(title)
        validate_text(question, "decision question")
        validate_text(recommendation, "decision recommendation", maximum=2048)
        for option in options:
            validate_text(option, "decision option", maximum=2048)
        if not 2 <= len(options) <= 5 or len(options) != len(set(options)):
            raise DomainError("validation_failed", "decision options must contain 2-5 unique values")
        now = utc_now()
        cursor = self.connection.execute(
            "INSERT INTO decisions(work_id,title,question,options_json,recommendation,state,proposed_by,created_at,updated_at)VALUES(?,?,?,?,?,'open',?,?,?)",
            (item_id, title, question, json.dumps(options), recommendation, actor, now, now),
        )
        return {"id": cursor.lastrowid, "state": "open"}

    def resolve_decision(
        self, decision_id: int, item_id: str | None, expected_version: int | None, resolution: str
    ) -> dict[str, Any]:
        validate_text(resolution, "decision resolution")
        row = self.connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None or row["state"] != "open":
            raise DomainError("not_found", "open decision does not exist")
        linked_work_id = str(row["work_id"]) if row["work_id"] is not None else None
        if linked_work_id is not None:
            if item_id != linked_work_id or expected_version is None:
                raise DomainError("stale_version", "linked work version required")
            self._version(Store(self.connection).get_work(linked_work_id), expected_version)
            self.connection.execute(
                "UPDATE work_items SET version=version+1,updated_at=? WHERE id=?", (utc_now(), linked_work_id)
            )
        self.connection.execute(
            "UPDATE decisions SET state='resolved',resolution=?,resolved_by='human',updated_at=? WHERE id=?",
            (resolution, utc_now(), decision_id),
        )
        return {"id": decision_id, "state": "resolved"}

    def dispatch_next(self) -> dict[str, Any] | None:
        preparing = self.connection.execute(
            "SELECT id FROM executions WHERE state='preparing' ORDER BY id LIMIT 1"
        ).fetchone()
        if preparing is not None:
            return self._materialize_dispatch(int(preparing["id"]))
        managed = not self.connection.in_transaction
        if managed:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            active = int(self.connection.execute("SELECT COUNT(*) FROM assignments WHERE state='open'").fetchone()[0])
            if active >= self.config.runtime.max_agents:
                if managed:
                    self.connection.execute("COMMIT")
                return None
            rows = self.connection.execute(
                "SELECT * FROM work_items WHERE status='ready' "
                "ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'normal' THEN 2 ELSE 3 END,seq"
            )
            store = Store(self.connection)
            for work in rows:
                if not store.dependencies_delivered(str(work["id"])):
                    continue
                actor = self.connection.execute(
                    "SELECT a.* FROM actors a WHERE a.kind='agent' AND a.specialty=? "
                    "AND (SELECT COUNT(*) FROM actor_leases l "
                    "WHERE l.actor_slug=a.slug AND l.released_at IS NULL) < a.capacity "
                    "ORDER BY a.slug LIMIT 1",
                    (work["specialty"],),
                ).fetchone()
                if actor is None:
                    continue
                number = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(number),0)+1 FROM executions WHERE work_id=?",
                        (work["id"],),
                    ).fetchone()[0]
                )
                worktree = self.config.root / ".worktrees/work" / f"{str(work['id']).lower()}-{number}"
                base = branch_sha(self.config.project.path, self.config.project.default_branch)
                branch = f"agents/{str(work['id']).lower()}/{number}"
                now = utc_now()
                cursor = self.connection.execute(
                    "INSERT INTO executions("
                    "work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,'preparing',?,?)",
                    (work["id"], number, base, branch, str(worktree), now, now),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not allocate execution")
                execution_id = cursor.lastrowid
                run = reserve_terminal(
                    self.connection,
                    self.config,
                    actor=str(actor["slug"]),
                    purpose_kind="work",
                    purpose_id=str(work["id"]),
                    working_directory=worktree,
                )
                self.connection.execute(
                    "INSERT INTO assignments("
                    "work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at"
                    ") VALUES(?,?,?,?,'open',?,?)",
                    (work["id"], execution_id, actor["slug"], run["id"], now, now),
                )
                changed = self.connection.execute(
                    "UPDATE work_items SET status='in_progress',active_execution_id=?,"
                    "version=version+1,updated_at=? WHERE id=? AND status='ready'",
                    (execution_id, now, work["id"]),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("ready work changed during dispatch")
                if managed:
                    self.connection.execute("COMMIT")
                return self._materialize_dispatch(execution_id)
            if managed:
                self.connection.execute("COMMIT")
            return None
        except BaseException:
            if managed and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _materialize_dispatch(self, execution_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT e.*,a.actor_slug,a.terminal_run_id FROM executions e "
            "JOIN assignments a ON a.execution_id=e.id AND a.state='open' WHERE e.id=?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("preparing execution has no open assignment")
        worktree = Path(str(row["worktree_path"]))
        try:
            current = branch_sha(self.config.project.path, self.config.project.default_branch)
            if current != row["base_sha"]:
                raise DomainError("base_changed", "default branch advanced before dispatch")
            base, branch = reserve_execution(
                self.config.project.path,
                self.config.project.default_branch,
                str(row["work_id"]),
                int(row["number"]),
                worktree,
            )
            if base != row["base_sha"] or branch != row["branch"]:
                raise RuntimeError("execution reservation changed")
            self.connection.execute(
                "UPDATE executions SET state='active',updated_at=? WHERE id=? AND state='preparing'",
                (utc_now(), execution_id),
            )
        except BaseException:
            self._fail_dispatch(row)
            raise
        now = utc_now()
        conversation = self.connection.execute(
            "SELECT id FROM conversations WHERE address=?",
            (f"work:{row['work_id']}",),
        ).fetchone()
        if conversation is not None:
            message = self.connection.execute(
                "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
                "VALUES(?,'system',?,'normal',?)",
                (conversation["id"], f"Implementation assigned to @{row['actor_slug']}.", now),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
                "VALUES(?,?,?,'pending',0,?)",
                (message.lastrowid, row["actor_slug"], row["terminal_run_id"], now),
            )
        return {
            "item_id": row["work_id"],
            "execution_id": execution_id,
            "terminal_run_id": row["terminal_run_id"],
            "actor": row["actor_slug"],
            "base_sha": row["base_sha"],
            "branch": row["branch"],
            "worktree": str(worktree),
        }

    def _fail_dispatch(self, row: sqlite3.Row) -> None:
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE assignments SET state='closed',reason='dispatch_failed',updated_at=? "
                "WHERE execution_id=? AND state='open'",
                (now, row["id"]),
            )
            self.connection.execute(
                "UPDATE terminal_runs SET state='failed',token_revoked_at=COALESCE(token_revoked_at,?),"
                "error='dispatch worktree failed',updated_at=? WHERE id=?",
                (now, now, row["terminal_run_id"]),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='aborted',updated_at=? WHERE terminal_run_id=? AND state='reserved'",
                (now, row["terminal_run_id"]),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
                (now, row["terminal_run_id"]),
            )
            self.connection.execute(
                "UPDATE executions SET state='superseded',updated_at=? WHERE id=? AND state='preparing'",
                (now, row["id"]),
            )
            self.connection.execute(
                "UPDATE work_items SET status='ready',active_execution_id=NULL,version=version+1,updated_at=? "
                "WHERE id=? AND active_execution_id=?",
                (now, row["work_id"], row["id"]),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        discard_execution_reservation(
            self.config.project.path,
            Path(str(row["worktree_path"])),
            str(row["branch"]),
            str(row["base_sha"]),
        )

    def submit_work(
        self, actor: str, item_id: str, expected_version: int, commit_sha: str, summary: str, terminal_run_id: int
    ) -> dict[str, Any]:
        summary = validate_text(summary, "submission summary")
        work = Store(self.connection).get_work(item_id)
        self._version(work, expected_version)
        assignment = self.connection.execute(
            "SELECT a.*,e.base_sha,e.branch,e.worktree_path FROM assignments a JOIN executions e ON e.id=a.execution_id WHERE a.work_id=? AND a.actor_slug=? AND a.terminal_run_id=? AND a.state='open' AND e.state='active'",
            (item_id, actor, terminal_run_id),
        ).fetchone()
        if assignment is None or work["status"] != "in_progress":
            raise DomainError("stale_generation", "implementation assignment changed")
        path = Path(str(assignment["worktree_path"]))
        default = branch_sha(self.config.project.path, self.config.project.default_branch)
        if default != assignment["base_sha"]:
            raise DomainError("base_changed", "default branch advanced")
        if (
            not is_clean(path)
            or head_sha(path) != commit_sha
            or not is_ancestor(self.config.project.path, str(assignment["base_sha"]), commit_sha)
            or commit_sha == assignment["base_sha"]
        ):
            raise DomainError("invalid_submission", "submission must be a clean committed descendant")
        revision = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM submissions WHERE execution_id=?",
                (assignment["execution_id"],),
            ).fetchone()[0]
        )
        now = utc_now()
        submission = self.connection.execute(
            "INSERT INTO submissions(execution_id,revision,commit_sha,summary,state,created_at,updated_at)VALUES(?,?,?,?, 'checking',?,?)",
            (assignment["execution_id"], revision, commit_sha, summary, now, now),
        ).lastrowid
        checktree = self.config.root / ".worktrees/check" / str(submission)
        add_detached(self.config.project.path, commit_sha, checktree)
        for position, argv in enumerate(self.config.project.verify, 1):
            self.connection.execute(
                "INSERT INTO checks(submission_id,scope,target_sha,position,command,worktree_path,state,created_at,updated_at)VALUES(?,'submission',?,?,?,?, 'queued',?,?)",
                (submission, commit_sha, position, json.dumps(argv), str(checktree), now, now),
            )
        self.connection.execute(
            "UPDATE work_items SET status='verifying',version=version+1,updated_at=? WHERE id=?", (now, item_id)
        )
        return {"submission_id": submission, "commit_sha": commit_sha, "version": expected_version + 1}

    def recover_running_checks(self) -> int:
        """Record interrupted evidence, then requeue checks owned by a prior daemon."""
        rows = list(
            self.connection.execute(
                "SELECT id,pid,stdout_tail,stderr_tail,stdout_truncated,stderr_truncated "
                "FROM checks WHERE state='running'"
            )
        )
        if not rows:
            return 0
        marker = "verification interrupted after Agents restart"
        recovered = 0
        for row in rows:
            # A persisted PID is not an ownership proof after a daemon restart:
            # it may have been reused. Never signal an unfenced process.

            stdout_ring = _TailRing()
            stdout_ring.append(str(row["stdout_tail"] or "").encode())
            stdout_ring.truncated = stdout_ring.truncated or bool(row["stdout_truncated"])
            stderr_ring = _TailRing()
            stderr_ring.append(str(row["stderr_tail"] or "").encode())
            stderr_ring.truncated = stderr_ring.truncated or bool(row["stderr_truncated"])
            if marker not in stderr_ring.text():
                if stderr_ring.text():
                    stderr_ring.append(b"\n")
                stderr_ring.append(marker.encode())
            now = utc_now()
            interrupted = self.connection.execute(
                "UPDATE checks SET state='interrupted',stdout_tail=?,stderr_tail=?,"
                "stdout_truncated=?,stderr_truncated=?,updated_at=? "
                "WHERE id=? AND state='running'",
                (
                    stdout_ring.text(),
                    stderr_ring.text(),
                    int(stdout_ring.truncated),
                    int(stderr_ring.truncated),
                    now,
                    row["id"],
                ),
            )
            if interrupted.rowcount != 1:
                continue
            # Keep the interrupted evidence durable before making the work eligible again.
            self.connection.commit()
            self.connection.execute(
                "UPDATE checks SET state='queued',pid=NULL,process_started_at=NULL,"
                "exit_code=NULL,duration_ms=NULL,updated_at=? "
                "WHERE id=? AND state='interrupted'",
                (utc_now(), row["id"]),
            )
            recovered += 1
        return recovered

    async def run_next_check(self) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM checks WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        argv = json.loads(str(row["command"]))
        env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "SHELL", "TERM")
            if key in os.environ
        }
        env["CI"] = "1"
        started = time.monotonic()
        started_at = utc_now()
        claimed = self.connection.execute(
            "UPDATE checks SET state='running',pid=NULL,process_started_at=?,updated_at=? "
            "WHERE id=? AND state='queued'",
            (started_at, started_at, row["id"]),
        )
        if claimed.rowcount != 1:
            return None
        self.connection.commit()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=row["worktree_path"],
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self.connection.execute(
                "UPDATE checks SET state='failed',stderr_tail=?,updated_at=? WHERE id=? AND state='running'",
                (f"verification launch failed: {exc}", utc_now(), row["id"]),
            )
            self.connection.commit()
            return {"id": row["id"], "state": "failed"}
        try:
            persisted = self.connection.execute(
                "UPDATE checks SET pid=?,updated_at=? WHERE id=? AND state='running' AND pid IS NULL",
                (process.pid, utc_now(), row["id"]),
            )
            if persisted.rowcount != 1:
                raise RuntimeError("verification process ownership fence changed")
            self.connection.commit()
        except BaseException:
            await _terminate_process_group(process)
            raise
        stdout_ring = _TailRing()
        stderr_ring = _TailRing()
        stdout_reader = asyncio.create_task(_drain_pipe(process.stdout, stdout_ring))
        stderr_reader = asyncio.create_task(_drain_pipe(process.stderr, stderr_ring))
        failure: str | None = None
        try:
            await asyncio.wait_for(process.wait(), CHECK_TIMEOUT_SECONDS)
        except TimeoutError:
            failure = "verification timed out"
            try:
                await _terminate_process_group(process)
            except Exception as exc:
                failure = f"{failure}; termination failed: {exc}"
        except Exception as exc:
            failure = f"verification wait failed: {exc}"
            try:
                await _terminate_process_group(process)
            except Exception as termination_exc:
                failure = f"{failure}; termination failed: {termination_exc}"

        try:
            results = await asyncio.wait_for(
                asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True),
                PROCESS_TERM_GRACE_SECONDS,
            )
        except TimeoutError:
            await _terminate_process_group(process)
            stdout_reader.cancel()
            stderr_reader.cancel()
            await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
            results = []
            detail = "verification output pipes remained open after process exit"
            failure = f"{failure}; {detail}" if failure else detail
        reader_failures = [
            result for result in results if not isinstance(result, _TailRing) and isinstance(result, BaseException)
        ]
        if reader_failures:
            detail = f"verification output reader failed: {reader_failures[0]}"
            failure = f"{failure}; {detail}" if failure else detail
        if failure:
            if stderr_ring.text():
                stderr_ring.append(b"\n")
            stderr_ring.append(failure.encode())
        returncode = process.returncode
        state = "passed" if failure is None and returncode == 0 else "failed"
        duration = int((time.monotonic() - started) * 1000)
        self.connection.execute(
            "UPDATE checks SET state=?,exit_code=?,duration_ms=?,stdout_tail=?,stderr_tail=?,"
            "stdout_truncated=?,stderr_truncated=?,updated_at=? WHERE id=?",
            (
                state,
                returncode,
                duration,
                stdout_ring.text(),
                stderr_ring.text(),
                int(stdout_ring.truncated),
                int(stderr_ring.truncated),
                utc_now(),
                row["id"],
            ),
        )
        return {"id": row["id"], "state": state}

    def _prepare_review_worktree(self, commit_sha: str, reviewtree: Path) -> None:
        if reviewtree.exists() and any(reviewtree.iterdir()):
            remove_recorded_worktree(
                self.config.project.path,
                reviewtree,
                commit_sha,
                allow_dirty=True,
            )
        add_detached(self.config.project.path, commit_sha, reviewtree)

    def _assign_review(self, submission: sqlite3.Row, submission_id: int, gate: str, now: str) -> bool:
        self.connection.execute("SAVEPOINT assign_review")
        reviewtree: Path | None = None
        try:
            if gate == "coordination":
                if not self._elder_available():
                    self.connection.execute("RELEASE SAVEPOINT assign_review")
                    return False
                actor = self.connection.execute(
                    "SELECT slug FROM actors WHERE slug='elder' AND kind='agent' AND persistent=1"
                ).fetchone()
                persistent = self._persistent_terminal("elder") if actor is not None else None
                if actor is None or persistent is None:
                    self.connection.execute("RELEASE SAVEPOINT assign_review")
                    return False
                reviewtree = self.config.root / ".worktrees/review" / f"{submission_id}-{gate}"
                self._prepare_review_worktree(str(submission["commit_sha"]), reviewtree)

                worktree_path = reviewtree
                terminal_run_id = int(persistent["id"])
            else:
                actor = self.connection.execute(
                    "SELECT a.* FROM actors a WHERE a.kind='agent' AND a.specialty=? "
                    "AND (SELECT COUNT(*) FROM actor_leases l "
                    "WHERE l.actor_slug=a.slug AND l.released_at IS NULL) < a.capacity "
                    "ORDER BY a.slug LIMIT 1",
                    (gate,),
                ).fetchone()
                if actor is None:
                    self.connection.execute("RELEASE SAVEPOINT assign_review")
                    return False
                reviewtree = self.config.root / ".worktrees/review" / f"{submission_id}-{gate}"
                self._prepare_review_worktree(str(submission["commit_sha"]), reviewtree)

                worktree_path = reviewtree
                run = reserve_terminal(
                    self.connection,
                    self.config,
                    actor=str(actor["slug"]),
                    purpose_kind="review",
                    purpose_id=f"{submission_id}-{gate}",
                    working_directory=reviewtree,
                )
                terminal_run_id = int(run["id"])
            self.connection.execute(
                "INSERT INTO reviews(submission_id,gate,actor_slug,terminal_run_id,worktree_path,verdict,created_at,updated_at)"
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (submission_id, gate, actor[0], terminal_run_id, str(worktree_path), now, now),
            )
            conversation = self.connection.execute(
                "SELECT id FROM conversations WHERE address=?",
                (f"work:{submission['work_id']}",),
            ).fetchone()
            if conversation is not None:
                message = self.connection.execute(
                    "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
                    "VALUES(?,'system',?,'normal',?)",
                    (conversation["id"], f"{gate.upper()} review assigned to @{actor[0]}.", now),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
                    "VALUES(?,?,?,'pending',0,?)",
                    (message.lastrowid, actor[0], terminal_run_id, now),
                )
            self.connection.execute("RELEASE SAVEPOINT assign_review")
            return True
        except BaseException:
            self.connection.execute("ROLLBACK TO SAVEPOINT assign_review")
            self.connection.execute("RELEASE SAVEPOINT assign_review")
            if reviewtree is not None and reviewtree.exists():
                remove_recorded_worktree(
                    self.config.project.path,
                    reviewtree,
                    str(submission["commit_sha"]),
                    allow_dirty=False,
                )
            raise

    def advance_submission(self, submission_id: int) -> dict[str, Any]:
        submission = self.connection.execute(
            "SELECT s.*,e.work_id FROM submissions s JOIN executions e ON e.id=s.execution_id WHERE s.id=?",
            (submission_id,),
        ).fetchone()
        if submission is None:
            raise DomainError("not_found", "submission does not exist")
        checks = list(self.connection.execute("SELECT state FROM checks WHERE submission_id=?", (submission_id,)))
        if any(row[0] in {"queued", "running"} for row in checks):
            return {"state": "checking"}
        if any(row[0] != "passed" for row in checks):
            self.connection.execute(
                "UPDATE submissions SET state='superseded',updated_at=? WHERE id=?", (utc_now(), submission_id)
            )
            self.connection.execute(
                "UPDATE work_items SET status='in_progress',version=version+1,updated_at=? WHERE id=?",
                (utc_now(), submission["work_id"]),
            )
            return {"state": "failed"}
        gates = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT gate FROM review_requirements WHERE work_id=? ORDER BY gate", (submission["work_id"],)
            )
        ]
        now = utc_now()
        for gate in gates:
            latest = self.connection.execute(
                "SELECT verdict FROM reviews WHERE submission_id=? AND gate=? ORDER BY id DESC LIMIT 1",
                (submission_id, gate),
            ).fetchone()
            if latest is None or latest["verdict"] == "superseded":
                self._assign_review(submission, submission_id, gate, now)

        self.connection.execute(
            "UPDATE submissions SET state='reviewing',updated_at=? WHERE id=?", (now, submission_id)
        )
        return {"state": "reviewing"}

    def submit_review(
        self,
        actor: str,
        item_id: str,
        submission_id: int,
        expected_version: int,
        gate: str,
        verdict: str,
        body: str,
        terminal_run_id: int,
    ) -> dict[str, Any]:
        validate_text(body, "review body")
        work = Store(self.connection).get_work(item_id)
        self._version(work, expected_version)
        review = self.connection.execute(
            "SELECT r.*,s.commit_sha,e.work_id FROM reviews r JOIN submissions s ON s.id=r.submission_id "
            "JOIN executions e ON e.id=s.execution_id WHERE r.submission_id=? AND r.gate=? AND r.verdict='pending' "
            "ORDER BY r.id DESC LIMIT 1",
            (submission_id, gate),
        ).fetchone()
        if (
            review is None
            or review["actor_slug"] != actor
            or review["terminal_run_id"] != terminal_run_id
            or review["work_id"] != item_id
            or review["verdict"] != "pending"
        ):
            raise DomainError("stale_generation", "review assignment changed")
        path = Path(str(review["worktree_path"]))
        if head_sha(path) != review["commit_sha"] or not is_clean(path):
            raise DomainError("invalid_review", "review worktree changed")
        if verdict not in {"pass", "changes_requested"}:
            raise DomainError("validation_failed", "invalid review verdict")
        self.connection.execute(
            "UPDATE reviews SET verdict=?,body=?,updated_at=? WHERE id=?", (verdict, body, utc_now(), review["id"])
        )
        if gate != "coordination":
            self._end_terminal(terminal_run_id)
        if verdict == "changes_requested":
            self.connection.execute(
                "UPDATE work_items SET status='in_progress',version=version+1,updated_at=? WHERE id=?",
                (utc_now(), item_id),
            )
            return {"state": "changes_requested", "version": expected_version + 1}
        blocked_gate = self.connection.execute(
            "SELECT 1 FROM review_requirements rr "
            "WHERE rr.work_id=? AND COALESCE(("
            "SELECT r.verdict FROM reviews r "
            "WHERE r.submission_id=? AND r.gate=rr.gate ORDER BY r.id DESC LIMIT 1"
            "),'') <> 'pass' LIMIT 1",
            (review["work_id"], submission_id),
        ).fetchone()
        if blocked_gate is None:
            self.connection.execute(
                "UPDATE submissions SET state='awaiting_approval',updated_at=? WHERE id=?", (utc_now(), submission_id)
            )
            self.connection.execute(
                "INSERT INTO approvals(submission_id,state,created_at,updated_at)VALUES(?,'pending',?,?)",
                (submission_id, utc_now(), utc_now()),
            )
            self.connection.execute(
                "UPDATE work_items SET status='awaiting_approval',version=version+1,updated_at=? WHERE id=?",
                (utc_now(), item_id),
            )
            return {"state": "awaiting_approval", "version": expected_version + 1}
        return {"state": "reviewing", "version": expected_version}

    def decide_approval(self, item_id: str, expected_version: int, accept: bool, feedback: str = "") -> dict[str, Any]:
        work = Store(self.connection).get_work(item_id)
        self._version(work, expected_version)
        if work["status"] != "awaiting_approval":
            raise DomainError("invalid_state", "work is not awaiting approval")
        row = self.connection.execute(
            "SELECT a.*,s.id submission_id,s.commit_sha,e.branch,e.worktree_path,ass.terminal_run_id FROM approvals a JOIN submissions s ON s.id=a.submission_id JOIN executions e ON e.id=s.execution_id JOIN assignments ass ON ass.execution_id=e.id AND ass.state='open' WHERE e.work_id=? AND a.state='pending'",
            (item_id,),
        ).fetchone()
        if row is None:
            raise DomainError("stale_generation", "approval changed")
        if (
            branch_sha(self.config.project.path, self.config.project.default_branch)
            != self.connection.execute(
                "SELECT base_sha FROM executions WHERE work_id=? AND state='active'", (item_id,)
            ).fetchone()[0]
            or head_sha(Path(row["worktree_path"])) != row["commit_sha"]
            or not is_clean(Path(row["worktree_path"]))
        ):
            raise DomainError("base_changed", "approval fencing failed")
        now = utc_now()
        if not accept:
            self.connection.execute(
                "UPDATE approvals SET state='rejected',feedback=?,decided_by='human',updated_at=? WHERE id=?",
                (feedback, now, row["id"]),
            )
            self.connection.execute(
                "UPDATE work_items SET status='in_progress',version=version+1,updated_at=? WHERE id=?", (now, item_id)
            )
            return {"state": "in_progress", "version": expected_version + 1}
        self.connection.execute(
            "UPDATE approvals SET state='accepted',decided_by='human',updated_at=? WHERE id=?", (now, row["id"])
        )
        self.connection.execute(
            "UPDATE submissions SET state='accepted',updated_at=? WHERE id=?", (now, row["submission_id"])
        )
        self.connection.execute(
            "UPDATE work_items SET status='accepted',accepted_submission_id=?,version=version+1,updated_at=? WHERE id=?",
            (row["submission_id"], now, item_id),
        )
        self.connection.execute(
            "UPDATE assignments SET state='closed',reason='accepted',updated_at=? WHERE work_id=? AND state='open'",
            (now, item_id),
        )
        self._end_terminal(int(row["terminal_run_id"]))
        return {
            "state": "accepted",
            "version": expected_version + 1,
            "branch": row["branch"],
            "commit_sha": row["commit_sha"],
        }

    def queue_integration(self, item_id: str) -> bool:
        work = Store(self.connection).get_work(item_id)
        if work["status"] != "accepted":
            return False
        submission = self.connection.execute(
            "SELECT * FROM submissions WHERE id=?", (work["accepted_submission_id"],)
        ).fetchone()
        default = branch_sha(self.config.project.path, self.config.project.default_branch)
        if not is_ancestor(self.config.project.path, str(submission["commit_sha"]), default):
            return False
        self.connection.execute(
            "DELETE FROM checks WHERE submission_id=? AND scope='integration' AND target_sha<>? AND state='queued'",
            (submission["id"], default),
        )
        existing = self.connection.execute(
            "SELECT 1 FROM checks WHERE submission_id=? AND scope='integration' AND target_sha=?",
            (submission["id"], default),
        ).fetchone()
        if existing:
            return True
        tree = self.config.root / ".worktrees/check" / f"integration-{submission['id']}-{default[:12]}"
        add_detached(self.config.project.path, default, tree)
        now = utc_now()
        for position, argv in enumerate(self.config.project.verify, 1):
            self.connection.execute(
                "INSERT INTO checks(submission_id,scope,target_sha,position,command,worktree_path,state,created_at,updated_at)VALUES(?,'integration',?,?,?,?, 'queued',?,?)",
                (submission["id"], default, position, json.dumps(argv), str(tree), now, now),
            )
        return True

    def finish_integration(self, item_id: str) -> dict[str, Any]:
        work = Store(self.connection).get_work(item_id)
        submission_id = work["accepted_submission_id"]
        default = branch_sha(self.config.project.path, self.config.project.default_branch)
        checks = list(
            self.connection.execute(
                "SELECT * FROM checks WHERE submission_id=? AND scope='integration' AND target_sha=?",
                (submission_id, default),
            )
        )
        if not checks or any(row["state"] in {"queued", "running"} for row in checks):
            return {"state": "checking"}
        if any(row["state"] != "passed" for row in checks):
            now = utc_now()
            blocker = self.connection.execute(
                "SELECT id FROM blockers WHERE target_kind='work' AND target_id=? AND state IN ('open','escalated')",
                (item_id,),
            ).fetchone()
            if blocker is None:
                blocker = self.connection.execute(
                    "INSERT INTO blockers(work_id,target_kind,target_id,kind,reason,requested_role,"
                    "actor_slug,resume_state,state,created_at,updated_at) "
                    "VALUES(?,'work',?,'integration_failed','integration verification failed',"
                    "'human','system','accepted','open',?,?) RETURNING id",
                    (item_id, item_id, now, now),
                ).fetchone()
            self.connection.execute(
                "UPDATE work_items SET status='blocked',blocked_from='accepted',version=version+1,"
                "updated_at=? WHERE id=? AND status='accepted'",
                (now, item_id),
            )
            return {"state": "blocked", "blocker_id": int(blocker["id"]), "reason": "integration_failed"}
        self.connection.execute(
            "UPDATE work_items SET status='delivered',integration_sha=?,version=version+1,updated_at=? WHERE id=? AND status='accepted'",
            (default, utc_now(), item_id),
        )
        return {"state": "delivered", "integration_sha": default}

    def _restart_terminal(self, run_id: int, reason: str) -> None:
        """Fence a stale terminal and prove its mapped backend run is gone."""
        row = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),"
            "state=CASE WHEN state IN ('reserved','creating','live','retained') THEN 'ending' ELSE state END,"
            "error=COALESCE(error,?),updated_at=? WHERE id=?",
            (now, reason, now, run_id),
        )
        handle: RunHandle | None = None
        if row["backend_run_id"] and row["backend_terminal_id"]:
            handle = RunHandle(
                str(row["execution_name"]),
                str(row["backend_run_id"]),
                str(row["backend_terminal_id"]),
            )
        elif str(row["execution_name"]) not in {"", "reserved"}:
            try:
                snapshot = self.backend.find_run(str(row["execution_name"]))
            except (ExecutionUnavailable, ExecutionBusy, ExecutionTimeout) as exc:
                raise DomainError("execution_unavailable", f"cannot verify old terminal: {exc}") from exc
            except ExecutionError as exc:
                raise DomainError("execution_cleanup_failed", f"cannot verify old terminal: {exc}") from exc
            if snapshot is not None:
                if snapshot.cwd != Path(str(row["working_directory"])).resolve():
                    raise DomainError("execution_identity_changed", "old backend run cwd changed")
                handle = snapshot.handle
        if handle is not None:
            try:
                self.backend.delete_run(handle)
            except ExecutionNotFound:
                pass
            except (ExecutionUnavailable, ExecutionBusy, ExecutionTimeout) as exc:
                raise DomainError("execution_unavailable", f"cannot delete old terminal: {exc}") from exc
            except ExecutionError as exc:
                raise DomainError("execution_cleanup_failed", f"cannot delete old terminal: {exc}") from exc
        self.connection.execute(
            "UPDATE launch_attempts SET state='aborted',error=?,updated_at=? "
            "WHERE terminal_run_id=? AND state IN ('reserved','posting','uncertain')",
            (reason, now, run_id),
        )
        self.connection.execute(
            "UPDATE assignments SET state='closed',reason=?,updated_at=? WHERE terminal_run_id=? AND state='open'",
            (reason, now, run_id),
        )
        self.connection.execute(
            "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
            (now, run_id),
        )

    def resolve_blocker(self, actor: str, blocker_id: int, resolution: str, action: str) -> dict[str, Any]:
        if actor not in {"human", "elder"}:
            raise DomainError("unauthorized", "actor cannot resolve blockers")
        blocker = self.connection.execute(
            "SELECT * FROM blockers WHERE id=? AND state IN('open','escalated')", (blocker_id,)
        ).fetchone()
        if blocker is None:
            raise DomainError("not_found", "open blocker does not exist")
        if action not in {"resume", "restart", "escalate"}:
            raise DomainError("validation_failed", "invalid blocker action")
        if action == "restart" and blocker["terminal_run_id"] is not None:
            self._restart_terminal(int(blocker["terminal_run_id"]), resolution)
        state = "escalated" if action == "escalate" else "resolved"
        now = utc_now()
        if action == "restart" and blocker["work_id"]:
            work_id = str(blocker["work_id"])
            execution_ids = [
                int(row[0])
                for row in self.connection.execute(
                    "SELECT id FROM executions WHERE work_id=? AND state IN ('preparing','active')", (work_id,)
                )
            ]
            if execution_ids:
                marks = ",".join("?" for _ in execution_ids)
                self.connection.execute(
                    f"UPDATE submissions SET state='superseded',updated_at=? "
                    f"WHERE execution_id IN ({marks}) AND state<>'accepted'",
                    (now, *execution_ids),
                )
                self.connection.execute(
                    f"UPDATE approvals SET state='superseded',updated_at=? "
                    f"WHERE submission_id IN (SELECT id FROM submissions WHERE execution_id IN ({marks})) "
                    f"AND state='pending'",
                    (now, *execution_ids),
                )
            self.connection.execute(
                "UPDATE executions SET state='superseded',updated_at=? WHERE work_id=? AND state IN ('preparing','active')",
                (now, work_id),
            )
            self.connection.execute(
                "UPDATE assignments SET state='closed',reason=?,updated_at=? WHERE work_id=? AND state='open'",
                (resolution, now, work_id),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE purpose_kind='work' AND purpose_id=? AND released_at IS NULL",
                (now, work_id),
            )
            self.connection.execute(
                "UPDATE work_items SET status='ready',blocked_from=NULL,active_execution_id=NULL,"
                "updated_at=? WHERE id=? AND status='blocked'",
                (now, work_id),
            )
        self.connection.execute(
            "UPDATE blockers SET state=?,resolution=?,updated_at=? WHERE id=?",
            (state, resolution, now, blocker_id),
        )
        dispatch: dict[str, Any] | None = None
        if action == "restart" and blocker["work_id"]:
            dispatch = self.dispatch_next()
        result: dict[str, Any] = {"id": blocker_id, "state": state, "action": action}
        if dispatch is not None:
            result["dispatch"] = dispatch
            current = self.connection.execute(
                "SELECT version FROM work_items WHERE id=?", (blocker["work_id"],)
            ).fetchone()
            if current is not None:
                result["version"] = int(current["version"])
        elif action == "restart" and blocker["work_id"]:
            current = self.connection.execute(
                "SELECT version FROM work_items WHERE id=?", (blocker["work_id"],)
            ).fetchone()
            if current is not None:
                self.connection.execute(
                    "UPDATE work_items SET version=version+1,updated_at=? WHERE id=?",
                    (utc_now(), blocker["work_id"]),
                )
                result["version"] = int(current["version"]) + 1
        elif action == "resume" and blocker["work_id"]:
            self.connection.execute(
                "UPDATE work_items SET status=COALESCE(?,blocked_from),blocked_from=NULL,version=version+1,updated_at=? WHERE id=?",
                (blocker["resume_state"], utc_now(), blocker["work_id"]),
            )
        return result

    def _end_terminal(self, run_id: int) -> None:
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),state='retained',updated_at=? WHERE id=?",
            (now, now, run_id),
        )
        self.connection.execute(
            "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL", (now, run_id)
        )

    @staticmethod
    def _version(row: sqlite3.Row, expected: int) -> None:
        if int(row["version"]) != expected:
            raise DomainError("stale_version", "work item version changed", dict(row))
