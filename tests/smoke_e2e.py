"""Credential-free, isolated direct-Herdr Agents delivery smoke.

The only provider executable exposed is ``tests/fixtures/bin/mock_cli``. Herdr
owns the isolated PTYs while the fixture exercises role-specific HTTP calls.
"""

from __future__ import annotations

import argparse
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
from unittest import mock

import httpx

from agents import service
from agents.cli import doctor
from agents.config import AgentsConfig, load
from agents.container_runtime import (
    ContainerizedHerdrBackend,
    ContainerRuntime,
    build_execution_backend,
    container_name,
)
from agents.db import connect, migrate
from agents.reconciler import bootstrap_persistent_agents
from agents.store import Store

ROOT = (
    Path(os.environ["AGENTS_CONFIG"]).resolve().parent
    if os.environ.get("AGENTS_SYSTEM_CONTAINER") == "1"
    else Path(__file__).resolve().parents[1]
)
FIXTURE_BIN = ROOT / "tests" / "fixtures" / "bin"
TOOL_HOME = Path.home()
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


def _config_file(source: Path, destination: Path, web_port: int, isolation: str = "host") -> None:
    text = source.read_text(encoding="utf-8")
    text = text.replace('verify = [["task", "check"], ["task", "test"]]', 'verify = [["git", "status", "--porcelain"]]')
    text = text.replace("poll_seconds = 5", "poll_seconds = 1")
    text = text.replace('provider = "opencode"', 'provider = "mock"')
    text = re.sub(r"(?m)^port = \d+$", f"port = {web_port}", text, count=1)
    text = re.sub(r'(?m)^isolation = "(?:host|container)"$', f'isolation = "{isolation}"', text, count=1)
    if isolation == "container":
        text = text.replace('image = "agents-agent-opencode:local"', 'image = "agents-agent-mock:local"')
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


