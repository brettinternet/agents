"""Credential-free, isolated Agents delivery smoke.

The smoke deliberately uses the managed CAO 2.4.1 server and tmux terminal. The
only provider executable it exposes is ``tests/fixtures/bin/mock_cli``; that
provider receives the generated Agents token through CAO's session env and
performs the role-specific HTTP calls in the fixture.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from agents import service
from agents.config import AgentsConfig, load
from agents.db import connect, migrate
from agents.reconciler import bootstrap_persistent_agents
from agents.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_BIN = ROOT / "tests" / "fixtures" / "bin"
ORIGIN_URL = "git@github.com:brettinternet/agents.git"


class SmokeFailure(RuntimeError):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def _run(*argv: str, cwd: Path) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SmokeFailure("temporary project", f"{argv[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config_file(source: Path, destination: Path, cao_port: int, web_port: int) -> None:
    text = source.read_text(encoding="utf-8")
    text = text.replace('verify = [["task", "check"], ["task", "test"]]', 'verify = [["git", "status", "--porcelain"]]')
    text = text.replace("poll_seconds = 5", "poll_seconds = 1")
    text = text.replace('provider = "opencode"', 'provider = "mock"')
    text = re.sub(r"(?m)^api_port = \d+$", f"api_port = {cao_port}", text, count=1)
    text = re.sub(r"(?m)^port = \d+$", f"port = {web_port}", text, count=1)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o600)


def _make_project(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-b", "main", cwd=directory)
    _run("git", "config", "user.name", "Agents Smoke", cwd=directory)
    _run("git", "config", "user.email", "agents-smoke@example.invalid", cwd=directory)
    (directory / "README.txt").write_text("isolated Agents smoke project\n", encoding="utf-8")
    _run("git", "add", "README.txt", cwd=directory)
    _run("git", "commit", "-m", "Initialize smoke project", cwd=directory)
    _run("git", "remote", "add", "origin", ORIGIN_URL, cwd=directory)


def _stage(prefix: str, detail: str = "") -> None:
    suffix = f": {detail}" if detail else ""
    print(f"[smoke] {prefix}{suffix}", flush=True)


def _db_row(config: AgentsConfig, query: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    connection = connect(config.state_dir / "agents.db")
    try:
        row = connection.execute(query, args).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _db_rows(config: AgentsConfig, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = connect(config.state_dir / "agents.db")
    try:
        return [dict(row) for row in connection.execute(query, args)]
    finally:
        connection.close()


def _mapped_sessions(client: Any, instance: str) -> list[dict[str, Any]]:
    prefix = f"cao-agents-{instance}-"
    return [
        row
        for row in client.list_sessions()
        if str(row.get("name") or row.get("session_name") or "").startswith(prefix)
    ]


def _wait(stage: str, predicate: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "condition remained false"
    while time.monotonic() < deadline:
        try:
            if predicate():
                _stage(stage)
                return
        except Exception as exc:  # provider/process races are retried until the stage deadline
            last_error = str(exc)
        time.sleep(0.5)
    raise SmokeFailure(stage, last_error)


@contextlib.contextmanager
def _isolated_environment(config_path: Path, home: Path, xdg: Path) -> Iterator[None]:
    old = os.environ.copy()
    for name in (
        "AGENTS_CONFIG",
        "AGENTS_PROVIDER",
        "AGENTS_MODEL",
        "AGENTS_REASONING_EFFORT",
        "AGENTS_CAO_PORT",
        "AGENTS_WEB_PORT",
        "AGENTS_WEB_TOKEN",
        "AGENTS_AGENT_TOKEN",
        "AGENTS_API_URL",
    ):
        os.environ.pop(name, None)
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    tmux_tmpdir = Path("/tmp") / f"agents-tmux-{uuid.uuid4().hex}"
    tmux_tmpdir.mkdir(mode=0o700)
    fixture_path = str(FIXTURE_BIN)
    os.environ.update(
        {
            "AGENTS_CONFIG": str(config_path),
            "AGENTS_PROVIDER": "mock",
            "CAO_HOME_DIR": str(config_path.parent / ".cao"),
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "TMUX_TMPDIR": str(tmux_tmpdir),
            "PATH": fixture_path + os.pathsep + old.get("PATH", ""),
        }
    )
    try:
        yield
    finally:
        os.environ.clear()
        shutil.rmtree(tmux_tmpdir, ignore_errors=True)
        os.environ.update(old)


def _prepare_runtime(runtime: Path, config_path: Path) -> AgentsConfig:
    (runtime / "agents").mkdir(parents=True)
    for profile in (ROOT / "agents").glob("*.md"):
        shutil.copy2(profile, runtime / "agents" / profile.name)
    (runtime / ".tools" / "bin").mkdir(parents=True)
    (runtime / ".venv" / "bin").mkdir(parents=True)
    for name in ("cao", "cao-server"):
        (runtime / ".tools" / "bin" / name).symlink_to((ROOT / ".tools" / "bin" / name).resolve())
    for name in ("agentsd", "agents-mcp-server"):
        (runtime / ".venv" / "bin" / name).symlink_to((ROOT / ".venv" / "bin" / name).resolve())
    config = load(config_path, env={"AGENTS_PROVIDER": "mock"})
    config.state_dir.mkdir(parents=True, mode=0o700)
    config.state_dir.chmod(0o700)
    from agents.profiles import ensure_secret, validate_templates

    ensure_secret(config.state_dir / "agent-auth-key", existing_state=False)
    ensure_secret(config.state_dir / "web-token", existing_state=False)
    validate_templates(config.root)
    connection = connect(config.state_dir / "agents.db")
    try:
        migrate(connection)
        Store(connection).initialize(config)
        bootstrap_persistent_agents(connection, config)
    finally:
        connection.close()
    return config


def _http_json(response: httpx.Response, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise SmokeFailure(stage, f"HTTP {response.status_code} returned non-JSON") from exc
    if response.status_code >= 400:
        raise SmokeFailure(stage, f"HTTP {response.status_code}: {value}")
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise SmokeFailure(stage, f"unexpected response: {value}")
    return value


def _work_detail(client: httpx.Client, item_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/work/{item_id}")
    value = _http_json(response, "work detail")
    data = value.get("data")
    if not isinstance(data, dict):
        raise SmokeFailure("work detail", "response data is not an object")
    return data


def _mutate(
    client: httpx.Client,
    method: str,
    path: str,
    body: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    headers = {
        "Origin": str(client.base_url).rstrip("/"),
        "X-CSRF-Token": client.cookies.get("agents_csrf", ""),
        "Idempotency-Key": f"smoke:{uuid.uuid4()}",
    }
    response = client.request(method, path, json=body, headers=headers)
    return _http_json(response, stage)


def _human_login(client: httpx.Client, config: AgentsConfig) -> None:
    token = (config.state_dir / "web-token").read_text(encoding="utf-8").strip()
    response = client.post(
        "/auth/login",
        data={"token": token},
        headers={"Origin": str(client.base_url).rstrip("/")},
        follow_redirects=False,
    )
    if response.status_code != 303 or not client.cookies.get("agents_session"):
        raise SmokeFailure("human login", f"HTTP {response.status_code}: {response.text[:200]}")
    if not client.cookies.get("agents_csrf"):
        raise SmokeFailure("human login", "CSRF cookie was not issued")
    _stage("human authenticated")


def _assert_evidence(config: AgentsConfig, item_id: str) -> dict[str, Any]:
    work = _db_row(config, "SELECT * FROM work_items WHERE id=?", (item_id,))
    if work is None or work["status"] != "delivered":
        raise SmokeFailure("evidence", f"work is not delivered: {work}")
    submissions = _db_rows(
        config,
        "SELECT s.*,e.branch,e.worktree_path FROM submissions s JOIN executions e ON e.id=s.execution_id WHERE e.work_id=?",
        (item_id,),
    )
    if len(submissions) != 1 or submissions[0]["state"] != "accepted" or not submissions[0]["commit_sha"]:
        raise SmokeFailure("evidence", f"immutable submission missing: {submissions}")
    submission = submissions[0]
    checks = _db_rows(config, "SELECT * FROM checks WHERE submission_id=? ORDER BY scope,id", (submission["id"],))
    if not checks or any(row["state"] != "passed" for row in checks):
        raise SmokeFailure("evidence", f"verification evidence missing: {checks}")
    reviews = _db_rows(config, "SELECT * FROM reviews WHERE submission_id=?", (submission["id"],))
    if (
        len(reviews) != 1
        or reviews[0]["gate"] != "research"
        or reviews[0]["actor_slug"] != "explorer"
        or reviews[0]["verdict"] != "pass"
    ):
        raise SmokeFailure("evidence", f"research evidence missing: {reviews}")
    approval = _db_row(config, "SELECT * FROM approvals WHERE submission_id=?", (submission["id"],))
    if approval is None or approval["state"] != "accepted":
        raise SmokeFailure("evidence", f"approval evidence missing: {approval}")
    if not _db_row(config, "SELECT 1 FROM messages WHERE body LIKE '%Smoke delivery%' LIMIT 1"):
        raise SmokeFailure("evidence", "conversation evidence missing")
    return {"submission": submission, "checks": checks, "reviews": reviews, "approval": approval}


def _cleanup(config: AgentsConfig | None) -> list[str]:
    if config is None:
        return []
    errors: list[str] = []
    try:
        shutdown = getattr(service, "shutdown", None)
        if shutdown is None:
            raise RuntimeError("service.shutdown is unavailable")
        shutdown(config)
    except Exception as exc:
        errors.append(str(exc))
        try:
            service.stop_agents(config)
        except Exception as stop_exc:
            errors.append(str(stop_exc))
    return errors


def run() -> None:
    config: AgentsConfig | None = None
    with tempfile.TemporaryDirectory(prefix="agents-smoke-") as temporary:
        runtime = Path(temporary)
        project = runtime / "project"
        config_path = runtime / "agents.toml"
        home = runtime / "home"
        xdg = runtime / "xdg"
        home.mkdir(mode=0o700)
        xdg.mkdir(mode=0o700)
        _make_project(project)
        _config_file(ROOT / "agents.toml", config_path, _free_port(), _free_port())
        # The copied config's relative project path now points at runtime/project.
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace('path = "."', 'path = "project"'), encoding="utf-8"
        )
        with _isolated_environment(config_path, home, xdg):
            config = _prepare_runtime(runtime, config_path)
            _stage("isolated project and state prepared", str(project))
            client = None
            web = None
            started = False
            try:
                service.start(config)
                started = True
                web = httpx.Client(base_url=f"http://127.0.0.1:{config.web.port}", timeout=10.0)
                _wait(
                    "Agents and CAO services ready",
                    lambda: service.status(config) == {"agentsd": True, "cao": True},
                )
                _wait(
                    "persistent agents live",
                    lambda: (
                        len(
                            _db_rows(
                                config, "SELECT id FROM terminal_runs WHERE purpose_kind='persistent' AND state='live'"
                            )
                        )
                        == 3
                    ),
                )
                client = web
                _human_login(client, config)
                intake = _mutate(
                    client,
                    "POST",
                    "/api/v1/intake",
                    {
                        "kind": "story",
                        "title": "Smoke delivery",
                        "problem": "Exercise the isolated mock delivery path.",
                        "outcome": "The Agents records immutable submission and review evidence.",
                    },
                    "intake",
                )
                item = intake.get("data")
                if not isinstance(item, dict) or item.get("id") != "AGENT-0001":
                    raise SmokeFailure("intake", f"unexpected item: {item}")
                item_id = str(item["id"])
                _stage("intake accepted", item_id)
                _mutate(
                    client,
                    "POST",
                    "/api/v1/messages",
                    {
                        "to": "#publishing",
                        "body": "@elder Please refine Smoke delivery for the deterministic smoke.",
                    },
                    "elder wake",
                )
                _wait(
                    "elder refinement started",
                    lambda: (
                        (_db_row(config, "SELECT status FROM work_items WHERE id=?", (item_id,)) or {}).get("status")
                        == "refining"
                    ),
                )
                _wait(
                    "publishing consultation completed",
                    lambda: bool(
                        _db_row(
                            config,
                            "SELECT 1 FROM consultations WHERE work_id=? AND specialty='publishing' AND state='completed'",
                            (item_id,),
                        )
                    ),
                )
                detail = _work_detail(client, item_id)
                consultations = detail.get("consultations")
                if not isinstance(consultations, list) or not any(
                    row.get("specialty") == "publishing" and row.get("state") == "completed"
                    for row in consultations
                    if isinstance(row, dict)
                ):
                    raise SmokeFailure(
                        "publishing consultation", "completed publishing consultation evidence is absent"
                    )
                _stage("publishing consultation evidence recorded")
                _wait(
                    "work marked ready",
                    lambda: (
                        (_db_row(config, "SELECT status FROM work_items WHERE id=?", (item_id,)) or {}).get("status")
                        == "ready"
                    ),
                )
                _wait(
                    "agent dispatched",
                    lambda: bool(
                        _db_row(config, "SELECT 1 FROM assignments WHERE work_id=? AND state='open'", (item_id,))
                    ),
                )
                _wait(
                    "immutable submission created",
                    lambda: bool(
                        _db_row(
                            config,
                            "SELECT 1 FROM submissions s JOIN executions e ON e.id=s.execution_id WHERE e.work_id=?",
                            (item_id,),
                        )
                    ),
                )
                _wait(
                    "configured verification passed",
                    lambda: bool(
                        _db_row(
                            config,
                            "SELECT 1 FROM checks c JOIN submissions s ON s.id=c.submission_id JOIN executions e ON e.id=s.execution_id WHERE e.work_id=? AND c.scope='submission' AND c.state='passed'",
                            (item_id,),
                        )
                    ),
                )
                _wait(
                    "research review passed",
                    lambda: bool(
                        _db_row(
                            config,
                            "SELECT 1 FROM reviews r JOIN submissions s ON s.id=r.submission_id "
                            "JOIN executions e ON e.id=s.execution_id "
                            "WHERE e.work_id=? AND r.gate='research' AND r.actor_slug='explorer' AND r.verdict='pass'",
                            (item_id,),
                        )
                    ),
                )
                detail = _work_detail(client, item_id)
                evidence_before = _db_row(
                    config,
                    "SELECT commit_sha FROM submissions s JOIN executions e ON e.id=s.execution_id WHERE e.work_id=? ORDER BY s.id DESC LIMIT 1",
                    (item_id,),
                )
                if evidence_before is None:
                    raise SmokeFailure("submission", "submission SHA is missing")
                _stage("immutable submission and research review evidence recorded", str(evidence_before["commit_sha"]))
                _mutate(
                    client,
                    "POST",
                    f"/api/v1/work/{item_id}/accept",
                    {"expected_version": int(detail["work"]["version"]), "feedback": ""},
                    "approval",
                )
                _wait(
                    "human approval recorded",
                    lambda: (
                        (_db_row(config, "SELECT status FROM work_items WHERE id=?", (item_id,)) or {}).get("status")
                        == "accepted"
                    ),
                )
                submission = _db_row(
                    config,
                    "SELECT s.commit_sha,e.branch FROM submissions s JOIN executions e ON e.id=s.execution_id WHERE e.work_id=? ORDER BY s.id DESC LIMIT 1",
                    (item_id,),
                )
                if submission is None:
                    raise SmokeFailure("integration", "accepted branch evidence is missing")
                _run("git", "checkout", "main", cwd=project)
                _run("git", "merge", "--ff-only", str(submission["branch"]), cwd=project)
                _stage("accepted branch merged without rewriting evidence", str(submission["commit_sha"]))
                _wait(
                    "integration verification passed",
                    lambda: bool(
                        _db_row(
                            config,
                            "SELECT 1 FROM checks c JOIN submissions s ON s.id=c.submission_id JOIN executions e ON e.id=s.execution_id WHERE e.work_id=? AND c.scope='integration' AND c.state='passed'",
                            (item_id,),
                        )
                    ),
                )
                _wait(
                    "delivered state recorded",
                    lambda: (
                        (_db_row(config, "SELECT status FROM work_items WHERE id=?", (item_id,)) or {}).get("status")
                        == "delivered"
                    ),
                )
                _assert_evidence(config, item_id)
                _stage("delivery evidence complete", item_id)
            except SmokeFailure:
                _stage("terminal diagnostics", repr(_db_rows(config, "SELECT * FROM terminal_runs")))
                _stage("launch diagnostics", repr(_db_rows(config, "SELECT * FROM launch_attempts")))
                _stage("incident diagnostics", repr(_db_rows(config, "SELECT * FROM incidents")))
                _stage("work diagnostics", repr(_db_rows(config, "SELECT * FROM work_items")))
                _stage("consultation diagnostics", repr(_db_rows(config, "SELECT * FROM consultations")))
                _stage(
                    "delivery diagnostics", repr(_db_rows(config, "SELECT * FROM deliveries ORDER BY id DESC LIMIT 20"))
                )
                for log_name in ("agentsd.log", "cao.log"):
                    log_path = config.state_dir / log_name
                    if log_path.exists():
                        _stage(log_name, log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
                raise
            except Exception as exc:
                _stage("unexpected diagnostics", repr(exc))
                for log_name in ("agentsd.log", "cao.log"):
                    log_path = config.state_dir / log_name
                    if log_path.exists():
                        _stage(log_name, log_path.read_text(encoding="utf-8", errors="replace")[-8000:])
                raise
            finally:
                if web is not None:
                    web.close()
                if started:
                    cleanup_errors = _cleanup(config)
                    if cleanup_errors:
                        detail = "; ".join(cleanup_errors)
                        if sys.exception() is None:
                            raise SmokeFailure("cleanup", detail)
                        print(f"[smoke] cleanup after failure: {detail}", file=sys.stderr, flush=True)
            if config is not None:
                instance_row = _db_row(config, "SELECT instance_id FROM project WHERE id=1")
                if instance_row is None:
                    raise SmokeFailure("cleanup", "project identity disappeared")
                instance = str(instance_row["instance_id"])
                sessions = subprocess.run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                prefix = f"cao-agents-{instance}-"
                remaining = [line for line in sessions.stdout.splitlines() if line.startswith(prefix)]
                if remaining:
                    raise SmokeFailure("cleanup", f"mapped tmux sessions survived: {remaining}")
                live = _db_rows(
                    config,
                    "SELECT session_name,state,token_revoked_at FROM terminal_runs "
                    "WHERE session_name LIKE ? AND state IN ('reserved','creating','live','retained')",
                    (f"{prefix}%",),
                )
                if live:
                    raise SmokeFailure("cleanup", f"mapped terminal rows remain active: {live}")
                _stage("services stopped; no mapped CAO sessions survive")


def main() -> int:
    try:
        run()
    except SmokeFailure as exc:
        print(f"[smoke] FAIL {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(f"[smoke] FAIL unexpected: {exc}", file=sys.stderr, flush=True)
        return 1
    print("[smoke] PASS isolated delivery", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
