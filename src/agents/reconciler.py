from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .auth import derive_agent_token, read_private_secret, token_digest
from .config import AgentsConfig, IsolationMode
from .container_runtime import ContainerRuntime, build_execution_backend
from .db import canonical_json, utc_now
from .execution import (
    ExecutionBackend,
    ExecutionBusy,
    ExecutionConflict,
    ExecutionError,
    ExecutionNotFound,
    ExecutionStatus,
    ExecutionTerminated,
    ExecutionTimeout,
    ExecutionUnavailable,
    RunHandle,
    RunSnapshot,
    RunSpec,
)
from .git_worktree import GitError, head_sha, remove_recorded_workspace
from .profiles import (
    PROVIDER_CAPABILITIES,
    ProfileError,
    execution_name,
    install_profile,
    materialize_profile,
    mcp_name,
    profile_name,
    remove_profile,
)

_RETRY = (1, 5, 30, 120, 300)

_COMPLETION_STATUSES = {"completed", "complete", "done", "exited", "stopped"}
_STALE_CWD_REPLACED = "stale backend workspace replaced after cwd mismatch"


def _api_url(config: AgentsConfig) -> str:
    host = "host.docker.internal" if config.execution.isolation is IsolationMode.CONTAINER else "127.0.0.1"
    return f"http://{host}:{config.web.port}"