def _assert_container_boundary(config: AgentsConfig) -> None:
    container = config.execution.container
    if container is None:
        raise SmokeFailure("container boundary", "container configuration is absent")
    instance_row = _db_row(config, "SELECT instance_id FROM project WHERE id=1")
    if instance_row is None:
        raise SmokeFailure("container boundary", "project identity is absent")
    instance = str(instance_row["instance_id"])
    runtime = ContainerRuntime(container)
    backend = build_execution_backend(config)
    rows = _db_rows(
        config,
        "SELECT id,execution_name,generation,container_image_id,working_directory,agent_auth_id "
        "FROM terminal_runs WHERE state='live' ORDER BY id",
    )
    if not rows:
        raise SmokeFailure("container boundary", "no live container run exists")
    expected_image = runtime.resolve_image_id(container.image)
    for row in rows:
        execution_name = str(row["execution_name"])
        if backend.find_run(execution_name) is None:
            raise SmokeFailure("container boundary", f"{execution_name} was not adopted")
        name = container_name(instance, int(row["id"]), int(row["generation"]))
        inspect = runtime.inspect_container(name)
        if inspect is None:
            raise SmokeFailure("container boundary", f"{name} is absent")
        host = inspect.get("HostConfig", {})
        config_data = inspect.get("Config", {})
        labels = config_data.get("Labels", {})
        if (
            inspect.get("Image") != expected_image
            or row["container_image_id"] != expected_image
            or config_data.get("User") != f"{os.getuid()}:{os.getgid()}"
            or not host.get("ReadonlyRootfs")
            or int(host.get("NanoCpus") or 0) != int(container.cpus * 1_000_000_000)
            or int(host.get("Memory") or 0) != container.memory_mb * 1024 * 1024
            or int(host.get("PidsLimit") or 0) != container.pids_limit
            or host.get("NetworkMode") != "agents-runs"
            or "ALL" not in (host.get("CapDrop") or ())
            or "no-new-privileges" not in (host.get("SecurityOpt") or ())
            or labels.get("dev.agents.instance") != instance
            or labels.get("dev.agents.execution") != execution_name
            or labels.get("dev.agents.image_id") != expected_image
        ):
            raise SmokeFailure("container boundary", f"{name} has unexpected immutable identity or hardening")
        mounts = inspect.get("Mounts", ())
        binds = {
            str(mount.get("Destination")): str(mount.get("Source"))
            for mount in mounts
            if isinstance(mount, dict) and mount.get("Type") == "bind"
        }
        cwd = str(Path(str(row["working_directory"])).resolve())
        runtime_dir = str((config.state_dir / "runtime" / str(row["agent_auth_id"])).resolve())
        has_git_dir = (Path(cwd) / ".git").is_dir()
        if binds != {cwd: cwd, runtime_dir: runtime_dir}:
            raise SmokeFailure("container boundary", f"{name} has an unexpected bind mount")
        sentinel = f"agents-{instance[:12]}-network-smoke"
        runtime.docker(
            "run",
            "--detach",
            "--name",
            sentinel,
            "--network",
            "agents-system",
            "--ip",
            "172.30.1.3",
            "--label",
            f"dev.agents.instance={instance}",
            "--label",
            "dev.agents.smoke=network-sentinel",
            expected_image,
            "/opt/agents/.venv/bin/python",
            "-c",
            "import socket;s=socket.socket();s.bind(('0.0.0.0',9891));s.listen();exec('while True:\\n c,_=s.accept();c.close()')",
        )
        try:
            runtime.docker(
                "exec",
                name,
                "python",
                "-c",
                (
                    "import os,socket,urllib.request\n"
                    f"urllib.request.urlopen('http://host.docker.internal:{config.web.port}/health',timeout=2).read()\n"
                    "urllib.request.urlopen('https://example.com/',timeout=5).read(1)\n"
                    f"assert os.path.isdir({cwd!r}+'/.git') is {has_git_dir!r}\n"
                    f"assert not os.path.exists({str(config.root / '.env.sops-age')!r})\n"
                    f"assert not os.path.exists({str(config.root / '.sops-isolated-home')!r})\n"
                    "assert 'SSH_AUTH_SOCK' not in os.environ\n"
                    "assert not os.path.exists('/var/run/docker.sock')\n"
                    "def denied(address):\n"
                    "    sock=socket.socket()\n"
                    "    sock.settimeout(0.5)\n"
                    "    try:\n"
                    "        return sock.connect_ex(address)!=0\n"
                    "    finally:\n"
                    "        sock.close()\n"
                    "blocked=[('172.30.0.1',22),('172.30.1.3',9891),('169.254.169.254',80)]\n"
                    "results={address:denied(address) for address in blocked}\n"
                    "assert all(results.values()),results\n"
                ),
            )
        finally:
            sentinel_inspect = runtime.inspect_container(sentinel)
            sentinel_labels = (
                sentinel_inspect.get("Config", {}).get("Labels", {}) if sentinel_inspect is not None else {}
            )
            if (
                sentinel_inspect is None
                or sentinel_labels.get("dev.agents.instance") != instance
                or sentinel_labels.get("dev.agents.smoke") != "network-sentinel"
            ):
                raise SmokeFailure("container boundary", "network sentinel identity changed; refusing cleanup")
            sentinel_id = sentinel_inspect.get("Id")
            if not isinstance(sentinel_id, str) or not sentinel_id:
                raise SmokeFailure("container boundary", "network sentinel has no immutable identity")
            runtime.remove_container(sentinel, sentinel_id)
    original_image = expected_image
    alternate_image = runtime.resolve_image_id("agents-system-mock:local")
    if alternate_image == original_image:
        raise SmokeFailure("container retag", "fixture images unexpectedly share one image ID")
    runtime.docker("image", "tag", alternate_image, container.image)
    try:
        for row in rows:
            if backend.find_run(str(row["execution_name"])) is None:
                raise SmokeFailure("container retag", "active run disappeared after configured tag changed")
    finally:
        runtime.docker("image", "tag", original_image, container.image)
    if runtime.resolve_image_id(container.image) != original_image:
        raise SmokeFailure("container retag", "configured image tag was not restored")
    _stage("container identity, hardening, network policy, and immutable retag adoption validated")


