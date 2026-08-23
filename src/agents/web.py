from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    AgentContext,
    AuthenticationError,
    HumanSession,
    authenticate_agent,
    constant_time_token,
    issue_human_session,
    read_agent_auth_key,
    read_private_secret,
    verify_human_session,
)
from .config import AgentsConfig, load
from .db import MutationConflict, canonical_json, connect, migrate, mutation, utc_now
from .delivery import Delivery
from .messages import Messages, Messaging
from .policy import DomainError, validate_request_id, validate_text
from .reconciler import Reconciler
from .service import acquire_daemon_lock
from .store import Store
from .workflow import Workflow

CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def ok(data: Any, version: int | None = None) -> dict[str, Any]:
    value = {"ok": True, "data": data}
    if version is not None:
        value["version"] = version
    return value


def error(code: str, message: str, current: Any | None = None) -> dict[str, Any]:
    detail = {"code": code, "message": message}
    if current is not None:
        detail["current"] = current
    return {"ok": False, "error": detail}


def _web_token(config: AgentsConfig) -> str:
    return os.environ.get("AGENTS_WEB_TOKEN") or read_private_secret(config.state_dir / "web-token")


def _request_id(request: Request, provided: str | None = None) -> str:
    value = provided if provided is not None else request.headers.get("Idempotency-Key")
    if value is None:
        raise HTTPException(400, detail=error("missing_idempotency_key", "Idempotency-Key header is required"))
    try:
        return validate_request_id(value)
    except DomainError as exc:
        raise HTTPException(400, detail=error(exc.code, exc.message)) from exc


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, detail=error("malformed_json", "request body must be valid JSON")) from exc
    if not isinstance(body, dict):
        raise HTTPException(400, detail=error("malformed_json", "request body must be a JSON object"))
    return body


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin != expected:
        raise HTTPException(403, detail=error("invalid_origin", "request must be exact same-origin"))


def _human(request: Request) -> HumanSession:
    try:
        return verify_human_session(request.cookies.get("agents_session", ""), _web_token(request.app.state.config))
    except AuthenticationError as exc:
        raise HTTPException(401, detail=error("unauthenticated", str(exc))) from exc