def _profile_runtime(config: AgentsConfig, agent_auth_id: str) -> tuple[Path | None, Path]:
    if config.execution.isolation is IsolationMode.HOST:
        return None, config.state_dir / "runtime"
    root = config.state_dir / "runtime" / agent_auth_id
    return root / "home", root / "provider"


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
    provider = config.execution.provider_id
    execution_backend = "herdr-container" if config.execution.isolation is IsolationMode.CONTAINER else "herdr"
    container_image_id: str | None = None
    if config.execution.isolation is IsolationMode.CONTAINER:
        if config.execution.container is None:
            raise RuntimeError("container configuration is required")
        container_image_id = ContainerRuntime(config.execution.container).resolve_image_id(
            config.execution.container.image
        )
    model = secrets.choice(config.models_for(actor))
    cursor = connection.execute(
        "INSERT INTO terminal_runs(execution_name,execution_backend,container_image_id,profile_name,mcp_name,profile_sha256,provider,model,reasoning_effort,generation,actor_slug,purpose_kind,purpose_id,working_directory,token_digest,agent_auth_id,profile_state,state,output_tail,launch_count,created_at,updated_at)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',0,?,?)",
        (
            "reserved",
            execution_backend,
            container_image_id,
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
            None,
            "reserved",
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
    execution = execution_name(instance, purpose_kind, session_purpose_id, actor, generation)
    key = bytes.fromhex(read_private_secret(config.state_dir / "agent-auth-key"))
    token = derive_agent_token(key, instance, run_id, generation)
    connection.execute(
        "UPDATE terminal_runs SET execution_name=?,profile_name=?,mcp_name=?,agent_auth_id=?,token_digest=?,updated_at=? WHERE id=?",
        (execution, profile, mcp, profile, token_digest(token), now, run_id),
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
            "SELECT COUNT(*) total,"
            "COUNT(*) FILTER (WHERE error IS NULL OR ("
            "error NOT LIKE 'CAO terminal working directory mismatch:%' "
            "AND error NOT LIKE 'transient Herdr cutover:%')) relevant "
            "FROM terminal_runs WHERE actor_slug=? AND purpose_kind='persistent'",
            (actor,),
        ).fetchone()
        if attempts["relevant"] >= 3 or attempts["total"] >= 12:
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


class Reconciler:
    def __init__(
        self,
        config: AgentsConfig,
        connection: sqlite3.Connection,
        backend: ExecutionBackend | None = None,
    ) -> None:
        self.config = config
        self.connection = connection
        self.backend = backend or build_execution_backend(config)
        self._dirty_runs: set[int] = set()
        self._terminated_runs: set[int] = set()
        self._event_task: asyncio.Task[Any] | None = None
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
        self._event_task = asyncio.create_task(self._consume_events())
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
            if self._event_task is not None:
                self._event_task.cancel()
            tasks = (*self._tasks, *((self._event_task,) if self._event_task is not None else ()))
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.backend.close()

    async def _consume_events(self) -> None:
        async for event in self.backend.events():
            if event.kind not in {
                "pane.updated",
                "pane.agent_status_changed",
                "pane.exited",
                "pane.closed",
                "workspace.closed",
            }:
                continue
            conditions: list[str] = []
            values: list[str] = []
            if event.run_id is not None:
                conditions.append("backend_run_id=?")
                values.append(event.run_id)
            if event.terminal_id is not None:
                conditions.append("backend_terminal_id=?")
                values.append(event.terminal_id)
            if not conditions:
                continue
            rows = self.connection.execute(
                f"SELECT id FROM terminal_runs WHERE state='live' AND ({' OR '.join(conditions)})",
                tuple(values),
            )
            for row in rows:
                run_id = int(row["id"])
                if event.kind in {"pane.exited", "pane.closed", "workspace.closed"}:
                    self._terminated_runs.add(run_id)
                self._dirty_runs.add(run_id)
                self._spawn(f"poll:{run_id}", self._poll(run_id))

    def _fence_stale_provider_runs(self) -> None:
        configured = self.config.execution.provider_id
        configured_backend = (
            "herdr-container" if self.config.execution.isolation is IsolationMode.CONTAINER else "herdr"
        )
        for run in list(
            self.connection.execute(
                "SELECT DISTINCT tr.* FROM terminal_runs tr LEFT JOIN launch_attempts la "
                "ON la.terminal_run_id=tr.id "
                "WHERE tr.state IN ('reserved','creating','live') OR la.state IN ('posting','uncertain')"
            )
        ):
            provider = str(run["provider"])
            stored_backend = str(run["execution_backend"])
            if provider == configured and provider in PROVIDER_CAPABILITIES and stored_backend == configured_backend:
                continue
            self._recover_terminal(
                run,
                f"stored provider/backend {provider!r}/{stored_backend!r} does not match "
                f"configured {configured!r}/{configured_backend!r}",
                terminal_state="ending",
                blocker_kind="provider_or_backend_changed",
                incident_kind="provider_or_backend_changed",
            )

    async def run_once(self) -> None:
        from .delivery import Delivery
        from .schedules import Scheduler

        try:
            self._fence_stale_provider_runs()
            bootstrap_persistent_agents(self.connection, self.config)
            if not self._recovered_checks:
                Delivery(self.config, self.connection, backend=self.backend).recover_running_checks()
                self._recovered_checks = True
        except Exception as exc:
            self._incident("bootstrap_failed", "persistent", "global", str(exc))
        try:
            Scheduler(self.config, self.connection).dispatch_due()
        except Exception as exc:
            self._incident("schedule_dispatch_failed", "schedule", "global", str(exc))
        health = await asyncio.to_thread(self.backend.health)
        healthy = health.healthy
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
        project = self.connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        if project is None:
            return
        prefix = f"agents-{project['instance_id']}-"
        mapped = {
            str(row["execution_name"])
            for row in self.connection.execute(
                "SELECT execution_name FROM terminal_runs "
                "WHERE state IN ('reserved','creating','live') AND token_revoked_at IS NULL"
            )
        }
        expected_cwds = {
            Path(str(row["working_directory"])).resolve()
            for row in self.connection.execute("SELECT DISTINCT working_directory FROM terminal_runs")
        }
        try:
            runs = await asyncio.to_thread(self.backend.list_runs, prefix)
        except ExecutionUnavailable, ExecutionTimeout, ExecutionBusy:
            return
        for run in runs:
            if run.handle.name in mapped:
                continue
            if run.cwd not in expected_cwds:
                self._incident(
                    "terminal_cleanup_refused",
                    "terminal",
                    run.handle.name,
                    f"backend cwd {run.cwd} is not recorded in Agents state",
                )
                continue
            try:
                async with self._profile_lock:
                    await asyncio.to_thread(self.backend.delete_run, run.handle)
            except ExecutionError as exc:
                self._incident("terminal_cleanup_failed", "terminal", run.handle.name, str(exc))

    async def _advance_delivery(self, provider_healthy: bool) -> None:
        from .delivery import Delivery

        delivery = Delivery(self.config, self.connection, backend=self.backend)
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
        if run is None or not self._launch_fence(run_id, {"reserved"}, {"reserved"}):
            return
        materialized = None
        launch = None
        posted = False
        try:
            key = bytes.fromhex(read_private_secret(self.config.state_dir / "agent-auth-key"))
            token = derive_agent_token(key, str(run["instance_id"]), run_id, int(run["generation"]))
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
                api_url=_api_url(self.config),
                reasoning_effort=str(run["reasoning_effort"]),
                mcp_command=(
                    "/opt/agents/.venv/bin/agents-mcp-server"
                    if self.config.execution.isolation is IsolationMode.CONTAINER
                    else None
                ),
            )
            if not self._launch_fence(run_id, {"reserved"}, {"reserved"}):
                self._discard_profile(materialized, [])
                return
            provider_home, provider_runtime = _profile_runtime(self.config, str(run["agent_auth_id"]))
            async with self._profile_lock:
                launch = await asyncio.to_thread(
                    install_profile,
                    materialized,
                    str(run["provider"]),
                    self.config.state_dir / "profiles.lock",
                    provider_home=provider_home,
                    runtime_dir=provider_runtime,
                    agent_auth_id=str(run["agent_auth_id"]),
                    model=str(run["model"]),
                )
            artifacts = list(launch.artifacts)
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
                    self._discard_profile(materialized, artifacts)
                    return
                self.connection.execute(
                    "UPDATE terminal_runs SET profile_sha256=?,profile_state='installed',state='creating',updated_at=? "
                    "WHERE id=? AND state='reserved' AND token_revoked_at IS NULL",
                    (materialized.sha256, now, run_id),
                )
                self.connection.execute(
                    "UPDATE launch_attempts SET counted=1,state='posting',updated_at=? "
                    "WHERE terminal_run_id=? AND state='reserved'",
                    (now, run_id),
                )
                for artifact in artifacts:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO terminal_artifacts(terminal_run_id,kind,path,fragment_key,"
                        "expected_sha256,expected_json_redacted,secret_fields_json,state,created_at,updated_at)"
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
            if not self._launch_fence(run_id, {"creating"}, {"posting"}):
                self._mark_cancelled_launch(run_id, "launch revoked before backend create", actual_posted=False)
                self._discard_profile(materialized, artifacts)
                return
            provider = str(run["provider"])
            mock = provider == "mock_cli"
            kind = {"opencode_cli": "opencode", "claude_code": "claude"}.get(provider, "mock")
            agent_name = f"agents-r{run_id:010d}-g{int(run['generation']):04d}"
            environment = dict(launch.env)
            environment.update(
                {
                    "AGENTS_AGENT_TOKEN": token,
                    "AGENTS_API_URL": _api_url(self.config),
                    "AGENTS_EXECUTION_ID": str(run["agent_auth_id"]),
                }
            )
            if broker_url := os.environ.get("AGENTS_SECRETS_API_URL"):
                environment["AGENTS_SECRETS_API_URL"] = broker_url
                environment["AGENTS_SECRETS_TRANSPORT"] = "agent-api"
            spec = RunSpec(
                str(run["execution_name"]),
                run_id,
                int(run["generation"]),
                Path(str(run["working_directory"])),
                agent_name,
                kind,
                tuple(launch.argv),
                tuple(sorted(environment.items())),
                provider,
                mock,
                str(run["container_image_id"] or ""),
            )
            posted = True
            snapshot = await asyncio.to_thread(self.backend.create_run, spec)
            if not self._snapshot_matches(run, snapshot, agent_name=agent_name, agent_kind=kind, mock=mock):
                raise ExecutionConflict("identity_mismatch", "backend created a mismatched run")
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
                        run_id,
                        "launch revoked after backend create",
                        handle=snapshot.handle,
                        actual_posted=True,
                    )
                    await asyncio.to_thread(self.backend.delete_run, snapshot.handle)
                    return
                self.connection.execute(
                    "UPDATE terminal_runs SET backend_run_id=?,backend_terminal_id=?,backend_revision=?,"
                    "state='live',error=NULL,launch_count=launch_count+1,updated_at=? "
                    "WHERE id=? AND state='creating' AND token_revoked_at IS NULL",
                    (
                        snapshot.handle.run_id,
                        snapshot.handle.terminal_id,
                        snapshot.revision,
                        now,
                        run_id,
                    ),
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
        except (ExecutionUnavailable, ExecutionBusy) as exc:
            if posted and not exc.outcome_unknown:
                now = utc_now()
                self.connection.execute(
                    "UPDATE terminal_runs SET state='reserved',updated_at=? WHERE id=? AND state='creating'",
                    (now, run_id),
                )
                self.connection.execute(
                    "UPDATE launch_attempts SET state='reserved',error=?,updated_at=? "
                    "WHERE terminal_run_id=? AND state='posting'",
                    (str(exc), now, run_id),
                )
            elif not posted:
                self._abort_prepost(run_id, str(exc))
        except ExecutionError as exc:
            if posted and (exc.outcome_unknown or isinstance(exc, (ExecutionTimeout, ExecutionConflict))):
                self.connection.execute(
                    "UPDATE launch_attempts SET state='uncertain',error=?,updated_at=? "
                    "WHERE terminal_run_id=? AND state='posting'",
                    (str(exc), utc_now(), run_id),
                )
                await self._adopt(run_id)
            else:
                current = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
                if current is not None:
                    self._recover_terminal(current, str(exc), incident_kind="terminal_launch_failed")
        except BaseException as exc:
            if posted:
                self.connection.execute(
                    "UPDATE launch_attempts SET state='uncertain',error=?,updated_at=? "
                    "WHERE terminal_run_id=? AND state='posting'",
                    (str(exc), utc_now(), run_id),
                )
                await self._adopt(run_id)
            else:
                self._abort_prepost(run_id, str(exc))
                if materialized is not None:
                    self._discard_profile(materialized, list(launch.artifacts) if launch is not None else [])

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

    def _record_backend_handle(self, run_id: int, handle: RunHandle, revision: int | None) -> None:
        self.connection.execute(
            "UPDATE terminal_runs SET backend_run_id=COALESCE(backend_run_id,?),"
            "backend_terminal_id=COALESCE(backend_terminal_id,?),backend_revision=?,updated_at=? WHERE id=?",
            (handle.run_id, handle.terminal_id, revision, utc_now(), run_id),
        )

    def _mark_cancelled_launch(
        self, run_id: int, reason: str, *, handle: RunHandle | None = None, actual_posted: bool
    ) -> None:
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE terminal_runs SET token_revoked_at=COALESCE(token_revoked_at,?),"
                "state=CASE WHEN state IN ('reserved','creating','live','retained') THEN 'ending' ELSE state END,"
                "backend_run_id=COALESCE(backend_run_id,?),"
                "backend_terminal_id=COALESCE(backend_terminal_id,?),error=COALESCE(error,?),updated_at=? WHERE id=?",
                (
                    now,
                    handle.run_id if handle is not None else None,
                    handle.terminal_id if handle is not None else None,
                    reason,
                    now,
                    run_id,
                ),
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

    async def _delete_run_locked(self, handle: RunHandle) -> None:
        await asyncio.to_thread(self.backend.delete_run, handle)

    async def _delete_run(self, handle: RunHandle) -> None:
        async with self._profile_lock:
            await self._delete_run_locked(handle)

    async def _replace_stale_cwd_run(self, run: sqlite3.Row, snapshot: RunSnapshot) -> bool:
        if run["error"] == _STALE_CWD_REPLACED:
            return False
        try:
            await self._delete_run(snapshot.handle)
        except ExecutionError:
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
                "UPDATE terminal_runs SET state='reserved',backend_run_id=NULL,backend_terminal_id=NULL,"
                "backend_revision=NULL,status=NULL,output_digest=NULL,output_tail='',digest_since=NULL,error=?,"
                "updated_at=? WHERE id=?",
                (_STALE_CWD_REPLACED, now, run["id"]),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='reserved',counted=0,error=?,updated_at=? WHERE terminal_run_id=?",
                (_STALE_CWD_REPLACED, now, run["id"]),
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
            row = self.connection.execute(
                "SELECT provider,agent_auth_id FROM terminal_runs WHERE profile_name=? ORDER BY id DESC LIMIT 1",
                (str(materialized.name),),
            ).fetchone()
            managed_artifacts = artifacts or [
                {
                    "kind": "source",
                    "path": str(materialized.path),
                    "sha256": str(materialized.sha256),
                    "secret_fields_json": "{}",
                }
            ]
            provider_home, provider_runtime = _profile_runtime(
                self.config, str(row["agent_auth_id"]) if row is not None else ""
            )
            remove_profile(
                str(materialized.name),
                Path(materialized.path),
                managed_artifacts,
                self.config.state_dir / "profiles.lock",
                provider_home=provider_home,
                runtime_dir=provider_runtime,
                secret_values=dict(getattr(materialized, "secret_values", None) or {}),
            )
            if row is not None:
                self.connection.execute(
                    "UPDATE terminal_artifacts SET state='removed',updated_at=? "
                    "WHERE terminal_run_id=(SELECT id FROM terminal_runs WHERE profile_name=? ORDER BY id DESC LIMIT 1) "
                    "AND state IN ('staged','installed')",
                    (utc_now(), str(materialized.name)),
                )
        except (OSError, ProfileError) as exc:
            self._incident("profile_cleanup_failed", "terminal", str(materialized.name), str(exc))

    @staticmethod
    def _snapshot_matches(
        run: sqlite3.Row,
        snapshot: RunSnapshot,
        *,
        agent_name: str | None = None,
        agent_kind: str | None = None,
        mock: bool | None = None,
    ) -> bool:
        expected_name = str(run["execution_name"])
        expected_run_id = run["backend_run_id"]
        expected_terminal_id = run["backend_terminal_id"]
        if snapshot.handle.name != expected_name or snapshot.backend_name != expected_name:
            return False
        if expected_run_id is not None and snapshot.handle.run_id != expected_run_id:
            return False
        if expected_terminal_id is not None and snapshot.handle.terminal_id != expected_terminal_id:
            return False
        if snapshot.cwd != Path(str(run["working_directory"])).resolve():
            return False
        is_mock = str(run["provider"]) == "mock_cli" if mock is None else mock
        if is_mock:
            return snapshot.agent_name is None and snapshot.agent_kind is None
        expected_agent_name = agent_name or f"agents-r{int(run['id']):010d}-g{int(run['generation']):04d}"
        expected_agent_kind = agent_kind or {
            "opencode_cli": "opencode",
            "claude_code": "claude",
        }.get(str(run["provider"]))
        return (snapshot.agent_name, snapshot.agent_kind) == (expected_agent_name, expected_agent_kind)

    async def _spec_for_uncertain_run(self, run: sqlite3.Row) -> RunSpec:
        run_id = int(run["id"])
        generation = int(run["generation"])
        key = bytes.fromhex(read_private_secret(self.config.state_dir / "agent-auth-key"))
        token = derive_agent_token(key, str(run["instance_id"]), run_id, generation)
        materialized = await asyncio.to_thread(
            materialize_profile,
            self.config.root,
            self.config.state_dir,
            template=str(run["profile_template"]),
            instance=str(run["instance_id"]),
            run_id=run_id,
            generation=generation,
            provider=str(run["provider"]),
            purpose_kind=str(run["purpose_kind"]),
            specialty=run["specialty"],
            token=token,
            api_url=_api_url(self.config),
            reasoning_effort=str(run["reasoning_effort"]),
            mcp_command=(
                "/opt/agents/.venv/bin/agents-mcp-server"
                if self.config.execution.isolation is IsolationMode.CONTAINER
                else None
            ),
        )
        provider_home, provider_runtime = _profile_runtime(self.config, str(run["agent_auth_id"]))
        async with self._profile_lock:
            launch = await asyncio.to_thread(
                install_profile,
                materialized,
                str(run["provider"]),
                self.config.state_dir / "profiles.lock",
                provider_home=provider_home,
                runtime_dir=provider_runtime,
                agent_auth_id=str(run["agent_auth_id"]),
                model=str(run["model"]),
            )
        provider = str(run["provider"])
        environment = dict(launch.env)
        environment.update(
            {
                "AGENTS_AGENT_TOKEN": token,
                "AGENTS_API_URL": _api_url(self.config),
                "AGENTS_EXECUTION_ID": str(run["agent_auth_id"]),
            }
        )
        if broker_url := os.environ.get("AGENTS_SECRETS_API_URL"):
            environment["AGENTS_SECRETS_API_URL"] = broker_url
            environment["AGENTS_SECRETS_TRANSPORT"] = "agent-api"
        return RunSpec(
            str(run["execution_name"]),
            run_id,
            generation,
            Path(str(run["working_directory"])),
            f"agents-r{run_id:010d}-g{generation:04d}",
            {"opencode_cli": "opencode", "claude_code": "claude"}.get(provider, "mock"),
            tuple(launch.argv),
            tuple(sorted(environment.items())),
            provider,
            provider == "mock_cli",
            str(run["container_image_id"] or ""),
        )

    async def _adopt(self, run_id: int) -> None:
        run = self.connection.execute(
            "SELECT tr.*,la.updated_at launch_updated,p.instance_id,a.profile_template,a.specialty "
            "FROM terminal_runs tr JOIN launch_attempts la ON la.terminal_run_id=tr.id "
            "JOIN project p ON p.id=1 JOIN actors a ON a.slug=tr.actor_slug WHERE tr.id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return
        try:
            snapshot = await asyncio.to_thread(self.backend.find_run, str(run["execution_name"]))
        except ExecutionUnavailable, ExecutionBusy, ExecutionTimeout:
            return
        except ExecutionConflict as exc:
            await self._fail_uncertain(run, str(exc), cleanup_matches=True)
            return
        if snapshot is None:
            if datetime.now(UTC) - _parse(str(run["launch_updated"])) > timedelta(seconds=120):
                await self._fail_uncertain(run, "uncertain backend run creation")
            return
        if self._run_revoked(run_id) or str(run["state"]) != "creating":
            self._mark_cancelled_launch(
                run_id,
                "revoked launch run removed",
                handle=snapshot.handle,
                actual_posted=True,
            )
            with contextlib.suppress(ExecutionNotFound):
                await self._delete_run(snapshot.handle)
            return
        if (
            snapshot.handle.name == run["execution_name"]
            and snapshot.cwd != Path(str(run["working_directory"])).resolve()
            and await self._replace_stale_cwd_run(run, snapshot)
        ):
            return
        if not self._snapshot_matches(run, snapshot):
            empty_shell = snapshot.agent_name is None and snapshot.agent_kind is None
            if empty_shell:
                try:
                    spec = await self._spec_for_uncertain_run(run)
                    snapshot = await asyncio.to_thread(self.backend.create_run, spec)
                except ExecutionUnavailable, ExecutionBusy, ExecutionTimeout:
                    return
                except ExecutionError as exc:
                    await self._fail_uncertain(run, str(exc))
                    return
                if self._snapshot_matches(run, snapshot):
                    self._record_backend_handle(run_id, snapshot.handle, snapshot.revision)
                else:
                    empty_shell = False
            if not self._snapshot_matches(run, snapshot):
                expected_prefix = f"agents-{run['instance_id']}-"
                expected_cwd = Path(str(run["working_directory"])).resolve()
                await self._fail_uncertain(run, "backend run identity, occupant, or cwd mismatch")
                if snapshot.handle.name.startswith(expected_prefix) and snapshot.cwd == expected_cwd:
                    with contextlib.suppress(ExecutionNotFound):
                        await self._delete_run(snapshot.handle)
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
                    run_id,
                    "launch revoked during adoption",
                    handle=snapshot.handle,
                    actual_posted=True,
                )
                await self._delete_run(snapshot.handle)
                return
            self.connection.execute(
                "UPDATE terminal_runs SET backend_run_id=?,backend_terminal_id=?,backend_revision=?,"
                "state='live',launch_count=launch_count+1,updated_at=? "
                "WHERE id=? AND state='creating' AND token_revoked_at IS NULL",
                (
                    snapshot.handle.run_id,
                    snapshot.handle.terminal_id,
                    snapshot.revision,
                    now,
                    run_id,
                ),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='succeeded',updated_at=? "
                "WHERE terminal_run_id=? AND state IN ('posting','uncertain')",
                (now, run_id),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    async def _fail_uncertain(
        self,
        run: sqlite3.Row,
        reason: str,
        *,
        cleanup_matches: bool = False,
    ) -> None:
        self._recover_terminal(
            run,
            reason,
            terminal_state="ending",
            incident_kind="uncertain_launch",
        )
        handles: list[RunHandle] = []
        if run["backend_run_id"] and run["backend_terminal_id"]:
            handles.append(
                RunHandle(
                    str(run["execution_name"]),
                    str(run["backend_run_id"]),
                    str(run["backend_terminal_id"]),
                )
            )
        elif cleanup_matches:
            try:
                matches = await asyncio.to_thread(self.backend.list_runs, str(run["execution_name"]))
            except ExecutionError as exc:
                self._incident("terminal_cleanup_failed", "terminal", str(run["id"]), str(exc))
                return
            expected_cwd = Path(str(run["working_directory"])).resolve()
            handles.extend(
                snapshot.handle
                for snapshot in matches
                if snapshot.handle.name == str(run["execution_name"]) and snapshot.cwd == expected_cwd
            )
        for handle in handles:
            try:
                await self._delete_run(handle)
            except ExecutionError as exc:
                self._incident("terminal_cleanup_failed", "terminal", str(run["id"]), str(exc))

    async def _poll(self, run_id: int) -> None:
        run = self.connection.execute("SELECT * FROM terminal_runs WHERE id=?", (run_id,)).fetchone()
        if run is None or not run["backend_run_id"] or not run["backend_terminal_id"]:
            return
        handle = RunHandle(
            str(run["execution_name"]),
            str(run["backend_run_id"]),
            str(run["backend_terminal_id"]),
        )
        if run_id in self._terminated_runs:
            self._terminated_runs.discard(run_id)
            self._missing(run)
            return
        try:
            snapshot = await asyncio.to_thread(self.backend.get_run, handle, include_output=False)
            if not self._snapshot_matches(run, snapshot):
                self._recover_terminal(run, "backend run identity, occupant, or cwd changed")
                return
            status = snapshot.status.value
            include_output = (
                run_id in self._dirty_runs
                or snapshot.revision != run["backend_revision"]
                or status != str(run["status"] or "")
            )
            if include_output:
                snapshot = await asyncio.to_thread(self.backend.get_run, handle, include_output=True)
        except ExecutionNotFound, ExecutionTerminated:
            self._missing(run)
            return
        except ExecutionUnavailable, ExecutionBusy, ExecutionTimeout:
            return
        except ExecutionError as exc:
            self._recover_terminal(run, str(exc))
            return
        finally:
            self._dirty_runs.discard(run_id)
        output = snapshot.output if include_output else str(run["output_tail"])
        digest = hashlib.sha256(output.encode()).hexdigest() if include_output else str(run["output_digest"] or "")
        now = utc_now()
        since = now if digest != run["output_digest"] else (run["digest_since"] or now)
        tail = output[-128 * 1024 :]
        status = snapshot.status.value
        self.connection.execute(
            "UPDATE terminal_runs SET backend_revision=?,updated_at=? WHERE id=?",
            (snapshot.revision, now, run_id),
        )
        self._record_terminal_status(run, status, digest, tail, since, now)
        if status != ExecutionStatus.WAITING_USER_ANSWER.value:
            self.connection.execute(
                "UPDATE blockers SET state='resolved',resolution='Provider resumed after human answer',updated_at=? "
                "WHERE terminal_run_id=? AND kind='waiting_user_answer' AND state IN ('open','escalated') "
                "AND EXISTS (SELECT 1 FROM terminal_inputs ti WHERE ti.terminal_run_id=? "
                "AND ti.state='sent' AND ti.created_at>=blockers.created_at)",
                (now, run_id, run_id),
            )
        if status == ExecutionStatus.WAITING_USER_ANSWER.value:
            self._provider_prompt(run, tail)
        elif status == ExecutionStatus.ERROR.value:
            self._recover_terminal(run, "provider run entered error state")
        elif status == ExecutionStatus.COMPLETED.value:
            if await self._completion_has_outcome(run):
                await self._complete_terminal(run)
            elif datetime.now(UTC) - _parse(str(since)) >= timedelta(seconds=self.config.runtime.worker_grace_seconds):
                self._missing_outcome(run)
        elif run["digest_since"] and datetime.now(UTC) - _parse(str(run["digest_since"])) > timedelta(
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
            row = self.connection.execute(
                "SELECT tr.*,p.instance_id FROM terminal_runs tr JOIN project p ON p.id=1 WHERE tr.id=?",
                (run_id,),
            ).fetchone()
            if row is None or row["token_revoked_at"] is None:
                return
            handle: RunHandle | None = None
            if row["backend_run_id"] and row["backend_terminal_id"]:
                handle = RunHandle(
                    str(row["execution_name"]),
                    str(row["backend_run_id"]),
                    str(row["backend_terminal_id"]),
                )
            else:
                try:
                    snapshot = await asyncio.to_thread(self.backend.find_run, str(row["execution_name"]))
                except ExecutionUnavailable, ExecutionBusy, ExecutionTimeout:
                    return
                except ExecutionError as exc:
                    self._incident("terminal_cleanup_failed", "terminal", str(run_id), str(exc))
                    return
                if snapshot is not None:
                    if snapshot.cwd != Path(str(row["working_directory"])).resolve():
                        self._incident(
                            "terminal_cleanup_failed",
                            "terminal",
                            str(run_id),
                            "backend run cwd changed; cleanup is unsafe",
                        )
                        return
                    handle = snapshot.handle
                    self._record_backend_handle(run_id, handle, snapshot.revision)
            if handle is not None:
                try:
                    await asyncio.to_thread(self.backend.delete_run, handle)
                except ExecutionUnavailable, ExecutionBusy, ExecutionTimeout:
                    return
                except ExecutionError as exc:
                    self._incident("terminal_cleanup_failed", "terminal", str(run_id), str(exc))
                    return
            artifacts = [
                dict(item)
                for item in self.connection.execute(
                    "SELECT kind,path,fragment_key,expected_sha256 AS sha256,expected_json_redacted,"
                    "secret_fields_json FROM terminal_artifacts "
                    "WHERE terminal_run_id=? AND state IN ('staged','installed')",
                    (run_id,),
                )
            ]
            key = bytes.fromhex(read_private_secret(self.config.state_dir / "agent-auth-key"))
            token = derive_agent_token(key, str(row["instance_id"]), run_id, int(row["generation"]))
            provider_home, provider_runtime = _profile_runtime(self.config, str(row["agent_auth_id"]))
            try:
                await asyncio.to_thread(
                    remove_profile,
                    str(row["profile_name"]),
                    self.config.state_dir / "profiles" / f"{row['profile_name']}.md",
                    artifacts,
                    self.config.state_dir / "profiles.lock",
                    provider_home=provider_home,
                    runtime_dir=provider_runtime,
                    secret_values={"AGENTS_AGENT_TOKEN": token},
                )
            except (OSError, ProfileError) as exc:
                self._incident("profile_cleanup_failed", "terminal", str(run_id), str(exc))
                return
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_artifacts SET state='removed',updated_at=? "
                "WHERE terminal_run_id=? AND state IN ('staged','installed')",
                (now, run_id),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=COALESCE(released_at,?) "
                "WHERE terminal_run_id=? AND released_at IS NULL",
                (now, run_id),
            )
            self.connection.execute(
                "UPDATE terminal_runs SET profile_state='removed',state='ended',updated_at=? "
                "WHERE id=? AND state IN ('retained','ending','failed','ended')",
                (now, run_id),
            )
            self.connection.commit()

    async def _wake(self, delivery_id: int) -> None:
        row = self.connection.execute(
            "SELECT d.attempts,tr.id AS terminal_run_id,tr.execution_name,tr.backend_run_id,tr.backend_terminal_id "
            "FROM deliveries d "
            "JOIN terminal_runs tr ON tr.state='live' AND tr.token_revoked_at IS NULL "
            "AND ((d.terminal_run_id IS NOT NULL AND tr.id=d.terminal_run_id) "
            "OR (d.terminal_run_id IS NULL AND tr.actor_slug=d.actor_slug "
            "AND tr.purpose_kind='persistent' AND tr.purpose_id=d.actor_slug)) WHERE d.id=?",
            (delivery_id,),
        ).fetchone()
        if row is None or not row["backend_run_id"] or not row["backend_terminal_id"]:
            return
        nonce = secrets.token_urlsafe(12)
        body = f"AGENTS_WAKE {delivery_id} {nonce}; call inbox, process messages in ID order, then ack_inbox"
        result = "accepted"
        error = None
        backend_id = None
        delay = 300
        handle = RunHandle(
            str(row["execution_name"]),
            str(row["backend_run_id"]),
            str(row["backend_terminal_id"]),
        )
        try:
            backend_id = await asyncio.to_thread(
                self.backend.send_message,
                handle,
                str(row["terminal_run_id"]),
                body,
            )
        except (ExecutionUnavailable, ExecutionBusy) as exc:
            result = "failed"
            error = str(exc)
            delay = _RETRY[min(int(row["attempts"]), len(_RETRY) - 1)]
        except ExecutionTimeout as exc:
            result = "uncertain"
            error = str(exc)
            if exc.outcome_unknown:
                live_run = self.connection.execute(
                    "SELECT * FROM terminal_runs WHERE id=?",
                    (row["terminal_run_id"],),
                ).fetchone()
                if live_run is not None:
                    self._recover_terminal(
                        live_run,
                        f"wake outcome unknown: {exc}",
                        terminal_state="ending",
                        incident_kind="wake_delivery_uncertain",
                    )
            else:
                delay = _RETRY[min(int(row["attempts"]), len(_RETRY) - 1)]
        except ExecutionError as exc:
            result = "failed"
            error = str(exc)
            self._incident("wake_delivery_failed", "delivery", str(delivery_id), str(exc))
        now = datetime.now(UTC)
        self.connection.execute(
            "INSERT INTO wake_attempts(delivery_id,terminal_run_id,nonce,backend_message_id,result,error,created_at)"
            "VALUES(?,?,?,?,?,?,?)",
            (delivery_id, row["terminal_run_id"], nonce, backend_id, result, error, utc_now()),
        )
        self.connection.execute(
            "UPDATE deliveries SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE id=?",
            ((now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z"), error, delivery_id),
        )
        if int(row["attempts"]) + 1 == 5:
            self._incident("wake_delivery_failed", "delivery", str(delivery_id), "Wake delivery has failed five times")

    async def _send_input(self, input_id: int) -> None:
        row = self.connection.execute(
            "SELECT ti.*,tr.execution_name,tr.backend_run_id,tr.backend_terminal_id "
            "FROM terminal_inputs ti JOIN terminal_runs tr ON tr.id=ti.terminal_run_id WHERE ti.id=?",
            (input_id,),
        ).fetchone()
        if row is None or not row["backend_run_id"] or not row["backend_terminal_id"]:
            return
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_inputs SET state='sending',updated_at=? WHERE id=? AND state='pending'",
            (now, input_id),
        )
        handle = RunHandle(
            str(row["execution_name"]),
            str(row["backend_run_id"]),
            str(row["backend_terminal_id"]),
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.backend.send_input, handle, str(row["body"])),
                timeout=10,
            )
            state = "sent"
            error = None
        except (ExecutionUnavailable, ExecutionBusy) as exc:
            state = "pending"
            error = str(exc)
        except BaseException as exc:
            state = "uncertain"
            error = str(exc)
        self.connection.execute(
            "UPDATE terminal_inputs SET state=?,error=?,updated_at=? WHERE id=?",
            (state, error, utc_now(), input_id),
        )
        if state == "uncertain":
            self._incident(
                "terminal_input_uncertain",
                "terminal_input",
                str(input_id),
                "Terminal input outcome is uncertain",
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
            "Mapped backend run disappeared",
            blocker_kind="missing_session",
            blocker_reason="mapped backend run disappeared",
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
                        remove_recorded_workspace(self.config, self.config.project.path, path, head_sha(path))
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
                        remove_recorded_workspace(
                            self.config,
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
                        remove_recorded_workspace(
                            self.config,
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
