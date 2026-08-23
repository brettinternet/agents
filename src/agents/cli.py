from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

import uvicorn

from . import service
from .auth import derive_agent_token, read_agent_auth_key
from .config import AgentsConfig, load, resolve_execution_session
from .container_runtime import ContainerRuntime, ContainerRuntimeError, _instance_id
from .db import connect, migrate, utc_now
from .git_worktree import GitError, branch_sha, git, reserve_execution_workspace, validate_project
from .herdr_client import HerdrClient, herdr_executable, herdr_socket_path
from .profiles import ensure_secret, validate_manifest_artifact, validate_templates
from .reconciler import reserve_terminal
from .store import Store
from .workflow import Workflow


class DoctorError(RuntimeError):
    pass


def _config() -> AgentsConfig:
    return load()


def _prepare(config: AgentsConfig) -> None:
    state = config.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    existing = (state / "agents.db").exists() and (state / "agents.db").stat().st_size > 0
    ensure_secret(state / "agent-auth-key", existing_state=existing)
    ensure_secret(state / "web-token", existing_state=False, allow_environment=True)
    validate_templates(config.root)
    connection = connect(state / "agents.db")
    try:
        migrate(connection)
        Store(connection).initialize(config)
    finally:
        connection.close()


_PROVIDER_CLI = {
    "opencode": ("opencode", "install OpenCode and ensure `opencode` is on PATH"),
    "claude": ("claude", "install Claude Code and ensure `claude` is on PATH"),
    "mock": ("mock_cli", "ensure `mock_cli` is on PATH"),
}


def preflight(config: AgentsConfig) -> list[str]:
    errors: list[str] = []
    executable, action = _PROVIDER_CLI[config.execution.provider]
    container_mode = str(config.execution.isolation) == "container"
    if not container_mode and shutil.which(executable) is None:
        errors.append(f"configured provider executable `{executable}` is missing; operator action: {action}")
    if config.execution.backend != "herdr":
        errors.append(f"unsupported execution backend `{config.execution.backend}`")
    if config.execution.version != "0.8.2":
        errors.append(f"Herdr version must be 0.8.2, got `{config.execution.version}`")
    try:
        herdr_executable()
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Herdr executable is missing; operator action: install Herdr 0.8.2 ({exc})")
    if container_mode:
        if config.execution.container is None:
            errors.append("[execution.container] is required in container mode")
        else:
            from .container_runtime import ContainerRuntime, ContainerRuntimeError

            try:
                runtime = ContainerRuntime(config.execution.container)
                runtime.validate_colima_version()
                runtime.status()
                runtime.resolve_image_id(config.execution.container.image)
            except ContainerRuntimeError as exc:
                errors.append(f"container runtime is unavailable: {exc}")
        credential = {
            "opencode": "OPENCODE_AUTH_JSON",
            "claude": "CLAUDE_CODE_OAUTH_TOKEN",
        }.get(config.execution.provider)
        if credential and not os.environ.get(credential):
            errors.append(f"{credential} is required for containerized {config.execution.provider}")
    for setting, placeholder in (("user.name", "Your Name"), ("user.email", "you@example.com")):
        try:
            value = git(config.project.path, "config", "--get", setting)
        except GitError:
            value = ""
        if not value:
            command = shlex.join(("git", "-C", str(config.project.path), "config", setting, placeholder))
            errors.append(f"Git commit identity `{setting}` is missing; operator action: run `{command}`")
    return errors


_HERDR_METHODS = {
    "ping",
    "session.snapshot",
    "workspace.create",
    "workspace.close",
    "agent.start",
    "pane.get",
    "pane.read",
    "agent.prompt",
    "pane.send_input",
    "events.subscribe",
}