def _human_mutation(
    request: Request,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> HumanSession:
    session = _human(request)
    _same_origin(request)
    if csrf is None or not constant_time_token(csrf, session.csrf):
        raise HTTPException(403, detail=error("invalid_csrf", "CSRF token mismatch"))
    _request_id(request)
    return session


def _agent(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    execution_id: Annotated[str | None, Header(alias="X-Agents-Execution-ID")] = None,
) -> AgentContext:
    if not authorization or not authorization.startswith("Bearer ") or not execution_id:
        raise HTTPException(401, detail=error("unauthenticated", "agent credentials required"))
    connection: sqlite3.Connection = request.app.state.connection
    project = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
    try:
        key = getattr(request.app.state, "agent_auth_key", None)
        if key is None:
            key = read_agent_auth_key(request.app.state.config.state_dir / "agent-auth-key")
        return authenticate_agent(
            connection,
            execution_id,
            authorization.removeprefix("Bearer "),
            key,
            str(project[0]),
        )
    except (AuthenticationError, ValueError) as exc:
        raise HTTPException(401, detail=error("unauthenticated", str(exc))) from exc


HumanRead = Annotated[HumanSession, Depends(_human)]
HumanMutation = Annotated[HumanSession, Depends(_human_mutation)]
AgentAuth = Annotated[AgentContext, Depends(_agent)]


def _domain(call):
    try:
        return call()
    except DomainError as exc:
        status = {
            "unauthorized": 403,
            "not_found": 404,
            "stale_version": 409,
            "scope_frozen": 409,
            "invalid_state": 409,
            "not_ready": 422,
            "validation_failed": 422,
        }.get(exc.code, 409)
        raise HTTPException(status, detail=error(exc.code, exc.message, exc.current)) from exc
    except MutationConflict as exc:
        raise HTTPException(
            409,
            detail=error("idempotency_conflict", str(exc)),
        ) from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load()
    agent_auth_key = read_agent_auth_key(config.state_dir / "agent-auth-key")
    lock = acquire_daemon_lock(config.state_dir)
    connection = connect(config.state_dir / "agents.db")
    migrate(connection)
    Store(connection).initialize(config)
    app.state.config = config
    app.state.connection = connection
    app.state.agent_auth_key = agent_auth_key
    reconciler_task = asyncio.create_task(Reconciler(config, connection).run())
    try:
        yield
    finally:
        reconciler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconciler_task
        connection.close()
        lock.close()


def create_app(config: AgentsConfig | None = None, connection: sqlite3.Connection | None = None) -> FastAPI:
    app = FastAPI(
        lifespan=None if config is not None else lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")
    if config is not None:
        app.state.config = config
        app.state.connection = connection
        app.state.agent_auth_key = read_agent_auth_key(config.state_dir / "agent-auth-key")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        detail = (
            exc.detail
            if isinstance(exc.detail, dict) and exc.detail.get("ok") is False
            else error("http_error", str(exc.detail))
        )
        return JSONResponse(status_code=exc.status_code, content=detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content=error("malformed_request", str(exc.errors())))

    @app.exception_handler(json.JSONDecodeError)
    async def json_error(_: Request, __: json.JSONDecodeError):
        return JSONResponse(status_code=400, content=error("malformed_json", "request body must be valid JSON"))

    @app.exception_handler(KeyError)
    async def missing_field(_: Request, exc: KeyError):
        return JSONResponse(status_code=400, content=error("missing_field", f"missing field: {exc.args[0]}"))

    @app.exception_handler(TypeError)
    async def malformed_type(_: Request, __: TypeError):
        return JSONResponse(status_code=400, content=error("malformed_request", "request body has invalid fields"))

    @app.exception_handler(ValueError)
    async def malformed_value(_: Request, __: ValueError):
        return JSONResponse(status_code=400, content=error("malformed_request", "request body has invalid fields"))

    @app.middleware("http")
    async def security(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "agentsd"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agents login</title>'
            '<link rel="stylesheet" href="/static/styles.css"></head><body><main><h1>Agents</h1>'
            '<form method="post" action="/auth/login"><label>Web token'
            '<input name="token" type="password" required autofocus></label><button>Sign in</button>'
            "</form></main></body></html>"
        )

    @app.post("/auth/login")
    async def login(request: Request, token: str = Form(...)):
        _same_origin(request)
        expected = _web_token(request.app.state.config)
        if not constant_time_token(token, expected):
            raise HTTPException(401, detail=error("unauthenticated", "invalid web token"))
        cookie, session = issue_human_session(expected)
        response = RedirectResponse("/", 303)
        secure = request.url.scheme == "https"
        response.set_cookie("agents_session", cookie, max_age=43200, httponly=True, samesite="strict", secure=secure)
        response.set_cookie(
            "agents_csrf", session.csrf, max_age=43200, httponly=False, samesite="strict", secure=secure
        )
        return response

    @app.post("/auth/logout")
    async def logout(request: Request, _: HumanMutation):
        response = JSONResponse(ok({"logged_out": True}))
        response.delete_cookie("agents_session")
        response.delete_cookie("agents_csrf")
        return response

    @app.get("/")
    async def index(request: Request):
        try:
            _human(request)
        except HTTPException:
            return RedirectResponse("/login", 303)
        return FileResponse(static / "index.html")

    @app.get("/api/v1/snapshot")
    async def snapshot(request: Request, _: HumanRead):
        connection = request.app.state.connection
        connection.execute("BEGIN")
        try:
            default = connection.execute("SELECT id,address FROM conversations WHERE address='#findings'").fetchone()
            messages = (
                [
                    dict(row)
                    for row in connection.execute(
                        "SELECT m.* FROM messages m WHERE conversation_id=? ORDER BY id DESC LIMIT 50",
                        (default["id"],),
                    )
                ]
                if default is not None
                else []
            )
            data = {
                "roster": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT a.*,tr.id terminal_run_id,tr.status terminal_status,tr.state terminal_state,"
                        "tr.purpose_kind terminal_purpose_kind,tr.purpose_id terminal_purpose_id "
                        "FROM actors a LEFT JOIN terminal_runs tr ON tr.actor_slug=a.slug "
                        "AND tr.state IN ('reserved','creating','live','retained') ORDER BY a.slug"
                    )
                ],
                "board": [dict(row) for row in connection.execute("SELECT * FROM work_items ORDER BY seq LIMIT 100")],
                "consultations": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM consultations WHERE state IN ('queued','assigned') ORDER BY id LIMIT 100"
                    )
                ],
                "decisions": [
                    dict(row) for row in connection.execute("SELECT * FROM decisions WHERE state='open' LIMIT 100")
                ],
                "blockers": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM blockers WHERE state IN ('open','escalated') LIMIT 100"
                    )
                ],
                "approvals": [
                    dict(row) for row in connection.execute("SELECT * FROM approvals WHERE state='pending' LIMIT 100")
                ],
                "incidents": [
                    dict(row) for row in connection.execute("SELECT * FROM incidents WHERE state='open' LIMIT 100")
                ],
                "conversations": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT c.*,MAX(m.id) last_message_id FROM conversations c "
                        "LEFT JOIN messages m ON m.conversation_id=c.id GROUP BY c.id ORDER BY c.address LIMIT 100"
                    )
                ],
                "default_conversation": dict(default) if default is not None else None,
                "messages": messages,
                "event_high_water": connection.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0],
            }
            connection.commit()
            return ok(data)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    @app.get("/agent/v1/health")
    async def agent_health(context: AgentAuth):
        return ok({"actor": context.actor_slug, "purpose_kind": context.purpose_kind, "purpose_id": context.purpose_id})

    @app.get("/agent/v1/backlog")
    async def backlog(
        request: Request,
        context: AgentAuth,
        status: str = "",
        limit: int = 50,
        after_id: str = "",
    ):
        if not 1 <= limit <= 100:
            raise HTTPException(422, detail=error("validation_failed", "limit must be 1..100"))
        connection = request.app.state.connection
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            clauses.append("status=?")
            args.append(status)
        if after_id:
            cursor = connection.execute("SELECT seq FROM work_items WHERE id=?", (after_id,)).fetchone()
            if cursor is None:
                raise HTTPException(404, detail=error("not_found", "after_id work item does not exist"))
            clauses.append("seq>?")
            args.append(cursor["seq"])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        return ok(
            [dict(row) for row in connection.execute(f"SELECT * FROM work_items{where} ORDER BY seq LIMIT ?", args)]
        )

    @app.get("/agent/v1/backlog/{item_id}")
    async def backlog_get(request: Request, item_id: str, context: AgentAuth):
        return ok(dict(_domain(lambda: Store(request.app.state.connection).get_work(item_id))))

    @app.post("/agent/v1/backlog")
    async def backlog_create(request: Request, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: Workflow(request.app.state.connection).create_work(
                body["request_id"],
                context.actor_slug,
                parent_id=body.get("parent_id"),
                kind=body["kind"],
                title=body["title"],
                problem=body["problem"],
                outcome=body["outcome"],
            )
        )
        return ok(data, data["version"])

    @app.post("/agent/v1/backlog/{item_id}/start-refinement")
    async def start_refinement(request: Request, item_id: str, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: Workflow(request.app.state.connection).start_refinement(
                body["request_id"], context.actor_slug, item_id, body["expected_version"]
            )
        )
        return ok(data, data["version"])

    @app.put("/agent/v1/backlog/{item_id}")
    async def refine(request: Request, item_id: str, context: AgentAuth):
        body = await _json_body(request)
        scope = {
            "kind": body["kind"],
            "title": body["title"],
            "problem": body["problem"],
            "outcome": body["outcome"],
            "priority": body["priority"],
            "specialty": body["specialty"],
            "criteria": body["acceptance_criteria"],
            "dependencies": body["dependencies"],
            "gates": body["review_gates"],
        }
        data = _domain(
            lambda: Workflow(request.app.state.connection).refine(
                body["request_id"], context.actor_slug, item_id, body["expected_version"], **scope
            )
        )
        return ok(data, data["version"])

    for action in ("ready", "reopen", "cancel"):

        async def mutate(request: Request, item_id: str, context: AgentAuth, action_name: str = action):
            body = await _json_body(request)
            workflow = Workflow(request.app.state.connection)
            if action_name == "ready":
                data = _domain(
                    lambda: workflow.mark_ready(
                        body["request_id"], context.actor_slug, item_id, body["expected_version"]
                    )
                )
            elif action_name == "reopen":
                data = _domain(
                    lambda: workflow.reopen(
                        body["request_id"], context.actor_slug, item_id, body["expected_version"], body["reason"]
                    )
                )
            else:
                data = _domain(
                    lambda: workflow.cancel(
                        body["request_id"], context.actor_slug, item_id, body["expected_version"], body["reason"]
                    )
                )
            return ok(data, data["version"])

        app.add_api_route(f"/agent/v1/backlog/{{item_id}}/{action}", mutate, methods=["POST"], name=f"agent_{action}")

    @app.post("/agent/v1/messages")
    async def agent_post_message(request: Request, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: Messaging(request.app.state.connection).post(
                body["request_id"],
                context.actor_slug,
                body["to"],
                body["body"],
                body.get("reply_to"),
                body.get("urgency", "normal"),
            )
        )
        return ok(data)

    @app.get("/agent/v1/inbox")
    async def agent_inbox(request: Request, context: AgentAuth, limit: int = 50):
        return ok(
            _domain(
                lambda: Messages(request.app.state.connection).inbox(
                    context.actor_slug, context.terminal_run_id, context.persistent, limit
                )
            )
        )

    @app.post("/agent/v1/inbox/ack")
    async def agent_ack(request: Request, context: AgentAuth):
        body = await _json_body(request)
        return ok(
            _domain(
                lambda: Messaging(request.app.state.connection).ack(
                    body["request_id"],
                    context.actor_slug,
                    context.terminal_run_id,
                    context.persistent,
                    body["message_ids"],
                )
            )
        )

    @app.get("/agent/v1/conversations/history")
    async def agent_history(
        request: Request,
        context: AgentAuth,
        address: str,
        before_id: int | None = None,
        limit: int = 50,
    ):
        return ok(
            _domain(
                lambda: Messages(request.app.state.connection).history(context.actor_slug, address, before_id, limit)
            )
        )

    @app.get("/agent/v1/assignment")
    async def agent_assignment(request: Request, context: AgentAuth):
        connection = request.app.state.connection
        assignment = connection.execute(
            "SELECT a.*,e.worktree_path,e.branch,e.base_sha FROM assignments a "
            "JOIN executions e ON e.id=a.execution_id "
            "WHERE a.terminal_run_id=? AND a.actor_slug=? AND a.state='open'",
            (context.terminal_run_id, context.actor_slug),
        ).fetchone()
        consultation = connection.execute(
            "SELECT * FROM consultations WHERE terminal_run_id=? AND responder=? AND state='assigned'",
            (context.terminal_run_id, context.actor_slug),
        ).fetchone()
        review = connection.execute(
            "SELECT * FROM reviews WHERE terminal_run_id=? AND actor_slug=? AND verdict='pending'",
            (context.terminal_run_id, context.actor_slug),
        ).fetchone()
        return ok(
            {
                "purpose_kind": context.purpose_kind,
                "purpose_id": context.purpose_id,
                "assignment": dict(assignment) if assignment else None,
                "consultation": dict(consultation) if consultation else None,
                "review": dict(review) if review else None,
            }
        )

    def active_work_participant(connection: sqlite3.Connection, context: AgentContext, item_id: str) -> sqlite3.Row:
        work = connection.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        if work is None:
            raise DomainError("not_found", f"work item {item_id} does not exist")
        participant = connection.execute(
            "SELECT 1 FROM assignments WHERE work_id=? AND actor_slug=? AND terminal_run_id=? AND state='open' "
            "UNION SELECT 1 FROM consultations WHERE work_id=? AND responder=? AND terminal_run_id=? AND state='assigned' "
            "UNION SELECT 1 FROM reviews r JOIN submissions s ON s.id=r.submission_id "
            "JOIN executions e ON e.id=s.execution_id "
            "WHERE e.work_id=? AND r.actor_slug=? AND r.terminal_run_id=? AND r.verdict='pending'",
            (
                item_id,
                context.actor_slug,
                context.terminal_run_id,
                item_id,
                context.actor_slug,
                context.terminal_run_id,
                item_id,
                context.actor_slug,
                context.terminal_run_id,
            ),
        ).fetchone()
        if participant is None:
            raise DomainError("unauthorized", "agent has no active assignment for this work item")
        return work

    def agent_work_mutation(
        request: Request,
        context: AgentContext,
        body: dict[str, Any],
        item_id: str,
        kind: str,
        call,
    ) -> dict[str, Any]:
        request_id = body["request_id"]
        request_body = {**body, "item_id": item_id}
        digest = hashlib.sha256(canonical_json(request_body).encode()).hexdigest()
        return mutation(
            request.app.state.connection,
            f"agent:{context.actor_slug}",
            request_id,
            kind,
            f"work:{item_id}",
            digest,
            lambda connection: call(connection),
        )

    def record_progress(
        connection: sqlite3.Connection,
        context: AgentContext,
        item_id: str,
        expected_version: int,
        summary: str,
    ) -> dict[str, Any]:
        work = active_work_participant(connection, context, item_id)
        if int(work["version"]) != expected_version:
            raise DomainError("stale_version", "work item version changed", dict(work))
        if str(work["status"]) in {"delivered", "cancelled", "blocked"}:
            raise DomainError("invalid_state", "work item cannot receive progress in its current state")
        validate_text(summary, "summary")
        now = utc_now()
        version = expected_version + 1
        connection.execute(
            "UPDATE work_items SET version=?,updated_at=? WHERE id=? AND version=?",
            (version, now, item_id, expected_version),
        )
        return {"id": item_id, "status": work["status"], "summary": summary, "version": version}

    def record_block(
        connection: sqlite3.Connection,
        context: AgentContext,
        item_id: str,
        expected_version: int,
        reason: str,
        requested_role: str,
    ) -> dict[str, Any]:
        work = active_work_participant(connection, context, item_id)
        if int(work["version"]) != expected_version:
            raise DomainError("stale_version", "work item version changed", dict(work))
        if str(work["status"]) in {"delivered", "cancelled", "blocked"}:
            raise DomainError("invalid_state", "work item cannot be blocked in its current state")
        validate_text(reason, "reason")
        validate_text(requested_role, "requested_role", 1, 200)
        current = str(work["status"])
        now = utc_now()
        cursor = connection.execute(
            "INSERT INTO blockers(work_id,target_kind,target_id,terminal_run_id,kind,reason,requested_role,actor_slug,"
            "resume_state,state,created_at,updated_at) VALUES(?, 'work', ?, ?, 'agent_blocked', ?, ?, ?, ?, 'open', ?, ?)",
            (item_id, item_id, context.terminal_run_id, reason, requested_role, context.actor_slug, current, now, now),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not allocate a blocker ID")
        version = expected_version + 1
        connection.execute(
            "UPDATE work_items SET status='blocked',blocked_from=?,version=?,updated_at=? WHERE id=? AND version=?",
            (current, version, now, item_id, expected_version),
        )
        return {"id": item_id, "blocker_id": cursor.lastrowid, "state": "blocked", "version": version}

    @app.post("/agent/v1/backlog/{item_id}/progress")
    async def agent_report_progress(request: Request, item_id: str, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: agent_work_mutation(
                request,
                context,
                body,
                item_id,
                "work.progress",
                lambda connection: record_progress(
                    connection, context, item_id, body["expected_version"], body["summary"]
                ),
            )
        )
        return ok(data, data["version"])

    @app.post("/agent/v1/backlog/{item_id}/block")
    async def agent_block_work(request: Request, item_id: str, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: agent_work_mutation(
                request,
                context,
                body,
                item_id,
                "blocker.created",
                lambda connection: record_block(
                    connection,
                    context,
                    item_id,
                    body["expected_version"],
                    body["reason"],
                    body["requested_role"],
                ),
            )
        )
        return ok(data, data["version"])

    def agent_delivery(request: Request, context: AgentContext, body: dict[str, Any], kind: str, entity: str, call):
        request_id = body["request_id"]
        digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()
        return mutation(
            request.app.state.connection,
            f"agent:{context.actor_slug}",
            request_id,
            kind,
            entity,
            digest,
            lambda _: call(Delivery(request.app.state.config, request.app.state.connection)),
        )

    @app.post("/agent/v1/backlog/{item_id}/consultations")
    async def agent_request_consultation(request: Request, item_id: str, context: AgentAuth):
        body = await _json_body(request)
        return ok(
            _domain(
                lambda: agent_delivery(
                    request,
                    context,
                    body,
                    "consultation.requested",
                    f"work:{item_id}",
                    lambda delivery: delivery.request_consultation(
                        context.actor_slug,
                        item_id,
                        body["expected_version"],
                        body["specialty"],
                        body["question"],
                    ),
                )
            )
        )

    @app.post("/agent/v1/consultations/{consultation_id}/submit")
    async def agent_submit_consultation(request: Request, consultation_id: int, context: AgentAuth):
        body = await _json_body(request)
        return ok(
            _domain(
                lambda: agent_delivery(
                    request,
                    context,
                    body,
                    "consultation.completed",
                    f"consultation:{consultation_id}",
                    lambda delivery: delivery.submit_consultation(
                        context.actor_slug,
                        consultation_id,
                        body["expected_version"],
                        body["response"],
                        context.terminal_run_id,
                    ),
                )
            )
        )

    @app.post("/agent/v1/decisions")
    async def agent_propose_decision(request: Request, context: AgentAuth):
        body = await _json_body(request)
        return ok(
            _domain(
                lambda: agent_delivery(
                    request,
                    context,
                    body,
                    "decision.proposed",
                    f"work:{body.get('item_id') or 'new'}",
                    lambda delivery: delivery.propose_decision(
                        context.actor_slug,
                        item_id=body.get("item_id"),
                        title=body["title"],
                        question=body["question"],
                        options=body["options"],
                        recommendation=body["recommendation"],
                    ),
                )
            )
        )

    def resolve_linked_blocker(
        delivery: Delivery,
        context: AgentContext,
        blocker_id: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        blocker = delivery.connection.execute(
            "SELECT * FROM blockers WHERE id=? AND state IN ('open','escalated')", (blocker_id,)
        ).fetchone()
        if blocker is None:
            raise DomainError("not_found", "open blocker does not exist")
        linked_work_id: str | None = None
        expected_version: int | None = None
        if blocker["work_id"] is not None:
            item_id = body["item_id"]
            expected_version = int(body["expected_version"])
            if item_id != blocker["work_id"]:
                raise DomainError("stale_version", "blocker is linked to a different work item")
            linked_work_id = str(item_id)
            work = Store(delivery.connection).get_work(linked_work_id)
            if int(work["version"]) != expected_version:
                raise DomainError("stale_version", "work item version changed", dict(work))
        result = delivery.resolve_blocker(context.actor_slug, blocker_id, body["resolution"], body["action"])
        if linked_work_id is not None and expected_version is not None:
            current = delivery.connection.execute(
                "SELECT version FROM work_items WHERE id=?", (linked_work_id,)
            ).fetchone()
            if current is not None and int(current["version"]) == expected_version:
                delivery.connection.execute(
                    "UPDATE work_items SET version=version+1,updated_at=? WHERE id=?",
                    (utc_now(), linked_work_id),
                )
            result["version"] = expected_version + 1
        return result

    @app.post("/agent/v1/blockers/{blocker_id}/resolve")
    async def agent_resolve_blocker(request: Request, blocker_id: int, context: AgentAuth):
        body = await _json_body(request)
        entity = f"work:{body['item_id']}" if body.get("item_id") else f"blocker:{blocker_id}"
        return ok(
            _domain(
                lambda: agent_delivery(
                    request,
                    context,
                    body,
                    "blocker.resolved",
                    entity,
                    lambda delivery: resolve_linked_blocker(delivery, context, blocker_id, body),
                )
            )
        )

    @app.post("/agent/v1/backlog/{item_id}/submit")
    async def agent_submit_work(request: Request, item_id: str, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: agent_delivery(
                request,
                context,
                body,
                "work.submitted",
                f"work:{item_id}",
                lambda delivery: delivery.submit_work(
                    context.actor_slug,
                    item_id,
                    body["expected_version"],
                    body["commit_sha"],
                    body["summary"],
                    context.terminal_run_id,
                ),
            )
        )
        return ok(data, data["version"])

    @app.post("/agent/v1/reviews/{submission_id}/{gate}")
    async def agent_submit_review(request: Request, submission_id: int, gate: str, context: AgentAuth):
        body = await _json_body(request)
        data = _domain(
            lambda: agent_delivery(
                request,
                context,
                body,
                "review.submitted",
                f"work:{body['item_id']}",
                lambda delivery: delivery.submit_review(
                    context.actor_slug,
                    body["item_id"],
                    submission_id,
                    body["expected_version"],
                    gate,
                    body["verdict"],
                    body["body"],
                    context.terminal_run_id,
                ),
            )
        )
        return ok(data, data["version"])

    @app.get("/api/v1/events")
    async def events(
        request: Request,
        _: HumanRead,
        after: int = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        cursor = max(after, int(last_event_id or 0))

        async def stream():
            nonlocal cursor
            heartbeats = 0
            while not await request.is_disconnected():
                rows = list(
                    request.app.state.connection.execute(
                        "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (cursor,)
                    )
                )
                for row in rows:
                    cursor = int(row["id"])
                    payload = json.dumps(dict(row), separators=(",", ":"))
                    yield f"id: {cursor}\nevent: agents\ndata: {payload}\n\n"
                heartbeats += 1
                if not rows and heartbeats % 15 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/v1/search")
    async def search(request: Request, _: HumanRead, query: str, after_id: int = 0, limit: int = 50):
        if not 1 <= len(query.encode()) <= 1024 or not 1 <= limit <= 100:
            raise HTTPException(422, detail=error("validation_failed", "invalid search query or limit"))
        rows = request.app.state.connection.execute(
            "SELECT m.*,c.address FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "JOIN conversation_members cm ON cm.conversation_id=c.id AND cm.actor_slug='human' "
            "WHERE m.id>? AND m.body LIKE ? ORDER BY m.id LIMIT ?",
            (after_id, f"%{query}%", limit),
        )
        return ok([dict(row) for row in rows])

    @app.get("/api/v1/conversations/{conversation_id}/messages")
    async def conversation_messages(
        request: Request,
        conversation_id: int,
        _: HumanRead,
        before_id: int | None = None,
        limit: int = 50,
    ):
        if not 1 <= limit <= 100:
            raise HTTPException(422, detail=error("validation_failed", "limit must be 1..100"))
        if (
            request.app.state.connection.execute(
                "SELECT 1 FROM conversation_members WHERE conversation_id=? AND actor_slug='human'",
                (conversation_id,),
            ).fetchone()
            is None
        ):
            raise HTTPException(404, detail=error("not_found", "conversation does not exist"))
        sql = "SELECT * FROM messages WHERE conversation_id=?"
        args: list[Any] = [conversation_id]
        if before_id is not None:
            sql += " AND id<?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return ok([dict(row) for row in request.app.state.connection.execute(sql, args)])

    @app.get("/api/v1/work/{item_id}")
    async def human_work(request: Request, item_id: str, _: HumanRead):
        connection = request.app.state.connection
        work = _domain(lambda: Store(connection).get_work(item_id))
        return ok(
            {
                "work": dict(work),
                "criteria": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM acceptance_criteria WHERE work_id=? ORDER BY position", (item_id,)
                    )
                ],
                "dependencies": [
                    row[0]
                    for row in connection.execute("SELECT depends_on_id FROM dependencies WHERE work_id=?", (item_id,))
                ],
                "consultations": [
                    dict(row) for row in connection.execute("SELECT * FROM consultations WHERE work_id=?", (item_id,))
                ],
                "decisions": [
                    dict(row) for row in connection.execute("SELECT * FROM decisions WHERE work_id=?", (item_id,))
                ],
                "executions": [
                    dict(row) for row in connection.execute("SELECT * FROM executions WHERE work_id=?", (item_id,))
                ],
                "submissions": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT s.* FROM submissions s JOIN executions e ON e.id=s.execution_id WHERE e.work_id=?",
                        (item_id,),
                    )
                ],
                "checks": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT c.* FROM checks c JOIN submissions s ON s.id=c.submission_id "
                        "JOIN executions e ON e.id=s.execution_id WHERE e.work_id=? ORDER BY c.id",
                        (item_id,),
                    )
                ],
                "reviews": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT r.* FROM reviews r JOIN submissions s ON s.id=r.submission_id "
                        "JOIN executions e ON e.id=s.execution_id WHERE e.work_id=? ORDER BY r.id",
                        (item_id,),
                    )
                ],
                "blockers": [
                    dict(row) for row in connection.execute("SELECT * FROM blockers WHERE work_id=?", (item_id,))
                ],
            }
        )

    @app.post("/api/v1/messages")
    async def human_post_message(
        request: Request,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)
        return ok(
            _domain(
                lambda: Messaging(request.app.state.connection).post(
                    idempotency_key,
                    "human",
                    body["to"],
                    body["body"],
                    body.get("reply_to"),
                    body.get("urgency", "normal"),
                )
            )
        )

    @app.post("/api/v1/intake")
    async def human_intake(
        request: Request,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)
        data = _domain(
            lambda: Workflow(request.app.state.connection).create_work(
                idempotency_key,
                "human",
                parent_id=body.get("parent_id"),
                kind=body["kind"],
                title=body["title"],
                problem=body["problem"],
                outcome=body["outcome"],
            )
        )
        return ok(data, data["version"])

    @app.post("/api/v1/work/{item_id}/start-refinement")
    async def human_start_refinement(
        request: Request,
        item_id: str,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)
        data = _domain(
            lambda: Workflow(request.app.state.connection).start_refinement(
                idempotency_key, "human", item_id, body["expected_version"]
            )
        )
        return ok(data, data["version"])

    @app.put("/api/v1/work/{item_id}/refine")
    async def human_refine(
        request: Request,
        item_id: str,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)
        data = _domain(
            lambda: Workflow(request.app.state.connection).refine(
                idempotency_key,
                "human",
                item_id,
                body["expected_version"],
                kind=body["kind"],
                title=body["title"],
                problem=body["problem"],
                outcome=body["outcome"],
                priority=body["priority"],
                specialty=body["specialty"],
                criteria=body["acceptance_criteria"],
                dependencies=body["dependencies"],
                gates=body["review_gates"],
            )
        )
        return ok(data, data["version"])

    for human_action in ("ready", "reopen", "cancel"):

        async def human_work_action(
            request: Request,
            item_id: str,
            _: HumanMutation,
            idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
            action_name: str = human_action,
        ):
            idempotency_key = _request_id(request, idempotency_key)
            body = await _json_body(request)
            workflow = Workflow(request.app.state.connection)
            if action_name == "ready":
                data = _domain(lambda: workflow.mark_ready(idempotency_key, "human", item_id, body["expected_version"]))
            elif action_name == "reopen":
                data = _domain(
                    lambda: workflow.reopen(idempotency_key, "human", item_id, body["expected_version"], body["reason"])
                )
            else:
                data = _domain(
                    lambda: workflow.cancel(idempotency_key, "human", item_id, body["expected_version"], body["reason"])
                )
            return ok(data, data["version"])

        app.add_api_route(
            f"/api/v1/work/{{item_id}}/{human_action}",
            human_work_action,
            methods=["POST"],
            name=f"human_{human_action}",
        )

    for approval_action in ("accept", "reject"):

        async def human_approval(
            request: Request,
            item_id: str,
            _: HumanMutation,
            idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
            action_name: str = approval_action,
        ):
            idempotency_key = _request_id(request, idempotency_key)
            body = await _json_body(request)
            accept = action_name == "accept"
            payload = {
                "item_id": item_id,
                "expected_version": body.get("expected_version"),
                "feedback": body.get("feedback", ""),
            }
            digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
            data = _domain(
                lambda: mutation(
                    request.app.state.connection,
                    "human",
                    idempotency_key,
                    f"approval.{action_name}ed",
                    f"work:{item_id}",
                    digest,
                    lambda connection: Delivery(request.app.state.config, connection).decide_approval(
                        item_id,
                        body["expected_version"],
                        accept,
                        body.get("feedback", ""),
                    ),
                )
            )
            return ok(data, data["version"])

        app.add_api_route(
            f"/api/v1/work/{{item_id}}/{approval_action}",
            human_approval,
            methods=["POST"],
            name=f"human_{approval_action}",
        )

    @app.post("/api/v1/decisions/{decision_id}/resolve")
    async def human_decision(
        request: Request,
        decision_id: int,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)

        def resolve(connection: sqlite3.Connection) -> dict[str, Any]:
            decision = connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if decision is None:
                raise DomainError("not_found", "decision does not exist")
            linked = decision["work_id"] is not None
            if linked and ("item_id" not in body or "expected_version" not in body):
                raise HTTPException(
                    400, detail=error("missing_field", "linked decision requires item_id and expected_version")
                )
            result = Delivery(request.app.state.config, connection).resolve_decision(
                decision_id,
                body.get("item_id"),
                body.get("expected_version"),
                body["resolution"],
            )
            if linked:
                result["version"] = int(body["expected_version"]) + 1
            return result

        payload = {"decision_id": decision_id, **body}
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        entity = f"work:{body['item_id']}" if body.get("item_id") else f"decision:{decision_id}"
        data = _domain(
            lambda: mutation(
                request.app.state.connection,
                "human",
                idempotency_key,
                "decision.resolved",
                entity,
                digest,
                resolve,
            )
        )
        return ok(data, data.get("version") if isinstance(data, dict) else None)

    @app.post("/api/v1/blockers/{blocker_id}/resolve")
    async def human_blocker(
        request: Request,
        blocker_id: int,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)

        def resolve(connection: sqlite3.Connection) -> dict[str, Any]:
            blocker = connection.execute(
                "SELECT * FROM blockers WHERE id=? AND state IN ('open','escalated')", (blocker_id,)
            ).fetchone()
            if blocker is None:
                raise DomainError("not_found", "open blocker does not exist")
            linked_work_id: str | None = None
            expected_version: int | None = None
            if blocker["work_id"] is not None:
                if "item_id" not in body or "expected_version" not in body:
                    raise HTTPException(
                        400, detail=error("missing_field", "linked blocker requires item_id and expected_version")
                    )
                if body["item_id"] != blocker["work_id"]:
                    raise DomainError("stale_version", "blocker is linked to a different work item")
                linked_work_id = str(body["item_id"])
                expected_version = int(body["expected_version"])
                work = Store(connection).get_work(linked_work_id)
                if int(work["version"]) != expected_version:
                    raise DomainError("stale_version", "work item version changed", dict(work))
            result = Delivery(request.app.state.config, connection).resolve_blocker(
                "human", blocker_id, body["resolution"], body["action"]
            )
            if linked_work_id is not None and expected_version is not None:
                current = connection.execute("SELECT version FROM work_items WHERE id=?", (linked_work_id,)).fetchone()
                if current is not None and int(current["version"]) == expected_version:
                    connection.execute(
                        "UPDATE work_items SET version=version+1,updated_at=? WHERE id=?",
                        (utc_now(), linked_work_id),
                    )
                result["version"] = expected_version + 1
            return result

        payload = {"blocker_id": blocker_id, **body}
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        entity = f"work:{body['item_id']}" if body.get("item_id") else f"blocker:{blocker_id}"
        data = _domain(
            lambda: mutation(
                request.app.state.connection,
                "human",
                idempotency_key,
                "blocker.resolved",
                entity,
                digest,
                resolve,
            )
        )
        return ok(data)

    @app.post("/api/v1/terminals/{terminal_run_id}/answer")
    async def human_terminal_answer(
        request: Request,
        terminal_run_id: int,
        _: HumanMutation,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        idempotency_key = _request_id(request, idempotency_key)
        body = await _json_body(request)
        payload = {"terminal_run_id": terminal_run_id, "body": body.get("body")}
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()

        def answer(connection: sqlite3.Connection) -> dict[str, Any]:
            terminal = connection.execute(
                "SELECT tr.id,b.created_at blocker_created_at FROM terminal_runs tr "
                "JOIN blockers b ON b.terminal_run_id=tr.id AND b.kind='waiting_user_answer' "
                "AND b.state IN ('open','escalated') "
                "WHERE tr.id=? AND tr.state IN ('creating','live','retained')",
                (terminal_run_id,),
            ).fetchone()
            if terminal is None:
                raise DomainError("not_found", "terminal run is not awaiting an answer")
            existing = connection.execute(
                "SELECT 1 FROM terminal_inputs WHERE terminal_run_id=? AND created_at>=? LIMIT 1",
                (terminal_run_id, terminal["blocker_created_at"]),
            ).fetchone()
            if existing is not None:
                raise DomainError("conflict", "an answer was already queued for this prompt")
            text = validate_text(body["body"], "answer", 1, 2048)
            now = utc_now()
            cursor = connection.execute(
                "INSERT INTO terminal_inputs(terminal_run_id,actor_slug,body,state,created_at,updated_at)"
                "VALUES(?,'human',?,'pending',?,?)",
                (terminal_run_id, text, now, now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not allocate terminal input ID")
            return {"id": cursor.lastrowid, "state": "pending"}

        data = _domain(
            lambda: mutation(
                request.app.state.connection,
                "human",
                idempotency_key,
                "terminal.answer",
                f"terminal:{terminal_run_id}",
                digest,
                answer,
            )
        )
        return ok(data)

    @app.get("/api/v1/terminals/{terminal_run_id}/output")
    async def human_terminal_output(request: Request, terminal_run_id: int, _: HumanRead):
        row = request.app.state.connection.execute(
            "SELECT id,actor_slug,state,status,output_tail,output_digest,updated_at FROM terminal_runs WHERE id=?",
            (terminal_run_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=error("not_found", "terminal run does not exist"))
        return ok(dict(row))

    return app


app = create_app()


def main() -> None:
    config = load()
    uvicorn.run("agents.web:app", host=config.web.host, port=config.web.port, workers=1, access_log=False)