def _assert_live_identity_conflict(config: AgentsConfig) -> None:
    container = config.execution.container
    if container is None:
        raise SmokeFailure("identity conflict", "container configuration is absent")
    instance_row = _db_row(config, "SELECT instance_id FROM project WHERE id=1")
    row = _db_row(
        config,
        "SELECT id,execution_name,generation,container_image_id FROM terminal_runs "
        "WHERE state='live' ORDER BY id LIMIT 1",
    )
    if instance_row is None or row is None:
        raise SmokeFailure("identity conflict", "no live container identity is available")
    instance = str(instance_row["instance_id"])
    name = container_name(instance, int(row["id"]), int(row["generation"]))
    runtime = ContainerRuntime(container)
    backend = build_execution_backend(config)
    if not isinstance(backend, ContainerizedHerdrBackend):
        raise SmokeFailure("identity conflict", "container execution backend is unavailable")
    backend.verified_container_name(
        str(row["execution_name"]),
        int(row["id"]),
        int(row["generation"]),
        str(row["container_image_id"]),
    )
    original_inspect = runtime.inspect_container(name)
    original_id = original_inspect.get("Id") if original_inspect is not None else None
    if not isinstance(original_id, str) or not original_id:
        raise SmokeFailure("identity conflict", "live fixture has no immutable container identity")
    runtime.remove_container(name, original_id)
    runtime.docker(
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"dev.agents.instance={instance}",
        "--label",
        "dev.agents.smoke=identity-conflict",
        str(row["container_image_id"]),
        "sleep",
        "300",
    )
    try:
        _wait(
            "identity conflict revocation",
            lambda: bool(
                _db_row(
                    config,
                    "SELECT 1 FROM terminal_runs WHERE id=? AND token_revoked_at IS NOT NULL",
                    (row["id"],),
                )
            ),
        )
        inspect = runtime.inspect_container(name)
        labels = inspect.get("Config", {}).get("Labels", {}) if inspect is not None else {}
        if labels.get("dev.agents.smoke") != "identity-conflict":
            raise SmokeFailure("identity conflict", "reconciliation adopted or deleted the unverified fixture")
    finally:
        inspect = runtime.inspect_container(name)
        labels = inspect.get("Config", {}).get("Labels", {}) if inspect is not None else {}
        if labels.get("dev.agents.smoke") == "identity-conflict":
            replacement_id = inspect.get("Id") if inspect is not None else None
            if not isinstance(replacement_id, str) or not replacement_id:
                raise SmokeFailure("identity conflict", "replacement fixture has no immutable container identity")
            runtime.remove_container(name, replacement_id)
    _stage("live wrong-label container was revoked and left untouched")


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
        "AGENTS_EFFORT",
        "AGENTS_REASONING_EFFORT",
        "AGENTS_WEB_PORT",
        "AGENTS_WEB_TOKEN",
        "AGENTS_AGENT_TOKEN",
        "AGENTS_API_URL",
        "AGENTS_EXECUTION_ID",
        "AGENTS_ISOLATION",
        "AGENTS_CONTAINER_IMAGE",
        "AGENTS_BROKER_SECRETS_ROOT",
        "AGENTS_SECRETS_API_URL",
        "AGENTS_SECRETS_TRANSPORT",
    ):
        os.environ.pop(name, None)
    fixture_bin = xdg / "fixture-bin"
    fixture_bin.mkdir(parents=True, mode=0o700)
    mock_wrapper = fixture_bin / "mock_cli"
    mock_wrapper.write_text(
        f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FIXTURE_BIN / 'mock_cli')!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    mock_wrapper.chmod(0o700)
    fixture_path = str(fixture_bin)
    broker_root = xdg / "broker"
    broker_root.mkdir(parents=True, mode=0o700)
    os.environ.update(
        {
            "AGENTS_CONFIG": str(config_path),
            "AGENTS_PROVIDER": "mock",
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "COLIMA_HOME": old.get("COLIMA_HOME", str(Path(old["HOME"]) / ".colima")),
            "PATH": fixture_path + os.pathsep + old.get("PATH", ""),
        }
    )
    if config_path.is_file() and 'isolation = "container"' in config_path.read_text(encoding="utf-8"):
        os.environ["AGENTS_BROKER_SECRETS_ROOT"] = str(broker_root)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def _prepare_runtime(runtime: Path, config_path: Path, smoke_instance: str | None = None) -> AgentsConfig:
    (runtime / "agents").mkdir(parents=True)
    for profile in (ROOT / "agents").glob("*.md"):
        shutil.copy2(profile, runtime / "agents" / profile.name)
    smoke_web = runtime / "src" / "agents" / "web.py"
    smoke_web.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "src" / "agents" / "web.py", smoke_web)
    (runtime / ".venv" / "bin").mkdir(parents=True)
    entrypoints = {"agentsd": "agents.web", "agents-mcp-server": "agents.mcp_server"}
    for name, module in entrypoints.items():
        executable = runtime / ".venv" / "bin" / name
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT / 'src')!r})\n"
            f"from {module} import main\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    config = load(config_path, env={"AGENTS_PROVIDER": "mock"})
    if str(config.execution.isolation) == "container":
        (runtime / ".env.schema").write_text(
            "# @defaultSensitive=false @defaultRequired=false\n# ---\n# @sensitive\nTEST_SECRET=\n",
            encoding="utf-8",
        )
        (runtime / ".env.local").write_text("", encoding="utf-8")
        (runtime / ".env.local").chmod(0o600)
        tool_environment = {
            "AGENTS_BROKER_SECRETS_ROOT": os.environ["AGENTS_BROKER_SECRETS_ROOT"],
            "HOME": str(TOOL_HOME),
            "PATH": os.environ["PATH"],
        }
        prepared = subprocess.run(
            [
                "mise",
                "exec",
                "age",
                "sops",
                "varlock",
                "--",
                sys.executable,
                "-c",
                (
                    "import sys;"
                    "from agents.secret_store import init_store,resolve_paths,set_secret_value;"
                    f"paths=resolve_paths(__import__('pathlib').Path({str(runtime)!r}));"
                    "init_store(paths);set_secret_value(paths,'TEST_SECRET',sys.stdin.buffer.read())"
                ),
            ],
            input=b"smoke-only-secret",
            capture_output=True,
            env=tool_environment,
            check=False,
        )
        if prepared.returncode != 0:
            detail = prepared.stderr.decode("utf-8", errors="replace").strip()
            raise SmokeFailure("secret fixture", detail or "unable to initialize isolated managed secret")
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
        if smoke_instance:
            connection.execute("UPDATE project SET instance_id=? WHERE id=1", (smoke_instance,))
            connection.commit()
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
        or reviews[0]["actor_slug"] != "researcher"
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