def _schema_methods(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value in _HERDR_METHODS else set()
    methods: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key in _HERDR_METHODS:
                methods.add(key)
            methods.update(_schema_methods(item))
    elif isinstance(value, list):
        for item in value:
            methods.update(_schema_methods(item))
    return methods


def _missing_herdr_methods(document: Mapping[str, object]) -> list[str]:
    return sorted(_HERDR_METHODS - _schema_methods(document))


def install_integration(config: AgentsConfig) -> None:
    if config.execution.provider == "mock":
        return
    session = resolve_execution_session(config)
    if not session:
        raise DoctorError("project identity is missing; initialize Agents before installing Herdr integration")
    command, environment = service._herdr_command(config, session, "integration", "install", config.execution.provider)
    result = subprocess.run(
        command,
        cwd=config.root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise DoctorError((result.stderr or result.stdout or "Herdr integration install failed").strip())


def doctor(config: AgentsConfig, online: bool = True) -> list[str]:
    errors: list[str] = []
    errors.extend(preflight(config))
    try:
        canonical, _ = validate_project(config.project.path, config.project.default_branch)
        if canonical != config.project.path:
            errors.append("project path is not canonical")
        remotes = git(config.project.path, "remote").splitlines()
        if "origin" in remotes and git(config.project.path, "remote", "get-url", "origin") != (
            "git@github.com:brettinternet/agents.git"
        ):
            errors.append("origin URL mismatch")
    except GitError as exc:
        errors.append(f"git prerequisite failed: {exc}")
    if not config.state_dir.is_dir() or config.state_dir.stat().st_mode & 0o077:
        errors.append("unsafe .agents directory")
    if config.herdr_config.exists() and (
        config.herdr_config.is_symlink()
        or not config.herdr_config.is_file()
        or config.herdr_config.stat().st_mode & 0o077
    ):
        errors.append("unsafe Herdr config")
    try:
        validate_templates(config.root)
    except ValueError as exc:
        errors.append(str(exc))
    if "workers=1" not in (config.root / "src/agents/web.py").read_text(encoding="utf-8"):
        errors.append("agentsd must run with exactly one Uvicorn worker")
    database = config.state_dir / "agents.db"
    if database.exists():
        connection = connect(database)
        try:
            for row in connection.execute(
                "SELECT profile_name,profile_sha256 FROM terminal_runs "
                "WHERE state IN ('creating','live','retained','ending') AND profile_name<>'reserved'"
            ):
                profile_path = config.state_dir / "profiles" / f"{row['profile_name']}.md"
                if (
                    not profile_path.is_file()
                    or profile_path.is_symlink()
                    or hashlib.sha256(profile_path.read_bytes()).hexdigest() != row["profile_sha256"]
                ):
                    errors.append(f"profile materialization mismatch: {row['profile_name']}")
            key = read_agent_auth_key(config.state_dir / "agent-auth-key")
            for row in connection.execute(
                "SELECT ta.kind,ta.path,ta.fragment_key,ta.expected_sha256,ta.expected_json_redacted,"
                "ta.secret_fields_json,tr.id run_id,tr.generation,p.instance_id "
                "FROM terminal_artifacts ta JOIN terminal_runs tr ON tr.id=ta.terminal_run_id "
                "JOIN project p ON p.id=1 WHERE ta.state='installed'"
            ):
                secret_values = {
                    "AGENTS_AGENT_TOKEN": derive_agent_token(
                        key, str(row["instance_id"]), int(row["run_id"]), int(row["generation"])
                    )
                }
                if not validate_manifest_artifact(
                    Path(str(row["path"])),
                    str(row["expected_sha256"]),
                    fragment_key=row["fragment_key"],
                    expected_json_redacted=row["expected_json_redacted"],
                    secret_fields_json=row["secret_fields_json"],
                    secret_values=secret_values,
                    require_secret_values=str(row["kind"]).startswith("runtime_"),
                ):
                    errors.append(f"runtime manifest mismatch: {row['path']}")
        except sqlite3.Error as exc:
            errors.append(f"runtime manifest check failed: {exc}")
        finally:
            connection.close()
    if online:
        session = resolve_execution_session(config)
        if not session:
            errors.append("project identity is missing; Herdr session is unavailable")
        else:
            client = HerdrClient(
                herdr_socket_path(session, env=service._herdr_environment(config)),
                expected_version=config.execution.version,
            )
            try:
                try:
                    health = client.health()
                    if (
                        not health.healthy
                        or health.version != config.execution.version
                        or health.protocol != 20
                        or not health.supports_events
                    ):
                        errors.append(
                            "Herdr health is incompatible: "
                            f"version={health.version!r} protocol={health.protocol!r} "
                            f"supports_events={health.supports_events!r} {health.message}".strip()
                        )
                except Exception as exc:
                    errors.append(f"Herdr health check failed: {exc}")
            finally:
                client.close()
            command, environment = service._herdr_command(config, session, "api", "schema", "--json")
            schema = subprocess.run(
                command,
                cwd=config.root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if schema.returncode:
                errors.append((schema.stderr or schema.stdout or "Herdr schema command failed").strip())
            else:
                try:
                    document = json.loads(schema.stdout)
                except json.JSONDecodeError as exc:
                    errors.append(f"Herdr schema is not valid JSON: {exc}")
                else:
                    if not isinstance(document, Mapping):
                        errors.append("Herdr schema is not a JSON object")
                    else:
                        missing = _missing_herdr_methods(document)
                        if missing:
                            errors.append("Herdr schema is missing required methods: " + ", ".join(missing))
            integration = {"opencode": "opencode", "claude": "claude"}.get(config.execution.provider)
            if integration is not None:
                command, environment = service._herdr_command(config, session, "integration", "status")
                integration_status = subprocess.run(
                    command,
                    cwd=config.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                expected = f"{integration}: current "
                if integration_status.returncode or not any(
                    line.startswith(expected) for line in integration_status.stdout.splitlines()
                ):
                    detail = (
                        integration_status.stderr
                        or integration_status.stdout
                        or f"{integration} integration is not installed"
                    ).strip()
                    errors.append(f"Herdr provider integration is not current: {detail}")
        if service.status(config) != {"agentsd": True, "herdr": True}:
            errors.append(f"owned services are not both running: {service.status(config)}")
        if config.execution.container is not None and str(config.execution.isolation) == "container":
            try:
                runtime = ContainerRuntime(config.execution.container)
                runtime.initialize(config.root, _instance_id(config), config.web.port)
                if (config.state_dir / "container-topology.json").is_file():
                    from .container_commands import ContainerCommandError, _verify_system_topology

                    try:
                        _verify_system_topology(config, exercise_janitor=False)
                    except ContainerCommandError as exc:
                        errors.append(str(exc))
                runtime.verify_api_reachable(config.web.port, _instance_id(config))
            except ContainerRuntimeError as exc:
                errors.append(str(exc))
    return errors


def _seed_development(connection: sqlite3.Connection, config: AgentsConfig) -> None:
    if connection.execute("SELECT 1 FROM work_items LIMIT 1").fetchone() is not None:
        return
    workflow = Workflow(connection)
    active = workflow.create_work(
        "dev-active",
        "human",
        parent_id=None,
        kind="story",
        title="Active exploration",
        problem="Demonstrate active delivery.",
        outcome="Dashboard exposes current internet exploration state.",
    )
    accepted = workflow.create_work(
        "dev-accepted",
        "human",
        parent_id=None,
        kind="story",
        title="Awaiting integration",
        problem="Demonstrate accepted work.",
        outcome="Human can inspect immutable evidence.",
    )
    blocked = workflow.create_work(
        "dev-blocked",
        "human",
        parent_id=None,
        kind="bug",
        title="Provider needs input",
        problem="The provider requested clarification.",
        outcome="The manager resolves the blocker.",
    )
    approval = workflow.create_work(
        "dev-approval",
        "human",
        parent_id=None,
        kind="story",
        title="Review completed",
        problem="Demonstrate human approval.",
        outcome="Human accepts or rejects immutable reviewed work.",
    )
    now = utc_now()
    for item, status in (
        (active, "in_progress"),
        (accepted, "accepted"),
        (blocked, "blocked"),
        (approval, "awaiting_approval"),
    ):
        connection.execute(
            "UPDATE work_items SET status=?,specialty='research',updated_at=? WHERE id=?",
            (status, now, item["id"]),
        )
    connection.execute("UPDATE work_items SET specialty='publishing' WHERE id=?", (approval["id"],))
    connection.execute("UPDATE work_items SET blocked_from='in_progress' WHERE id=?", (blocked["id"],))
    connection.execute(
        "INSERT INTO consultations("
        "work_id,specialty,question,requester,responder,state,response,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            active["id"],
            "research",
            "Which exploration path?",
            "manager",
            "researcher",
            "completed",
            "Use the existing store.",
            now,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO decisions("
        "work_id,title,question,options_json,recommendation,state,proposed_by,created_at,updated_at"
        ") VALUES(?,?,?,?,?,'open','manager',?,?)",
        (
            accepted["id"],
            "Integration order",
            "Which item lands first?",
            '["Active","Accepted"]',
            "Accepted",
            now,
            now,
        ),
    )
    project = connection.execute("SELECT canonical_path,default_branch FROM project WHERE id=1").fetchone()
    if project is None:
        raise RuntimeError("Agents project is not initialized")
    terminal = connection.execute(
        "INSERT INTO terminal_runs("
        "execution_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,purpose_kind,"
        "purpose_id,working_directory,token_digest,backend_terminal_id,execution_backend,backend_run_id,agent_auth_id,"
        "profile_state,state,status,output_tail,created_at,updated_at"
        ") VALUES('agents-dev-w-prompt-researcher-1-g0001','agents-dev-r0000000001-g0001',"
        "'agents-dev-r0000000001-g0001','demo','mock','',1,'researcher','work',?,?,"
        "'demo','demo-terminal','herdr','agents-dev-w-prompt-researcher-1-g0001',"
        "'agents-dev-r0000000001-g0001','installed','live','waiting','Need human answer',?,?)",
        (blocked["id"], str(project["canonical_path"]), now, now),
    ).lastrowid
    connection.execute(
        "INSERT INTO actor_leases(actor_slug,purpose_kind,purpose_id,terminal_run_id,acquired_at)"
        "VALUES('researcher','work',?,?,?)",
        (blocked["id"], terminal, now),
    )
    connection.execute(
        "INSERT INTO launch_attempts(terminal_run_id,budget_exempt,counted,state,created_at,updated_at)"
        "VALUES(?,0,1,'succeeded',?,?)",
        (terminal, now, now),
    )
    connection.execute(
        "INSERT INTO blockers("
        "work_id,target_kind,target_id,terminal_run_id,kind,reason,requested_role,actor_slug,resume_state,state,"
        "created_at,updated_at) VALUES(?,'work',?,?,'provider_prompt','Choose the safe default','human','system',"
        "'in_progress','open',?,?)",
        (blocked["id"], blocked["id"], terminal, now, now),
    )
    sha = branch_sha(Path(str(project["canonical_path"])), str(project["default_branch"]))
    for item, submission_state, approval_state in (
        (accepted, "accepted", "accepted"),
        (approval, "awaiting_approval", "pending"),
    ):
        worktree = config.root / ".worktrees" / "dev" / str(item["id"]).lower()
        base_sha, branch = reserve_execution_workspace(
            config,
            config.project.path,
            config.project.default_branch,
            str(item["id"]),
            1,
            worktree,
        )
        execution = connection.execute(
            "INSERT INTO executions(work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at)"
            "VALUES(?,1,?,?,?,?,?,?)",
            (
                item["id"],
                base_sha,
                branch,
                str(worktree),
                "active" if approval_state == "pending" else "closed",
                now,
                now,
            ),
        ).lastrowid
        submission = connection.execute(
            "INSERT INTO submissions(execution_id,revision,commit_sha,summary,state,created_at,updated_at)"
            "VALUES(?,1,?,'Demo submission',?,?,?)",
            (execution, sha, submission_state, now, now),
        ).lastrowid
        connection.execute(
            "INSERT INTO checks(submission_id,scope,target_sha,position,command,worktree_path,state,"
            "exit_code,duration_ms,stdout_tail,created_at,updated_at)VALUES(?,'submission',?,1,'[\"task\",\"check\"]',"
            "?,'passed',0,120,'All checks passed',?,?)",
            (submission, sha, str(worktree), now, now),
        )
        connection.execute(
            "INSERT INTO reviews(submission_id,gate,actor_slug,worktree_path,verdict,body,created_at,updated_at)"
            "VALUES(?,'research','researcher',?,'pass','Verified immutable submission',?,?)",
            (submission, str(worktree), now, now),
        )
        connection.execute(
            "INSERT INTO approvals(submission_id,state,decided_by,created_at,updated_at)VALUES(?,?,?,?,?)",
            (submission, approval_state, "human" if approval_state == "accepted" else None, now, now),
        )
        connection.execute("UPDATE work_items SET accepted_submission_id=? WHERE id=?", (submission, item["id"]))
        if approval_state == "pending":
            run = reserve_terminal(
                connection,
                config,
                actor="writer",
                purpose_kind="work",
                purpose_id=str(item["id"]),
                working_directory=worktree,
            )
            connection.execute(
                "UPDATE terminal_runs SET backend_terminal_id='agents-approval',profile_state='installed',state='live',"
                "status='idle',updated_at=? WHERE id=?",
                (now, run["id"]),
            )
            connection.execute(
                "UPDATE launch_attempts SET counted=1,state='succeeded',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            connection.execute(
                "INSERT INTO assignments(work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at)"
                "VALUES(?,?, 'writer',?,'open',?,?)",
                (item["id"], execution, run["id"], now, now),
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("migrate")
    sub.add_parser("doctor").add_argument("--offline", action="store_true")
    service_parser = sub.add_parser("service")
    service_parser.add_argument("action", choices=("start", "stop", "status", "foreground"))
    container_parser = sub.add_parser("container")
    container_parser.add_argument(
        "action",
        choices=("runtime-init", "build", "start", "stop", "status", "gc", "janitor", "reset", "smoke"),
    )
    container_parser.add_argument("topology", nargs="?", choices=("agent", "system"), default="system")
    for name in ("dashboard", "sessions", "smoke", "dev-mock", "shutdown"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    config = _config()
    if args.command == "init":
        _prepare(config)
        errors = preflight(config)
        if not errors:
            try:
                install_integration(config)
                service.start(config)
            except (DoctorError, service.ServiceError) as exc:
                errors = [str(exc)]
            else:
                errors = doctor(config)
    elif args.command == "migrate":
        connection = connect(config.state_dir / "agents.db")
        try:
            migrate(connection)
            Store(connection).initialize(config)
        finally:
            connection.close()
        errors = []
    elif args.command == "doctor":
        errors = doctor(config, online=not args.offline)
    elif args.command == "service":
        if args.action == "start":
            service.start(config)
        elif args.action == "stop":
            service.stop(config)
        elif args.action == "foreground":
            _prepare(config)
            install_integration(config)
            service.foreground(config)
        else:
            print(service.status(config))
        errors = []
    elif args.command == "container":
        from . import container_commands

        action = args.action.replace("-", "_")
        command = getattr(container_commands, action)
        result = command(config, args.topology) if args.action == "smoke" else command(config)
        if result is not None:
            print(json.dumps(result, sort_keys=True) if isinstance(result, dict) else result)
        errors = []
    elif args.command == "dashboard":
        print(config.state_dir / "web-token")
        errors = []
    elif args.command == "sessions":
        session = resolve_execution_session(config)
        if not session:
            raise SystemExit("project identity is missing; initialize Agents before listing sessions")
        client = HerdrClient(
            herdr_socket_path(session, env=service._herdr_environment(config)),
            expected_version=config.execution.version,
        )
        try:
            snapshot = client.request("session.snapshot", {})
            prefix = f"{session}-"
            for workspace in service._workspaces(snapshot):
                name = service._workspace_label(workspace)
                if name.startswith(prefix):
                    print(name)
        finally:
            client.close()
        errors = []
    elif args.command == "shutdown":
        service.shutdown(config)
        errors = []
    elif args.command == "smoke":
        errors = doctor(config, online=True)
    elif args.command == "dev-mock":
        _prepare(config)
        from .web import create_app

        connection = connect(config.state_dir / "agents.db")
        try:
            migrate(connection)
            Store(connection).initialize(config)
            _seed_development(connection, config)
            uvicorn.run(
                create_app(config, connection),
                host=config.web.host,
                port=config.web.port,
                workers=1,
                access_log=False,
            )
        finally:
            connection.close()
        errors = []
    else:
        _prepare(config)
        errors = []
    if errors:
        raise SystemExit("\n".join(errors))


if __name__ == "__main__":
    main()
