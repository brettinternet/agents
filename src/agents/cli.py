from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from . import service
from .auth import derive_agent_token, read_agent_auth_key
from .cao_client import CaoClient
from .config import AgentsConfig, load
from .db import connect, migrate, utc_now
from .git_worktree import GitError, branch_sha, git, reserve_execution, validate_project
from .profiles import ensure_secret, validate_manifest_artifact, validate_templates
from .reconciler import reserve_terminal
from .store import Store
from .workflow import Workflow


class DoctorError(RuntimeError):
    pass


def _config() -> AgentsConfig:
    load_dotenv()
    return load()


def _prepare(config: AgentsConfig) -> None:
    if not (config.root / ".env").exists():
        (config.root / ".env").write_text("# Local Agents environment overrides\n", encoding="utf-8")
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


def doctor(config: AgentsConfig, online: bool = True) -> list[str]:
    errors: list[str] = []
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
    try:
        validate_templates(config.root)
    except ValueError as exc:
        errors.append(str(exc))
    cao = config.root / ".tools" / "bin" / "cao"
    tmux = shutil.which("tmux")
    if not cao.is_file():
        errors.append("managed cao executable is missing")
    else:
        result = subprocess.run([str(cao), "--version"], capture_output=True, text=True, check=False)
        if result.returncode or "2.4.1" not in result.stdout + result.stderr:
            errors.append("CAO version is not 2.4.1")
    if tmux is None:
        errors.append("tmux is missing")
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
        client = CaoClient(config.cao.api_port)
        if not client.health():
            errors.append("CAO health check failed")
        else:
            try:
                document = client.openapi()
                paths = document.get("paths", {})
                required = {
                    "/sessions": {"post"},
                    "/sessions/{session_name}": {"get", "delete"},
                    "/sessions/{session_name}/terminals": {"get"},
                    "/terminals/{terminal_id}": {"get"},
                    "/terminals/{terminal_id}/working-directory": {"get"},
                    "/terminals/{terminal_id}/input": {"post"},
                    "/terminals/{terminal_id}/output": {"get"},
                    "/terminals/{terminal_id}/inbox/messages": {"post"},
                }
                missing = [
                    f"{path} {','.join(sorted(methods - set(paths.get(path, {}))))}"
                    for path, methods in required.items()
                    if path not in paths or not methods.issubset(set(paths[path]))
                ]
                if missing:
                    errors.append("CAO OpenAPI is missing required methods: " + ", ".join(missing))
                encoded = str(document)
                for field in ("session_name", "provider", "working_directory", "env_vars", "terminal_id", "message"):
                    if field not in encoded:
                        errors.append(f"CAO OpenAPI is missing required field: {field}")
            except RuntimeError as exc:
                errors.append(str(exc))
        if service.status(config) != {"agentsd": True, "cao": True}:
            errors.append("owned services are not both running")
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
        outcome="The elder resolves the blocker.",
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
            "elder",
            "explorer",
            "completed",
            "Use the existing store.",
            now,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO decisions("
        "work_id,title,question,options_json,recommendation,state,proposed_by,created_at,updated_at"
        ") VALUES(?,?,?,?,?,'open','elder',?,?)",
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
        "session_name,profile_name,mcp_name,profile_sha256,provider,model,generation,actor_slug,purpose_kind,"
        "purpose_id,working_directory,token_digest,terminal_id,profile_state,state,status,output_tail,created_at,updated_at"
        ") VALUES('cao-agents-dev-w-prompt-explorer-1-g0001','agents-dev-r0000000001-g0001',"
        "'agents-dev-r0000000001-g0001','demo','mock','',1,'explorer','work',?,?,"
        "'demo','demo-terminal','installed','live','waiting','Need human answer',?,?)",
        (blocked["id"], str(project["canonical_path"]), now, now),
    ).lastrowid
    connection.execute(
        "INSERT INTO actor_leases(actor_slug,purpose_kind,purpose_id,terminal_run_id,acquired_at)"
        "VALUES('explorer','work',?,?,?)",
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
        base_sha, branch = reserve_execution(
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
            "VALUES(?,'research','explorer',?,'pass','Verified immutable submission',?,?)",
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
                actor="yapper",
                purpose_kind="work",
                purpose_id=str(item["id"]),
                working_directory=worktree,
            )
            connection.execute(
                "UPDATE terminal_runs SET terminal_id='agents-approval',profile_state='installed',state='live',"
                "status='idle',updated_at=? WHERE id=?",
                (now, run["id"]),
            )
            connection.execute(
                "UPDATE launch_attempts SET counted=1,state='succeeded',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            connection.execute(
                "INSERT INTO assignments(work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at)"
                "VALUES(?,?, 'yapper',?,'open',?,?)",
                (item["id"], execution, run["id"], now, now),
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("migrate")
    sub.add_parser("doctor").add_argument("--offline", action="store_true")
    service_parser = sub.add_parser("service")
    service_parser.add_argument("action", choices=("start", "stop", "status"))
    for name in ("dashboard", "sessions", "smoke", "dev-mock", "shutdown"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    config = _config()
    if args.command == "init":
        _prepare(config)
        service.start(config)
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
        else:
            print(service.status(config))
        errors = []
    elif args.command == "dashboard":
        print(config.state_dir / "web-token")
        errors = []
    elif args.command == "sessions":
        for row in CaoClient(config.cao.api_port).list_sessions():
            name = str(row.get("name") or row.get("session_name") or "")
            if name.startswith("cao-agents-"):
                print(name)
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