def run(isolation: str = "host", smoke_instance: str = "") -> None:
    config: AgentsConfig | None = None
    parent = ROOT if isolation == "container" or os.environ.get("AGENTS_SYSTEM_CONTAINER") == "1" else Path("/tmp")
    original_initialize = ContainerRuntime.initialize

    def initialize_fixture(runtime: ContainerRuntime, _repository: Path, instance: str, api_port: int) -> None:
        original_initialize(runtime, ROOT, instance, api_port)

    runtime_context = (
        mock.patch.object(ContainerRuntime, "initialize", initialize_fixture)
        if isolation == "container"
        else contextlib.nullcontext()
    )
    with (
        tempfile.TemporaryDirectory(
            prefix=".agents-smoke-" if isolation == "container" else "agents-smoke-", dir=parent
        ) as temporary,
        tempfile.TemporaryDirectory(prefix="agents-smoke-home-", dir="/tmp") as private_temporary,
        runtime_context,
    ):
        runtime = Path(temporary)
        project = runtime / "project"
        config_path = runtime / "agents.toml"
        home = Path(private_temporary) / "home"
        xdg = Path(private_temporary) / "xdg"
        home.mkdir(mode=0o700)
        xdg.mkdir(mode=0o700)
        _make_project(project)
        web_port = int(os.environ["AGENTS_SMOKE_API_PORT"]) if isolation == "container" else _free_port()
        _config_file(ROOT / "agents.toml", config_path, web_port, isolation)
        # The copied config's relative project path now points at runtime/project.
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace('path = "."', 'path = "project"'), encoding="utf-8"
        )
        with _isolated_environment(config_path, home, xdg):
            config = _prepare_runtime(runtime, config_path, smoke_instance or None)
            persistent_agent_count = sum(
                1 for actor in config.actors if actor["kind"] == "agent" and actor.get("persistent")
            )
            _stage("isolated project and state prepared", str(project))
            client = None
            web = None
            started = False
            try:
                service.start(config)
                started = True
                web = httpx.Client(base_url=f"http://127.0.0.1:{config.web.port}", timeout=10.0)
                _wait(
                    "Agents and Herdr services ready",
                    lambda: service.status(config) == {"agentsd": True, "herdr": True},
                )
                doctor_errors = doctor(config)
                if doctor_errors:
                    raise SmokeFailure("doctor", "; ".join(doctor_errors))
                _stage("doctor validated Herdr binary, schema, socket, provider, and ownership")
                _wait(
                    "persistent agents live",
                    lambda: (
                        len(
                            _db_rows(
                                config, "SELECT id FROM terminal_runs WHERE purpose_kind='persistent' AND state='live'"
                            )
                        )
                        == persistent_agent_count
                    ),
                )
                retained = _db_rows(
                    config,
                    "SELECT actor_slug,generation,backend_run_id,backend_terminal_id FROM terminal_runs "
                    "WHERE purpose_kind='persistent' AND state='live' ORDER BY actor_slug",
                )
                if isolation == "container":
                    _assert_container_boundary(config)
                service.stop(config)
                if service.status(config) != {"agentsd": False, "herdr": True}:
                    raise SmokeFailure("retention", "ordinary stop did not retain Herdr")
                service.start(config)
                _wait(
                    "retained actors reconnected",
                    lambda: (
                        service.status(config) == {"agentsd": True, "herdr": True}
                        and _db_rows(
                            config,
                            "SELECT actor_slug,generation,backend_run_id,backend_terminal_id FROM terminal_runs "
                            "WHERE purpose_kind='persistent' AND state='live' ORDER BY actor_slug",
                        )
                        == retained
                    ),
                )
                service.stop(config)
                service.stop_herdr(config)
                service.start(config)
                _wait(
                    "full Herdr restart fenced old actors",
                    lambda: (
                        len(
                            _db_rows(
                                config,
                                "SELECT id FROM terminal_runs WHERE purpose_kind='persistent' AND state='live' "
                                "AND generation>1",
                            )
                        )
                        == persistent_agent_count
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
                        "body": "@manager Please refine Smoke delivery for the deterministic smoke.",
                    },
                    "manager wake",
                )
                _wait(
                    "manager refinement started",
                    lambda: (
                        (_db_row(config, "SELECT status FROM work_items WHERE id=?", (item_id,)) or {}).get("status")
                        in {
                            "refining",
                            "ready",
                            "in_progress",
                            "verifying",
                            "awaiting_approval",
                            "accepted",
                            "delivered",
                        }
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
                            "WHERE e.work_id=? AND r.gate='research' AND r.actor_slug='researcher' AND r.verdict='pass'",
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
                if isolation == "container":
                    _assert_live_identity_conflict(config)
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
                for log_name in ("agentsd.log", "herdr.log"):
                    log_path = config.state_dir / log_name
                    if log_path.exists():
                        _stage(log_name, log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
                raise
            except Exception as exc:
                _stage("unexpected diagnostics", repr(exc))
                for log_name in ("agentsd.log", "herdr.log"):
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
                # service.shutdown has already closed and confirmed every mapped workspace.
                prefix = f"agents-{instance}-"
                live = _db_rows(
                    config,
                    "SELECT execution_name,state,token_revoked_at FROM terminal_runs "
                    "WHERE execution_name LIKE ? AND state IN ('reserved','creating','live','retained')",
                    (f"{prefix}%",),
                )
                if live:
                    raise SmokeFailure("cleanup", f"mapped terminal rows remain active: {live}")
                _stage("services stopped; no mapped Herdr workspaces survive")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("herdr",), default="herdr")
    parser.add_argument("--isolation", choices=("host", "container"), default="host")
    parser.add_argument("--instance", default="")
    args = parser.parse_args()
    try:
        run(args.isolation, args.instance)
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
