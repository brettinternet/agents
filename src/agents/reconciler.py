from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from .auth import derive_agent_token, read_private_secret, token_digest
from .cao_client import CaoClient, CaoNotFound, CaoUnavailable
from .config import AgentsConfig
from .db import canonical_json, utc_now
from .git_worktree import GitError, head_sha, remove_recorded_worktree
from .profiles import (
    PROVIDER_CAPABILITIES,
    ProfileError,
    install_profile,
    materialize_profile,
    mcp_name,
    profile_name,
    purpose_tools,
    remove_profile,
    session_name,
    validate_manifest_artifact,
)

_RETRY = (1, 5, 30, 120, 300)

_COMPLETION_STATUSES = {"completed", "complete", "done", "exited", "stopped"}


def _normalize_status(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    return "completed" if normalized in _COMPLETION_STATUSES else normalized


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def reserve_terminal(
    connection: sqlite3.Connection,
    config: AgentsConfig,
    *,
    actor: str,
    purpose_kind: str,
    purpose_id: str,
    working_directory: Path,
    budget_exempt: bool = False,
) -> dict[str, Any]:
    connection.execute("SAVEPOINT reserve_terminal")
    try:
        run = _reserve_terminal_unchecked(
            connection,
            config,
            actor=actor,
            purpose_kind=purpose_kind,
            purpose_id=purpose_id,
            working_directory=working_directory,
            budget_exempt=budget_exempt,
        )
        connection.execute("RELEASE SAVEPOINT reserve_terminal")
        return run
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT reserve_terminal")
        connection.execute("RELEASE SAVEPOINT reserve_terminal")
        raise


def _reserve_terminal_unchecked(
    connection: sqlite3.Connection,
    config: AgentsConfig,
    *,
    actor: str,
    purpose_kind: str,
    purpose_id: str,
    working_directory: Path,
    budget_exempt: bool = False,
) -> dict[str, Any]:
    project = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
    actor_row = connection.execute("SELECT * FROM actors WHERE slug=?", (actor,)).fetchone()
    if project is None or actor_row is None:
        raise RuntimeError("Agents is not initialized")
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    used = connection.execute(
        "SELECT COUNT(*) FROM launch_attempts WHERE budget_exempt=0 AND ((counted=1 AND updated_at>=?) OR state='reserved')",
        (since,),
    ).fetchone()[0]
    if not budget_exempt and used >= config.runtime.launch_budget_per_hour:
        raise RuntimeError("rolling launch budget exhausted")
    generation = int(
        connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM terminal_runs WHERE actor_slug=? AND purpose_kind=? AND purpose_id=?",
            (actor, purpose_kind, purpose_id),
        ).fetchone()[0]
    )
    now = utc_now()
    provider = config.cao.provider_id
    model = secrets.choice(config.models_for(actor))
    cursor = connection.execute(
        "INSERT INTO terminal_runs(session_name,profile_name,mcp_name,profile_sha256,provider,model,reasoning_effort,generation,actor_slug,purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,output_tail,launch_count,created_at,updated_at)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved','',0,?,?)",
        (
            "reserved",
            "reserved",
            "reserved",
            "",
            provider,
            model.id,
            model.effort,
            generation,
            actor,
            purpose_kind,
            purpose_id,
            str(working_directory.resolve()),
            "",
            "reserved",
            now,
            now,
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not allocate terminal run")
    run_id = cursor.lastrowid
    instance = str(project[0])
    profile = profile_name(instance, run_id, generation)
    mcp = mcp_name(instance, run_id, generation)
    session_purpose_id = purpose_id
    if purpose_kind == "work":
        work = connection.execute("SELECT seq,active_execution_id FROM work_items WHERE id=?", (purpose_id,)).fetchone()
        execution = None
        if work is not None and work["active_execution_id"] is not None:
            execution = connection.execute(
                "SELECT number FROM executions WHERE id=?", (work["active_execution_id"],)
            ).fetchone()
        if execution is None:
            execution = connection.execute(
                "SELECT number FROM executions WHERE work_id=? ORDER BY id DESC LIMIT 1", (purpose_id,)
            ).fetchone()
        if work is not None and execution is not None:
            session_purpose_id = f"{int(work['seq'])}-{int(execution['number'])}"
    session = session_name(instance, purpose_kind, session_purpose_id, actor, generation)
    key = bytes.fromhex(read_private_secret(config.state_dir / "agent-auth-key"))
    token = derive_agent_token(key, instance, run_id, generation)
    connection.execute(
        "UPDATE terminal_runs SET session_name=?,profile_name=?,mcp_name=?,token_digest=?,updated_at=? WHERE id=?",
        (session, profile, mcp, token_digest(token), now, run_id),
    )
    connection.execute(
        "INSERT INTO actor_leases(actor_slug,purpose_kind,purpose_id,terminal_run_id,acquired_at)VALUES(?,?,?,?,?)",
        (actor, purpose_kind, purpose_id, run_id, now),
    )
    connection.execute(
        "INSERT INTO launch_attempts(terminal_run_id,budget_exempt,counted,state,created_at,updated_at)"
        "VALUES(?,?,0,'reserved',?,?)",
        (run_id, int(budget_exempt), now, now),
    )
    return dict(connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone())


def bootstrap_persistent_agents(connection: sqlite3.Connection, config: AgentsConfig) -> list[int]:
    reserved: list[int] = []
    actors = connection.execute("SELECT slug FROM actors WHERE kind='agent' AND persistent=1 ORDER BY slug")
    for row in actors:
        actor = str(row["slug"])
        if connection.execute(
            "SELECT 1 FROM terminal_runs WHERE actor_slug=? AND purpose_kind='persistent' "
            "AND state IN ('reserved','creating','live','retained')",
            (actor,),
        ).fetchone():
            continue
        attempts = connection.execute(
            "SELECT COUNT(*) FROM terminal_runs WHERE actor_slug=? AND purpose_kind='persistent'",
            (actor,),
        ).fetchone()[0]
        if attempts >= 3:
            continue
        run = reserve_terminal(
            connection,
            config,
            actor=actor,
            purpose_kind="persistent",
            purpose_id=actor,
            working_directory=config.project.path,
            budget_exempt=True,
        )
        reserved.append(int(run["id"]))
    return reserved


class CaoApi(Protocol):
    def health(self) -> bool: ...
    def create_session(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_session(self, name: str) -> dict[str, Any]: ...
    def list_terminals(self, session_name: str) -> list[dict[str, Any]]: ...
    def get_terminal(self, terminal_id: str) -> dict[str, Any]: ...
    def get_output(self, terminal_id: str) -> str: ...
    def get_working_directory(self, terminal_id: str) -> str: ...
    def enqueue_wake(self, terminal_id: str, sender_id: str, message: str) -> str: ...
    def send_input(self, terminal_id: str, message: str) -> bool: ...
    def delete_session(self, name: str) -> None: ...
    def list_sessions(self) -> list[dict[str, Any]]: ...


class Reconciler:
    def __init__(self, config: AgentsConfig, connection: sqlite3.Connection, client: CaoApi | None = None) -> None:
        self.config = config
        self.connection = connection
        self.client = client or CaoClient(config.cao.api_port)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._launch_locks: dict[int, asyncio.Lock] = {}
        self._profile_lock = asyncio.Lock()
        self._recovered_checks = False

    def _spawn(self, key: str, coro: Any) -> None:
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        self._inflight[key] = task

        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if self._inflight.get(key) is done:
                self._inflight.pop(key, None)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    self._incident(
                        "reconciler_task_failed",
                        "system",
                        key,
                        str(error),
                    )

        task.add_done_callback(finished)

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._incident("reconciler_cycle_failed", "system", "global", str(exc))
                await asyncio.sleep(self.config.runtime.poll_seconds)
        finally:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _fence_stale_provider_runs(self) -> None:
        configured = self.config.cao.provider_id
        for run in list(
            self.connection.execute(
                "SELECT DISTINCT tr.* FROM terminal_runs tr LEFT JOIN launch_attempts la "
                "ON la.terminal_run_id=tr.id "
                "WHERE tr.state IN ('reserved','creating','live') OR la.state IN ('posting','uncertain')"
            )
        ):
            provider = str(run["provider"])
            if provider == configured and provider in PROVIDER_CAPABILITIES:
                continue
            self._recover_terminal(
                run,
                f"stored provider {provider!r} is not the configured supported provider {configured!r}",
                terminal_state="ending",
                blocker_kind="provider_changed",
                incident_kind="provider_changed",
            )

    async def run_once(self) -> None:
        from .delivery import Delivery
        from .schedules import Scheduler

        try:
            self._fence_stale_provider_runs()
            bootstrap_persistent_agents(self.connection, self.config)
            if not self._recovered_checks:
                Delivery(self.config, self.connection).recover_running_checks()
                self._recovered_checks = True
        except Exception as exc:
            self._incident("bootstrap_failed", "persistent", "global", str(exc))
        try:
            Scheduler(self.config, self.connection).dispatch_due()
        except Exception as exc:
            self._incident("schedule_dispatch_failed", "schedule", "global", str(exc))
        healthy = self.client.health()
        await self._advance_delivery(healthy)
        self._cleanup_expired()
        for row in self.connection.execute("SELECT id FROM terminal_runs WHERE state IN ('retained','ending')"):
            run_id = int(row["id"])
            self._spawn(f"cleanup:{run_id}", self._cleanup_terminal(run_id))
        self._spawn("unmapped", self._remove_unmapped_sessions())
        if not healthy:
            return
        for row in self.connection.execute(
            "SELECT tr.id FROM terminal_runs tr JOIN launch_attempts la ON la.terminal_run_id=tr.id "
            "WHERE la.state='reserved' AND tr.state='reserved'"
        ):
            run_id = int(row[0])
            self._spawn(f"launch:{run_id}", self._launch(run_id))
        for row in self.connection.execute(
            "SELECT tr.id FROM terminal_runs tr JOIN launch_attempts la ON la.terminal_run_id=tr.id "
            "WHERE la.state IN ('posting','uncertain') AND tr.state='creating'"
        ):
            run_id = int(row[0])
            launch = self._inflight.get(f"launch:{run_id}")
            if launch is not None and not launch.done():
                continue
            self._spawn(f"adopt:{run_id}", self._adopt(run_id))
        for row in self.connection.execute("SELECT id FROM terminal_runs WHERE state='live'"):
            run_id = int(row[0])
            self._spawn(f"poll:{run_id}", self._poll(run_id))
        for row in self.connection.execute("SELECT id FROM terminal_inputs WHERE state='pending'"):
            input_id = int(row[0])
            self._spawn(f"input:{input_id}", self._send_input(input_id))
        for row in self.connection.execute(
            "SELECT id FROM deliveries WHERE state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            "ORDER BY id LIMIT 20",
            (utc_now(),),
        ):
            delivery_id = int(row[0])
            self._spawn(f"wake:{delivery_id}", self._wake(delivery_id))

    async def _remove_unmapped_sessions(self) -> None:
        list_sessions = getattr(self.client, "list_sessions", None)
        delete_session = getattr(self.client, "delete_session", None)
        if not callable(list_sessions) or not callable(delete_session):
            return
        try:
            sessions = cast(list[dict[str, Any]], await asyncio.to_thread(list_sessions))
        except CaoUnavailable:
            return
        project = self.connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        if project is None:
            return
        prefix = f"cao-agents-{project['instance_id']}-"
        mapped = {
            str(row["session_name"])
            for row in self.connection.execute(
                "SELECT session_name FROM terminal_runs "
                "WHERE state IN ('reserved','creating','live') AND token_revoked_at IS NULL"
            )
        }
        for session in sessions:
            name = session.get("name", session.get("session_name"))
            if isinstance(name, str) and name.startswith(prefix) and name not in mapped:
                try:
                    async with self._profile_lock:
                        await asyncio.to_thread(delete_session, name)
                        await asyncio.to_thread(self.client.get_session, name)
                except CaoNotFound:
                    continue
                except CaoUnavailable as exc:
                    self._incident("terminal_cleanup_failed", "terminal", name, str(exc))
                    continue
                self._incident(
                    "terminal_cleanup_failed",
                    "terminal",
                    name,
                    "unmapped CAO session still exists after deletion",
                )

    async def _advance_delivery(self, provider_healthy: bool) -> None:
        from .delivery import Delivery

        delivery = Delivery(self.config, self.connection)
        try:
            if any(self.connection.execute("SELECT 1 FROM checks WHERE state='queued'").fetchone() for _ in (0,)):
                self._spawn("check", delivery.run_next_check())
            for row in list(
                self.connection.execute(
                    "SELECT s.id FROM submissions s JOIN executions e ON e.id=s.execution_id "
                    "WHERE (s.state='checking' AND NOT EXISTS("
                    "SELECT 1 FROM checks c WHERE c.submission_id=s.id AND c.state IN ('queued','running')))"
                    " OR (s.state='reviewing' AND EXISTS("
                    "SELECT 1 FROM review_requirements rr "
                    "WHERE rr.work_id=e.work_id AND COALESCE(("
                    "SELECT r.verdict FROM reviews r WHERE r.submission_id=s.id AND r.gate=rr.gate "
                    "ORDER BY r.id DESC LIMIT 1),'') NOT IN ('pending','pass','changes_requested')))"
                )
            ):
                delivery.advance_submission(int(row["id"]))
            for row in list(self.connection.execute("SELECT id FROM work_items WHERE status='accepted'")):
                item_id = str(row["id"])
                delivery.queue_integration(item_id)
                pending = self.connection.execute(
                    "SELECT 1 FROM checks c JOIN submissions s ON s.id=c.submission_id "
                    "JOIN work_items w ON w.accepted_submission_id=s.id "
                    "WHERE w.id=? AND c.scope='integration' AND c.state IN ('queued','running')",
                    (item_id,),
                ).fetchone()
                if pending is None:
                    integration = self.connection.execute(
                        "SELECT 1 FROM checks c JOIN submissions s ON s.id=c.submission_id "
                        "JOIN work_items w ON w.accepted_submission_id=s.id "
                        "WHERE w.id=? AND c.scope='integration'",
                        (item_id,),
                    ).fetchone()
                    if integration is not None:
                        delivery.finish_integration(item_id)
            if provider_healthy:
                for _ in range(self.config.runtime.max_consultations):
                    consultation = delivery.dispatch_consultation_next()
                    if consultation is None:
                        break
                    self._spawn(
                        f"launch:{consultation['terminal_run_id']}",
                        self._launch(int(consultation["terminal_run_id"])),
                    )
                dispatch = delivery.dispatch_next()
                if dispatch is not None:
                    self._spawn(
                        f"launch:{dispatch['terminal_run_id']}",
                        self._launch(int(dispatch["terminal_run_id"])),
                    )
        except Exception as exc:
            self._incident("delivery_reconciliation_failed", "delivery", "global", str(exc))

    async def _launch(self, run_id: int) -> None:
        lock = self._launch_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            await self._launch_impl(run_id)

    async def _launch_impl(self, run_id: int) -> None:
        run = self.connection.execute(
            "SELECT tr.*,a.profile_template,a.specialty,p.instance_id FROM terminal_runs tr "
            "JOIN actors a ON a.slug=tr.actor_slug JOIN project p ON p.id=1 WHERE tr.id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return
        posted = False
        materialized = None
        artifacts: list[dict[str, Any]] = []
        terminal_id: str | None = None
        cancelled = False
        try:
            key = bytes.fromhex(read_private_secret(self.config.state_dir / "agent-auth-key"))
            token = derive_agent_token(key, str(run["instance_id"]), run_id, int(run["generation"]))
            async with self._profile_lock:
                if not self._launch_fence(run_id, {"reserved"}, {"reserved"}):
                    cancelled = True
                else:
                    materialized = await asyncio.to_thread(
                        materialize_profile,
                        self.config.root,
                        self.config.state_dir,
                        template=str(run["profile_template"]),
                        instance=str(run["instance_id"]),
                        run_id=run_id,
                        generation=int(run["generation"]),
                        provider=str(run["provider"]),
                        purpose_kind=str(run["purpose_kind"]),
                        specialty=run["specialty"],
                        token=token,
                        api_url=f"http://127.0.0.1:{self.config.web.port}",
                        reasoning_effort=str(run["reasoning_effort"]),
                    )
                if not cancelled:
                    assert materialized is not None
                    now = utc_now()
                    self.connection.execute("BEGIN IMMEDIATE")
                    try:
                        fence = self.connection.execute(
                            "SELECT tr.state,la.state,tr.token_revoked_at FROM terminal_runs tr "
                            "JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
                            (run_id,),
                        ).fetchone()
                        if fence is None or tuple(fence) != ("reserved", "reserved", None):
                            self.connection.rollback()
                            cancelled = True
                        else:
                            self.connection.execute(
                                "UPDATE terminal_runs SET profile_sha256=?,profile_state='staged',updated_at=? "
                                "WHERE id=? AND state='reserved' AND token_revoked_at IS NULL",
                                (materialized.sha256, now, run_id),
                            )
                            self.connection.commit()
                    except BaseException:
                        self.connection.rollback()
                        raise
                if not cancelled:
                    assert materialized is not None
                    if not self._launch_fence(run_id, {"reserved"}, {"reserved"}):
                        cancelled = True
                    else:
                        artifacts = await asyncio.to_thread(
                            install_profile,
                            self.config.root / ".tools/bin/cao",
                            self.config.cao_home,
                            materialized,
                            str(run["provider"]),
                            self.config.state_dir / "profiles.lock",
                        )
                if not cancelled:
                    assert materialized is not None
                    now = utc_now()
                    self.connection.execute("BEGIN IMMEDIATE")
                    try:
                        fence = self.connection.execute(
                            "SELECT tr.state,la.state,tr.token_revoked_at FROM terminal_runs tr "
                            "JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
                            (run_id,),
                        ).fetchone()
                        if fence is None or tuple(fence) != ("reserved", "reserved", None):
                            self.connection.rollback()
                            cancelled = True
                        else:
                            self.connection.execute(
                                "UPDATE terminal_runs SET profile_sha256=?,profile_state='installed',"
                                "state='creating',updated_at=? WHERE id=? AND state='reserved' "
                                "AND token_revoked_at IS NULL",
                                (materialized.sha256, now, run_id),
                            )
                            self.connection.execute(
                                "UPDATE launch_attempts SET counted=1,state='posting',updated_at=? "
                                "WHERE terminal_run_id=? AND state='reserved'",
                                (now, run_id),
                            )
                            for artifact in artifacts:
                                self.connection.execute(
                                    "INSERT OR IGNORE INTO terminal_artifacts(terminal_run_id,kind,path,"
                                    "fragment_key,expected_sha256,expected_json_redacted,secret_fields_json,"
                                    "state,created_at,updated_at)"
                                    "VALUES(?,?,?,?,?,?,?,'installed',?,?)",
                                    (
                                        run_id,
                                        str(artifact.get("kind", "config")),
                                        artifact["path"],
                                        artifact.get("fragment_key"),
                                        artifact["sha256"],
                                        artifact.get("expected_json_redacted"),
                                        artifact.get("secret_fields_json", "{}"),
                                        now,
                                        now,
                                    ),
                                )
                            self.connection.commit()
                    except BaseException:
                        self.connection.rollback()
                        raise
                if not cancelled and not self._launch_fence(run_id, {"creating"}, {"posting"}):
                    self._mark_cancelled_launch(run_id, "launch revoked before CAO POST", actual_posted=False)
                    cancelled = True
                if not cancelled:
                    posted = True
                    value = await asyncio.to_thread(
                        self.client.create_session,
                        profile=str(run["profile_name"]),
                        provider=str(run["provider"]),
                        session_name=str(run["session_name"]),
                        working_directory=str(run["working_directory"]),
                        allowed_tools=list(purpose_tools(str(run["purpose_kind"]), run["specialty"])),
                        env_vars={
                            "AGENTS_AGENT_TOKEN": token,
                            "AGENTS_API_URL": f"http://127.0.0.1:{self.config.web.port}",
                        },
                        model=str(run["model"]),
                    )
                    terminal_id = value.get("id") or value.get("terminal_id")
                    if not isinstance(terminal_id, str):
                        raise CaoUnavailable("create session response has no terminal ID")
                    self._record_terminal_id(run_id, terminal_id)
                    session = await asyncio.to_thread(self.client.get_session, str(run["session_name"]))
                    terminals = await asyncio.to_thread(self.client.list_terminals, str(run["session_name"]))
                    if not self._session_matches(run, session):
                        raise CaoUnavailable("CAO returned a mismatched session identity")
                    if (
                        len(terminals) != 1
                        or (terminals[0].get("id") or terminals[0].get("terminal_id")) != terminal_id
                        or not self._terminal_matches(run, terminals[0])
                    ):
                        raise CaoUnavailable("CAO session does not contain the expected terminal")
                    directory = await asyncio.to_thread(self.client.get_working_directory, terminal_id)
                    if Path(directory).resolve() != Path(str(run["working_directory"])).resolve():
                        raise CaoUnavailable("created terminal has the wrong working directory")
                    self._seal_runtime_artifacts(run_id, terminal_id, terminals[0])
                    now = utc_now()
                    self.connection.execute("BEGIN IMMEDIATE")
                    try:
                        fence = self.connection.execute(
                            "SELECT tr.state,la.state,tr.token_revoked_at FROM terminal_runs tr "
                            "JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
                            (run_id,),
                        ).fetchone()
                        if fence is None or tuple(fence) != ("creating", "posting", None):
                            self.connection.rollback()
                            self._mark_cancelled_launch(
                                run_id, "launch revoked after CAO POST", terminal_id=terminal_id, actual_posted=True
                            )
                            await self._delete_session_locked(str(run["session_name"]))
                            cancelled = True
                        else:
                            self.connection.execute(
                                "UPDATE terminal_runs SET terminal_id=?,state='live',launch_count=launch_count+1,"
                                "updated_at=? WHERE id=? AND state='creating' AND token_revoked_at IS NULL",
                                (terminal_id, now, run_id),
                            )
                            self.connection.execute(
                                "UPDATE launch_attempts SET state='succeeded',updated_at=? "
                                "WHERE terminal_run_id=? AND state='posting'",
                                (now, run_id),
                            )
                            self.connection.commit()
                    except BaseException:
                        if self.connection.in_transaction:
                            self.connection.rollback()
                        raise
            if cancelled:
                if materialized is not None:
                    self._discard_profile(materialized, artifacts)
                return
        except BaseException as exc:
            if not posted:
                if self._run_revoked(run_id):
                    self._mark_cancelled_launch(run_id, str(exc), actual_posted=False)
                else:
                    self._abort_prepost(run_id, str(exc))
                if materialized is not None:
                    self._discard_profile(materialized, artifacts)
                return
            self.connection.execute(
                "UPDATE launch_attempts SET state='uncertain',error=?,updated_at=? "
                "WHERE terminal_run_id=? AND state='posting'",
                (str(exc), utc_now(), run_id),
            )
            await self._adopt(run_id)

    def _launch_fence(self, run_id: int, terminal_states: set[str], attempt_states: set[str]) -> bool:
        row = self.connection.execute(
            "SELECT tr.state terminal_state,la.state launch_state,tr.token_revoked_at "
            "FROM terminal_runs tr JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
            (run_id,),
        ).fetchone()
        return (
            row is not None
            and row["terminal_state"] in terminal_states
            and row["launch_state"] in attempt_states
            and row["token_revoked_at"] is None
        )

    def _run_revoked(self, run_id: int) -> bool:
        row = self.connection.execute(
            "SELECT token_revoked_at,state FROM terminal_runs WHERE id=?", (run_id,)
        ).fetchone()
        return row is not None and (
            row["token_revoked_at"] is not None or row["state"] in {"ending", "ended", "failed"}
        )

    def _record_terminal_id(self, run_id: int, terminal_id: str) -> None:
        self.connection.execute(
            "UPDATE terminal_runs SET terminal_id=?,updated_at=? WHERE id=? AND terminal_id IS NULL",
            (terminal_id, utc_now(), run_id),
        )

    def _mark_cancelled_launch(
        self, run_id: int, reason: str, *, terminal_id: str | None = None, actual_posted: bool
    ) -> None:
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),"
                "state=CASE WHEN state IN ('reserved','creating','live','retained') THEN 'ending' ELSE state END,"
                "terminal_id=COALESCE(terminal_id,?),error=COALESCE(error,?),updated_at=? WHERE id=?",
                (now, terminal_id, reason, now, run_id),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='aborted',counted=CASE WHEN ? THEN counted ELSE 0 END,"
                "error=?,updated_at=? WHERE terminal_run_id=? AND state IN ('reserved','posting','uncertain')",
                (int(actual_posted), reason, now, run_id),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
                (now, run_id),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    async def _delete_session_locked(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.client.delete_session, name)
            await asyncio.to_thread(self.client.get_session, name)
        except CaoNotFound:
            return
        raise CaoUnavailable(f"CAO session still exists after deletion: {name}")

    async def _delete_session(self, name: str) -> None:
        async with self._profile_lock:
            await self._delete_session_locked(name)

    async def _replace_stale_session(self, run: sqlite3.Row) -> bool:
        try:
            await self._delete_session(str(run["session_name"]))
        except CaoUnavailable:
            return False
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            fence = self.connection.execute(
                "SELECT tr.state terminal_state,la.state launch_state,tr.token_revoked_at "
                "FROM terminal_runs tr JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
                (run["id"],),
            ).fetchone()
            if (
                fence is None
                or fence["terminal_state"] != "creating"
                or fence["launch_state"] not in {"posting", "uncertain"}
                or fence["token_revoked_at"] is not None
            ):
                self.connection.rollback()
                return True
            self.connection.execute(
                "UPDATE terminal_runs SET state='reserved',terminal_id=NULL,error=NULL,updated_at=? WHERE id=?",
                (now, run["id"]),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='reserved',counted=0,error=NULL,updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def _abort_prepost(self, run_id: int, reason: str) -> None:
        run = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return
        self._recover_terminal(
            run,
            reason,
            terminal_profile_state="failed",
            incident_kind="terminal_launch_failed",
            launch_attempt_state="aborted",
            launch_attempt_counted=0,
        )

    def _discard_profile(self, materialized: Any, artifacts: list[dict[str, Any]]) -> None:
        try:
            path = Path(materialized.path)
            secret_values = dict(getattr(materialized, "secret_values", None) or {})
            cao = self.config.root / ".tools/bin/cao"
            if cao.is_file():
                managed_artifacts = artifacts
                if not managed_artifacts and path.is_file() and not path.is_symlink():
                    digest = getattr(materialized, "sha256", hashlib.sha256(path.read_bytes()).hexdigest())
                    managed_artifacts = [{"path": str(path), "sha256": str(digest)}]
                remove_profile(
                    cao,
                    self.config.cao_home,
                    str(materialized.name),
                    path,
                    managed_artifacts,
                    self.config.state_dir / "profiles.lock",
                    secret_values=secret_values,
                )
                return
            if path.is_symlink():
                self._incident("profile_cleanup_mismatch", "terminal", str(materialized.name), "profile is a symlink")
            elif path.exists():
                path.unlink()
            for artifact in artifacts:
                artifact_path = Path(str(artifact["path"]))
                if not artifact_path.exists():
                    continue
                valid = validate_manifest_artifact(
                    artifact_path,
                    str(artifact["sha256"]),
                    fragment_key=artifact.get("fragment_key"),
                    expected_json_redacted=artifact.get("expected_json_redacted"),
                    secret_fields_json=artifact.get("secret_fields_json"),
                    secret_values=secret_values,
                )
                if valid and artifact.get("fragment_key") is None:
                    artifact_path.unlink()
                else:
                    self._incident(
                        "profile_cleanup_mismatch",
                        "terminal",
                        str(materialized.name),
                        f"artifact cannot be safely removed: {artifact_path}",
                    )
        except (OSError, ProfileError) as exc:
            self._incident("profile_cleanup_failed", "terminal", str(materialized.name), str(exc))

    @staticmethod
    def _session_matches(run: sqlite3.Row, session: dict[str, Any]) -> bool:
        identity = session.get("session_name", session.get("name"))
        if identity not in {None, run["session_name"]}:
            return False
        for keys, expected in (
            (("provider", "provider_id"), str(run["provider"])),
            (("profile", "profile_name", "agent_profile"), str(run["profile_name"])),
            (("working_directory", "workdir"), str(run["working_directory"])),
        ):
            values = [session[key] for key in keys if key in session and session[key] is not None]
            if values and any(str(value) != expected for value in values):
                return False
        return True

    @staticmethod
    def _terminal_matches(run: sqlite3.Row, terminal: dict[str, Any]) -> bool:
        identity = terminal.get("session_name", terminal.get("tmux_session", terminal.get("name")))
        provider = terminal.get("provider", terminal.get("provider_id"))
        profile = terminal.get("profile", terminal.get("profile_name", terminal.get("agent_profile")))
        return identity == run["session_name"] and provider == run["provider"] and profile == run["profile_name"]

    def _runtime_paths(self, run_id: int, terminal_id: str) -> tuple[tuple[str, Path], ...]:
        provider = self.connection.execute("SELECT provider FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
        if provider is None or str(provider["provider"]) != "claude_code":
            return ()
        if not terminal_id or Path(terminal_id).name != terminal_id or terminal_id in {".", ".."}:
            raise CaoUnavailable("CAO terminal ID is unsafe for runtime artifact paths")
        base = (self.config.cao_home / "tmp").resolve()
        prompt = (base / f"{terminal_id}.prompt").resolve()
        mcp = (base / f"{terminal_id}.mcp.json").resolve()
        if prompt.parent != base or mcp.parent != base:
            raise CaoUnavailable("CAO runtime artifact path escapes its owned directory")
        return (("runtime_prompt", prompt), ("runtime_mcp", mcp))

    def _seal_runtime_artifacts(self, run_id: int, terminal_id: str, terminal: dict[str, Any]) -> None:
        expected = dict(self._runtime_paths(run_id, terminal_id))
        candidates: list[tuple[str, Path]] = list(expected.items())
        for kind, keys in (
            ("runtime_prompt", ("runtime_prompt_path", "prompt_path")),
            ("runtime_mcp", ("runtime_mcp_path", "mcp_path")),
        ):
            for key in keys:
                value = terminal.get(key)
                if isinstance(value, str):
                    path = Path(value).resolve()
                    if kind in expected and path != expected[kind].resolve():
                        raise CaoUnavailable(f"CAO runtime artifact path mismatch: {path}")
                    if kind not in expected:
                        raise CaoUnavailable(f"unexpected CAO runtime artifact path: {path}")
                    break
        for value in terminal.get("runtime_artifacts", ()):
            if isinstance(value, dict) and isinstance(value.get("path"), str):
                kind = str(value.get("kind", "runtime_mcp"))
                if kind not in {"runtime_prompt", "runtime_mcp"}:
                    continue
                path = Path(str(value["path"])).resolve()
                if kind in expected and path != expected[kind].resolve():
                    raise CaoUnavailable(f"CAO runtime artifact path mismatch: {path}")
                if kind not in expected:
                    raise CaoUnavailable(f"unexpected CAO runtime artifact path: {path}")
        now = utc_now()
        run = self.connection.execute(
            "SELECT tr.generation,p.instance_id FROM terminal_runs tr JOIN project p ON p.id=1 WHERE tr.id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise CaoUnavailable("terminal run identity is missing")
        key = bytes.fromhex(read_private_secret(self.config.state_dir / "agent-auth-key"))
        token = derive_agent_token(key, str(run["instance_id"]), run_id, int(run["generation"]))
        secret_fields_json = json.dumps(
            {"AGENTS_AGENT_TOKEN": hashlib.sha256(token.encode()).hexdigest()},
            sort_keys=True,
            separators=(",", ":"),
        )
        for kind, path in candidates:
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
                raise CaoUnavailable(f"unsafe CAO runtime artifact: {path}")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if not validate_manifest_artifact(
                path,
                digest,
                secret_fields_json=secret_fields_json,
                secret_values={"AGENTS_AGENT_TOKEN": token},
                require_secret_values=True,
            ):
                raise CaoUnavailable(f"CAO runtime artifact has invalid secret content: {path}")
            self.connection.execute(
                "INSERT OR IGNORE INTO terminal_artifacts(terminal_run_id,kind,path,expected_sha256,"
                "secret_fields_json,state,created_at,updated_at)VALUES(?,?,?,?,?,'installed',?,?)",
                (run_id, kind, str(path), digest, secret_fields_json, now, now),
            )

    async def _adopt(self, run_id: int) -> None:
        run = self.connection.execute(
            "SELECT tr.*,la.updated_at launch_updated FROM terminal_runs tr "
            "JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return
        session: dict[str, Any] = {}
        try:
            session = await asyncio.to_thread(self.client.get_session, str(run["session_name"]))
        except CaoNotFound:
            session = {}
        except CaoUnavailable:
            return
        try:
            terminals = await asyncio.to_thread(self.client.list_terminals, str(run["session_name"]))
        except CaoNotFound:
            terminals = []
        except CaoUnavailable:
            return
        if len(terminals) > 1 or (session and not terminals):
            await self._fail_uncertain(run, "CAO session has zero or multiple terminals")
            return
        terminal_id = None
        if len(terminals) == 1:
            terminal = terminals[0]
            value = terminal.get("id") or terminal.get("terminal_id")
            if not isinstance(value, str) or not self._terminal_matches(run, terminal):
                await self._fail_uncertain(run, "CAO terminal identity/provider/profile mismatch")
                return
            if isinstance(run["terminal_id"], str) and value != run["terminal_id"]:
                await self._fail_uncertain(run, "persisted CAO terminal identity changed")
                return
            terminal_id = value
            self._record_terminal_id(run_id, terminal_id)
        if terminal_id is None and not session and not terminals and isinstance(run["terminal_id"], str):
            try:
                terminal = await asyncio.to_thread(self.client.get_terminal, str(run["terminal_id"]))
            except CaoNotFound:
                terminal = None
            except CaoUnavailable:
                return
            if terminal is not None:
                value = terminal.get("id") or terminal.get("terminal_id")
                if value != run["terminal_id"] or not self._terminal_matches(run, terminal):
                    await self._fail_uncertain(run, "persisted CAO terminal identity changed")
                    return
                terminal_id = str(run["terminal_id"])
                terminals = [terminal]
        if self._run_revoked(run_id) or str(run["state"]) != "creating":
            if session or terminals:
                with contextlib.suppress(CaoNotFound):
                    await self._delete_session(str(run["session_name"]))
                self._mark_cancelled_launch(
                    run_id,
                    "revoked launch session removed",
                    terminal_id=terminal_id,
                    actual_posted=True,
                )
            return
        session_identity = session.get("session_name", session.get("name"))
        if session_identity not in {None, run["session_name"]} or not self._session_matches(run, session):
            await self._fail_uncertain(run, "CAO session identity/provider/profile mismatch")
            return
        if terminal_id is not None:
            try:
                directory = await asyncio.to_thread(self.client.get_working_directory, terminal_id)
            except CaoUnavailable:
                return
            expected_directory = Path(str(run["working_directory"])).resolve()
            actual_directory = Path(directory).resolve()
            if actual_directory != expected_directory:
                if await self._replace_stale_session(run):
                    return
                await self._fail_uncertain(
                    run,
                    f"CAO terminal working directory mismatch: expected {expected_directory}, got {actual_directory}",
                )
                return
            try:
                self._seal_runtime_artifacts(int(run_id), terminal_id, terminals[0])
            except CaoUnavailable as exc:
                await self._fail_uncertain(run, str(exc))
                return
            now = utc_now()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                fence = self.connection.execute(
                    "SELECT tr.state terminal_state,la.state launch_state,tr.token_revoked_at "
                    "FROM terminal_runs tr JOIN launch_attempts la ON la.terminal_run_id=tr.id WHERE tr.id=?",
                    (run_id,),
                ).fetchone()
                if (
                    fence is None
                    or fence["terminal_state"] != "creating"
                    or fence["launch_state"] not in {"posting", "uncertain"}
                    or fence["token_revoked_at"] is not None
                ):
                    self.connection.rollback()
                    self._mark_cancelled_launch(
                        run_id, "launch revoked during adoption", terminal_id=terminal_id, actual_posted=True
                    )
                    await self._delete_session(str(run["session_name"]))
                    return
                self.connection.execute(
                    "UPDATE terminal_runs SET terminal_id=?,state='live',launch_count=launch_count+1,updated_at=? "
                    "WHERE id=? AND state='creating' AND token_revoked_at IS NULL",
                    (terminal_id, now, run_id),
                )
                self.connection.execute(
                    "UPDATE launch_attempts SET state='succeeded',updated_at=? "
                    "WHERE terminal_run_id=? AND state IN ('posting','uncertain')",
                    (now, run_id),
                )
                self.connection.commit()
                return
            except BaseException:
                self.connection.rollback()
                raise
        if datetime.now(UTC) - _parse(str(run["launch_updated"])) > timedelta(seconds=120):
            await self._fail_uncertain(run, "uncertain session creation")

    async def _fail_uncertain(self, run: sqlite3.Row, reason: str) -> None:
        self._recover_terminal(
            run,
            reason,
            terminal_state="ending",
            incident_kind="uncertain_launch",
        )
        try:
            await self._delete_session(str(run["session_name"]))
        except CaoUnavailable as exc:
            self._incident("terminal_cleanup_failed", "terminal", str(run["id"]), str(exc))

    async def _poll(self, run_id: int) -> None:
        run = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
        if run is None or not run["terminal_id"]:
            return
        try:
            terminal = await asyncio.to_thread(self.client.get_terminal, str(run["terminal_id"]))
            output = await asyncio.to_thread(self.client.get_output, str(run["terminal_id"]))
            directory = await asyncio.to_thread(self.client.get_working_directory, str(run["terminal_id"]))
        except CaoNotFound:
            self._missing(run)
            return
        except CaoUnavailable:
            return
        if not self._terminal_matches(run, terminal):
            self._recover_terminal(run, "terminal provider/profile identity changed")
            return
        if Path(directory).resolve() != Path(str(run["working_directory"])).resolve():
            self._recover_terminal(run, "working directory changed")
            return
        digest = hashlib.sha256(output.encode()).hexdigest()
        now = utc_now()
        since = now if digest != run["output_digest"] else (run["digest_since"] or now)
        status = str(terminal.get("status", "")).lower()
        tail = output[-128 * 1024 :]
        self._record_terminal_status(run, status, digest, tail, since, now)
        if status != "waiting_user_answer":
            self.connection.execute(
                "UPDATE blockers SET state='resolved',resolution='Provider resumed after human answer',updated_at=? "
                "WHERE terminal_run_id=? AND kind='waiting_user_answer' AND state IN ('open','escalated') "
                "AND EXISTS (SELECT 1 FROM terminal_inputs ti WHERE ti.terminal_run_id=? "
                "AND ti.state='sent' AND ti.created_at>=blockers.created_at)",
                (now, run_id, run_id),
            )
        if status == "waiting_user_answer":
            self._provider_prompt(run, tail)
        elif status == "error":
            self._recover_terminal(run, "provider terminal entered error state")
        elif status in _COMPLETION_STATUSES:
            if await self._completion_has_outcome(run):
                await self._complete_terminal(run)
            elif datetime.now(UTC) - _parse(str(since)) >= timedelta(seconds=self.config.runtime.worker_grace_seconds):
                self._missing_outcome(run)
        elif status == "processing" and datetime.now(UTC) - _parse(str(since)) > timedelta(
            seconds=self.config.runtime.stall_seconds
        ):
            self._incident("stalled_terminal", "terminal", str(run_id), "Terminal output has not changed")

    def _record_terminal_status(
        self,
        run: sqlite3.Row,
        status: str,
        digest: str,
        tail: str,
        since: str,
        now: str,
    ) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE terminal_runs SET status=?,output_digest=?,output_tail=?,digest_since=?,updated_at=? WHERE id=?",
                (status, digest, tail, since, now, run["id"]),
            )
            if _normalize_status(run["status"]) != _normalize_status(status):
                self.connection.execute(
                    "INSERT INTO events(actor_slug,kind,entity_kind,entity_id,metadata_json,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        run["actor_slug"],
                        "terminal.status_changed",
                        "terminal",
                        f"terminal:{run['id']}",
                        canonical_json(
                            {
                                "previous_status": _normalize_status(run["status"]),
                                "status": _normalize_status(status),
                                "state": run["state"],
                                "purpose_kind": run["purpose_kind"],
                                "purpose_id": run["purpose_id"],
                            }
                        ),
                        now,
                    ),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    async def _completion_has_outcome(self, run: sqlite3.Row) -> bool:
        purpose = str(run["purpose_kind"])
        if purpose == "work":
            return (
                self.connection.execute(
                    "SELECT 1 FROM submissions s JOIN executions e ON e.id=s.execution_id "
                    "WHERE e.work_id=? AND s.created_at>=?",
                    (run["purpose_id"], run["created_at"]),
                ).fetchone()
                is not None
            )
        if purpose == "consultation":
            row = self.connection.execute("SELECT state FROM consultations WHERE id=?", (run["purpose_id"],)).fetchone()
            return row is not None and row["state"] == "completed"
        if purpose == "review":
            values = str(run["purpose_id"]).split("-", 1)
            if len(values) != 2:
                return False
            row = self.connection.execute(
                "SELECT verdict FROM reviews WHERE submission_id=? AND gate=? AND terminal_run_id=? "
                "ORDER BY id DESC LIMIT 1",
                (*values, run["id"]),
            ).fetchone()
            return row is not None and row["verdict"] in {"pass", "changes_requested"}
        return False

    def _missing_outcome(self, run: sqlite3.Row) -> None:
        self._recover_terminal(
            run,
            "terminal completed without a fenced outcome",
            blocker_kind="missing_outcome",
            blocker_reason="terminal completed without a fenced outcome",
            incident_kind="missing_outcome",
        )

    async def _complete_terminal(self, run: sqlite3.Row) -> None:
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='ending',token_revoked_at=COALESCE(token_revoked_at,?),"
            "updated_at=? WHERE id=? AND state='live'",
            (now, now, run["id"]),
        )
        self.connection.execute(
            "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
            (now, run["id"]),
        )

    async def _cleanup_terminal(self, run_id: int) -> None:
        async with self._profile_lock:
            row = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["token_revoked_at"] is None:
                return
            terminal_id = str(row["terminal_id"]) if row["terminal_id"] else None
            if terminal_id is None and str(row["profile_state"]) != "reserved":
                try:
                    terminals = await asyncio.to_thread(self.client.list_terminals, str(row["session_name"]))
                except CaoNotFound:
                    terminals = []
                except CaoUnavailable as exc:
                    self._incident("terminal_cleanup_failed", "terminal", str(run_id), str(exc))
                    return
                if len(terminals) != 1:
                    self._incident(
                        "terminal_cleanup_failed",
                        "terminal",
                        str(run_id),
                        "CAO session has zero or multiple terminals; exact cleanup is unsafe",
                    )
                    return
                value = terminals[0].get("id") or terminals[0].get("terminal_id")
                if not isinstance(value, str) or not self._terminal_matches(row, terminals[0]):
                    self._incident("terminal_cleanup_failed", "terminal", str(run_id), "terminal identity changed")
                    return
                terminal_id = value
                self._record_terminal_id(run_id, terminal_id)
            try:
                await self._delete_session_locked(str(row["session_name"]))
            except CaoUnavailable as exc:
                self._incident("terminal_cleanup_failed", "terminal", str(run_id), str(exc))
                return
            except Exception as exc:
                self._incident("terminal_cleanup_failed", "terminal", str(run_id), str(exc))
                return
            rows = list(
                self.connection.execute(
                    "SELECT kind,path,fragment_key,expected_sha256,expected_json_redacted,secret_fields_json "
                    "FROM terminal_artifacts "
                    "WHERE terminal_run_id=? AND state IN ('staged','installed')",
                    (run_id,),
                )
            )
            runtime_manifest = {
                (str(item["kind"]), str(item["path"])): item
                for item in rows
                if str(item["kind"]).startswith("runtime_")
            }
            runtime_paths = list(self._runtime_paths(run_id, terminal_id)) if terminal_id else []
            for kind, path in runtime_paths:
                for manifest_kind, manifest_path in runtime_manifest:
                    if manifest_kind == kind and Path(manifest_path).resolve() != path.resolve():
                        raise ProfileError(f"runtime artifact path mismatch: {manifest_path}")
            for kind, path in runtime_manifest:
                if not any(
                    kind == expected_kind and Path(path).resolve() == expected_path.resolve()
                    for expected_kind, expected_path in runtime_paths
                ):
                    raise ProfileError(f"runtime artifact has no exact owned path: {path}")
            project = self.connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
            if project is None:
                raise ProfileError("Agents project identity is missing")
            key = bytes.fromhex(read_private_secret(self.config.state_dir / "agent-auth-key"))
            secret_values = {
                "AGENTS_AGENT_TOKEN": derive_agent_token(
                    key, str(project["instance_id"]), run_id, int(row["generation"])
                )
            }
            profile_artifacts = [
                {
                    "kind": str(item["kind"]),
                    "path": str(item["path"]),
                    "sha256": str(item["expected_sha256"]),
                    "fragment_key": item["fragment_key"],
                    "expected_json_redacted": item["expected_json_redacted"],
                    "secret_fields_json": item["secret_fields_json"],
                }
                for item in rows
                if not str(item["kind"]).startswith("runtime_")
            ]
            profile = str(row["profile_name"])
            profile_path = self.config.state_dir / "profiles" / f"{profile}.md"
            if not profile_artifacts and profile_path.is_file() and not profile_path.is_symlink():
                profile_artifacts = [{"path": str(profile_path), "sha256": str(row["profile_sha256"])}]
            cao = self.config.root / ".tools/bin/cao"
            try:
                if profile not in {"", "reserved"}:
                    if cao.is_file():
                        await asyncio.to_thread(
                            remove_profile,
                            cao,
                            self.config.cao_home,
                            profile,
                            profile_path,
                            profile_artifacts,
                            self.config.state_dir / "profiles.lock",
                            secret_values=secret_values,
                        )
                    else:
                        self._discard_profile(
                            type(
                                "Profile",
                                (),
                                {"path": profile_path, "name": profile, "secret_values": secret_values},
                            )(),
                            profile_artifacts,
                        )
                for kind, path in runtime_paths:
                    manifest = next(
                        (
                            item
                            for (manifest_kind, manifest_path), item in runtime_manifest.items()
                            if manifest_kind == kind and Path(manifest_path).resolve() == path.resolve()
                        ),
                        None,
                    )
                    if not path.exists():
                        continue
                    if manifest is not None:
                        if not validate_manifest_artifact(
                            path,
                            str(manifest["expected_sha256"]),
                            secret_fields_json=manifest["secret_fields_json"],
                            secret_values=secret_values,
                            require_secret_values=True,
                        ):
                            raise ProfileError(f"runtime artifact changed: {path}")
                    elif path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
                        raise ProfileError(f"unsafe runtime artifact: {path}")
                    path.unlink()
                now = utc_now()
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    self.connection.execute(
                        "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
                        (now, run_id),
                    )
                    self.connection.execute(
                        "UPDATE terminal_artifacts SET state='removed',updated_at=? "
                        "WHERE terminal_run_id=? AND state IN ('staged','installed')",
                        (now, run_id),
                    )
                    self.connection.execute(
                        "UPDATE terminal_runs SET state='ended',profile_state='removed',updated_at=? "
                        "WHERE id=? AND state IN ('retained','ending','failed','ended')",
                        (now, run_id),
                    )
                    self.connection.commit()
                except BaseException:
                    self.connection.rollback()
                    raise
            except (ProfileError, OSError) as exc:
                self._incident("profile_cleanup_failed", "terminal", str(run_id), str(exc))

    async def _wake(self, delivery_id: int) -> None:
        row = self.connection.execute(
            "SELECT d.attempts,tr.id AS terminal_run_id,tr.terminal_id "
            "FROM deliveries d "
            "JOIN terminal_runs tr ON tr.state='live' AND tr.token_revoked_at IS NULL "
            "AND ("
            "(d.terminal_run_id IS NOT NULL AND tr.id=d.terminal_run_id) "
            "OR (d.terminal_run_id IS NULL AND tr.actor_slug=d.actor_slug "
            "AND tr.purpose_kind='persistent' AND tr.purpose_id=d.actor_slug)"
            ") "
            "WHERE d.id=?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            return
        nonce = secrets.token_urlsafe(12)
        body = f"AGENTS_WAKE {delivery_id} {nonce}; call inbox, process messages in ID order, then ack_inbox"
        result = "accepted"
        error = None
        cao_id = None
        try:
            cao_id = await asyncio.to_thread(
                self.client.enqueue_wake, str(row["terminal_id"]), str(row["terminal_id"]), body
            )
            delay = 300
        except CaoUnavailable as exc:
            result = "failed"
            error = str(exc)
            delay = _RETRY[min(int(row["attempts"]), len(_RETRY) - 1)]
        now = datetime.now(UTC)
        self.connection.execute(
            "INSERT INTO wake_attempts(delivery_id,terminal_run_id,nonce,cao_message_id,result,error,created_at)VALUES(?,?,?,?,?,?,?)",
            (delivery_id, row["terminal_run_id"], nonce, cao_id, result, error, utc_now()),
        )
        self.connection.execute(
            "UPDATE deliveries SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE id=?",
            ((now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z"), error, delivery_id),
        )
        if int(row["attempts"]) + 1 == 5:
            self._incident("wake_delivery_failed", "delivery", str(delivery_id), "Wake delivery has failed five times")

    async def _send_input(self, input_id: int) -> None:
        row = self.connection.execute(
            "SELECT ti.*,tr.terminal_id FROM terminal_inputs ti JOIN terminal_runs tr ON tr.id=ti.terminal_run_id WHERE ti.id=?",
            (input_id,),
        ).fetchone()
        if row is None:
            return
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_inputs SET state='sending',updated_at=? WHERE id=? AND state='pending'", (now, input_id)
        )
        try:
            success = await asyncio.wait_for(
                asyncio.to_thread(self.client.send_input, str(row["terminal_id"]), str(row["body"])), timeout=10
            )
            state = "sent" if success else "failed"
            error = None if success else "CAO rejected input"
        except BaseException as exc:
            state = "uncertain"
            error = str(exc)
        self.connection.execute(
            "UPDATE terminal_inputs SET state=?,error=?,updated_at=? WHERE id=?", (state, error, utc_now(), input_id)
        )
        if state == "uncertain":
            self._incident(
                "terminal_input_uncertain", "terminal_input", str(input_id), "Terminal input outcome is uncertain"
            )

    def _provider_prompt(self, run: sqlite3.Row, tail: str) -> None:
        existing = self.connection.execute(
            "SELECT 1 FROM blockers WHERE target_kind=? AND target_id=? AND kind='waiting_user_answer' AND state IN ('open','escalated')",
            (run["purpose_kind"], run["purpose_id"]),
        ).fetchone()
        if existing:
            return
        now = utc_now()
        work_id = run["purpose_id"] if run["purpose_kind"] == "work" else None
        self.connection.execute(
            "INSERT INTO blockers(work_id,target_kind,target_id,terminal_run_id,kind,reason,requested_role,actor_slug,resume_state,state,created_at,updated_at)VALUES(?,?,?,?,? ,?,'human',?,NULL,'open',?,?)",
            (
                work_id,
                run["purpose_kind"],
                run["purpose_id"],
                run["id"],
                "waiting_user_answer",
                tail[-2048:],
                run["actor_slug"],
                now,
                now,
            ),
        )
        self._incident("provider_prompt", "terminal", str(run["id"]), "Provider is waiting for a human answer")

    def _recover_terminal(
        self,
        run: sqlite3.Row,
        reason: str,
        *,
        blocker_kind: str = "terminal_failure",
        blocker_reason: str | None = None,
        incident_kind: str = "terminal_failed",
        terminal_state: str = "failed",
        terminal_profile_state: str | None = None,
        launch_attempt_state: str = "failed",
        launch_attempt_counted: int | None = None,
    ) -> None:
        now = utc_now()
        target_kind = str(run["purpose_kind"])
        target_id = str(run["purpose_id"])
        work_id = target_id if target_kind == "work" else None
        resume_state: str | None = None
        blocker_reason = blocker_reason or reason
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute("SELECT state FROM terminal_runs WHERE id=?", (run["id"],)).fetchone()
            if current is None or str(current["state"]) in {"ending", "ended", "failed"}:
                self.connection.execute("ROLLBACK")
                return
            self.connection.execute(
                "UPDATE terminal_runs SET state=?,profile_state=COALESCE(?,profile_state),"
                "token_revoked_at=COALESCE(token_revoked_at,?),error=?,updated_at=? WHERE id=?",
                (terminal_state, terminal_profile_state, now, reason, now, run["id"]),
            )
            if launch_attempt_counted is None:
                self.connection.execute(
                    "UPDATE launch_attempts SET state=?,error=?,updated_at=? "
                    "WHERE terminal_run_id=? AND state IN ('reserved','posting','uncertain')",
                    (launch_attempt_state, reason, now, run["id"]),
                )
            else:
                self.connection.execute(
                    "UPDATE launch_attempts SET state=?,counted=?,error=?,updated_at=? "
                    "WHERE terminal_run_id=? AND state IN ('reserved','posting','uncertain')",
                    (launch_attempt_state, launch_attempt_counted, reason, now, run["id"]),
                )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=? AND released_at IS NULL",
                (now, run["id"]),
            )
            self.connection.execute(
                "UPDATE consultations SET state='failed',version=version+1,updated_at=? "
                "WHERE terminal_run_id=? AND state='assigned'",
                (now, run["id"]),
            )
            if work_id is not None:
                work = self.connection.execute(
                    "SELECT status,blocked_from FROM work_items WHERE id=?", (work_id,)
                ).fetchone()
                if work is not None:
                    status = str(work["status"])
                    resume_state = str(work["blocked_from"] or status)
                    if status != "blocked":
                        changed = self.connection.execute(
                            "UPDATE work_items SET status='blocked',blocked_from=?,version=version+1,updated_at=? "
                            "WHERE id=? AND status=?",
                            (resume_state, now, work_id, status),
                        )
                        if changed.rowcount != 1:
                            raise RuntimeError("terminal recovery changed work state")
            pending_reviews = list(
                self.connection.execute(
                    "SELECT id FROM reviews WHERE terminal_run_id=? AND verdict='pending' ORDER BY id DESC",
                    (run["id"],),
                )
            )
            for review in pending_reviews:
                self.connection.execute(
                    "UPDATE reviews SET verdict='superseded',"
                    "body='Review terminal failed before submitting a verdict; assignment superseded.',"
                    "updated_at=? WHERE id=? AND verdict='pending'",
                    (now, review["id"]),
                )
            if (
                self.connection.execute(
                    "SELECT 1 FROM blockers WHERE target_kind=? AND target_id=? AND state IN ('open','escalated')",
                    (target_kind, target_id),
                ).fetchone()
                is None
            ):
                self.connection.execute(
                    "INSERT INTO blockers(work_id,target_kind,target_id,terminal_run_id,kind,reason,"
                    "requested_role,actor_slug,resume_state,state,created_at,updated_at)VALUES(?,?,?,?,"
                    "?,?,'human',?,?, 'open',?,?)",
                    (
                        work_id,
                        target_kind,
                        target_id,
                        run["id"],
                        blocker_kind,
                        blocker_reason,
                        run["actor_slug"],
                        resume_state,
                        now,
                        now,
                    ),
                )
            self._incident("terminal_failed", "terminal", str(run["id"]), reason)
            if incident_kind != "terminal_failed":
                self._incident(incident_kind, "terminal", str(run["id"]), reason)
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _missing(self, run: sqlite3.Row) -> None:
        self._recover_terminal(
            run,
            "Mapped CAO session disappeared",
            blocker_kind="missing_session",
            blocker_reason="mapped CAO session disappeared",
            incident_kind="missing_session",
        )

    def _cleanup_expired(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.config.runtime.worker_grace_seconds)
        cutoff_text = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for row in self.connection.execute(
            "SELECT id FROM terminal_runs WHERE state IN ('retained','ending','failed','ended') AND updated_at<=?",
            (cutoff_text,),
        ):
            run_id = int(row["id"])
            self._spawn(f"cleanup:{run_id}", self._cleanup_terminal(run_id))
        try:
            for row in self.connection.execute(
                "SELECT e.worktree_path FROM executions e JOIN work_items w ON w.id=e.work_id "
                "WHERE w.status IN ('delivered','cancelled') AND e.updated_at<=?",
                (cutoff_text,),
            ):
                path = Path(str(row["worktree_path"]))
                if path.exists():
                    try:
                        remove_recorded_worktree(self.config.project.path, path, head_sha(path))
                    except (GitError, OSError) as exc:
                        self._incident("worktree_cleanup_mismatch", "worktree", str(path), str(exc))
            for row in self.connection.execute(
                "SELECT worktree_path,target_sha FROM checks WHERE state IN ('passed','failed','interrupted') "
                "AND updated_at<=?",
                (cutoff_text,),
            ):
                path = Path(str(row["worktree_path"]))
                if path.exists():
                    try:
                        remove_recorded_worktree(
                            self.config.project.path,
                            path,
                            str(row["target_sha"]),
                            allow_dirty=True,
                        )
                    except (GitError, OSError) as exc:
                        self._incident("checktree_cleanup_mismatch", "worktree", str(path), str(exc))
            for row in self.connection.execute(
                "SELECT r.worktree_path,s.commit_sha FROM reviews r "
                "JOIN submissions s ON s.id=r.submission_id "
                "JOIN executions e ON e.id=s.execution_id "
                "JOIN work_items w ON w.id=e.work_id "
                "WHERE w.status IN ('delivered','cancelled') AND w.updated_at<=?",
                (cutoff_text,),
            ):
                path = Path(str(row["worktree_path"]))
                if path.exists():
                    try:
                        remove_recorded_worktree(
                            self.config.project.path,
                            path,
                            str(row["commit_sha"]),
                        )
                    except (GitError, OSError) as exc:
                        self._incident("reviewtree_cleanup_mismatch", "worktree", str(path), str(exc))
        except Exception as exc:
            self._incident("worktree_cleanup_failed", "worktree", "global", str(exc))

    def _incident(self, kind: str, entity_kind: str, entity_id: str, summary: str) -> None:
        if self.connection.execute(
            "SELECT 1 FROM incidents WHERE kind=? AND entity_kind=? AND entity_id=? AND state='open'",
            (kind, entity_kind, entity_id),
        ).fetchone():
            return
        now = utc_now()
        self.connection.execute(
            "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,created_at,updated_at)VALUES(?,?,?,'high','open',?,'{}',?,?)",
            (kind, entity_kind, entity_id, summary, now, now),
        )
