from __future__ import annotations

import calendar
import hashlib
import ipaddress
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .config import AgentsConfig, ContainerConfig, IsolationMode
from .execution import (
    BackendHealth,
    ExecutionBackend,
    ExecutionConflict,
    ExecutionNotFound,
    ExecutionUnavailable,
    RunHandle,
    RunSnapshot,
    RunSpec,
)
from .git_worktree import GitError, remove_recorded_workspace
from .herdr_client import HerdrBackend

_INSTANCE_LABEL = "dev.agents.instance"
_RETENTION_LABEL = "dev.agents.retention"


class ContainerRuntimeError(RuntimeError):
    pass


def _completed(argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        tuple(argv),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        start_new_session=True,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ContainerRuntimeError(f"{argv[0]} failed: {message}")
    return result


def _resource_missing(exc: ContainerRuntimeError) -> bool:
    message = str(exc).lower()
    return "no such" in message or "not found" in message


class ContainerRuntime:
    def __init__(self, config: ContainerConfig) -> None:
        self.config = config

    def status(self) -> dict[str, Any]:
        result = _completed(("colima", "--profile", self.config.colima_profile, "status", "--json"))
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ContainerRuntimeError("colima returned malformed status JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("docker_socket"), str):
            raise ContainerRuntimeError("colima status has no docker_socket")
        return value

    def docker_environment(self) -> dict[str, str]:
        socket = str(self.status()["docker_socket"])
        return {**os.environ, "DOCKER_HOST": socket if socket.startswith("unix://") else f"unix://{socket}"}

    def docker(self, *args: str) -> str:
        return _completed(("docker", *args), env=self.docker_environment()).stdout.strip()

    def resolve_image_id(self, image: str) -> str:
        image_id = self.docker("image", "inspect", "--format", "{{.Id}}", image)
        if not image_id.startswith("sha256:"):
            raise ContainerRuntimeError(f"Docker returned an invalid image ID for {image!r}")
        return image_id

    def inspect_container(self, name: str) -> dict[str, Any] | None:
        try:
            output = self.docker("container", "inspect", name)
        except ContainerRuntimeError as exc:
            if _resource_missing(exc):
                return None
            raise
        try:
            values = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ContainerRuntimeError("docker inspect returned malformed JSON") from exc
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ContainerRuntimeError("docker inspect returned an unexpected container set")
        return values[0]

    def remove_container(self, name: str) -> None:
        self.docker("container", "rm", "--force", name)
        if self.inspect_container(name) is not None:
            raise ContainerRuntimeError(f"container {name!r} still exists after removal")

    def initialize(self, repository: Path, instance: str, api_port: int) -> None:
        try:
            status = self.status()
        except ContainerRuntimeError:
            _completed(
                (
                    "colima",
                    "--profile",
                    self.config.colima_profile,
                    "start",
                    "--runtime",
                    "docker",
                    "--vm-type",
                    "vz",
                    "--mount-type",
                    "virtiofs",
                    "--kubernetes=false",
                    "--activate=false",
                    "--ssh-agent=false",
                    "--network-address=false",
                    "--mount",
                    f"{repository.resolve()}:w",
                )
            )
            status = self.status()
        if (
            status.get("driver") != "macOS Virtualization.Framework"
            or status.get("arch") not in {"aarch64", "arm64"}
            or status.get("runtime") != "docker"
            or status.get("mount_type") != "virtiofs"
            or status.get("kubernetes") is not False
        ):
            raise ContainerRuntimeError(
                "existing Colima profile must use Apple Virtualization, arm64, Docker, VirtioFS, and disabled Kubernetes"
            )
        repository = repository.resolve()
        mounted = self._ssh("test", "-d", str(repository), "-a", "-w", str(repository))
        if mounted.returncode:
            raise ContainerRuntimeError("existing Colima profile does not mount the repository read-write")
        mounts = self._ssh("mount", "-t", "virtiofs")
        mount_lines = [line for line in mounts.stdout.splitlines() if line.strip()]
        marker = f" on {repository} type virtiofs ("
        host_targets = []
        for line in mount_lines:
            if " on " in line and " type virtiofs " in line:
                host_targets.append(line.split(" on ", 1)[1].split(" type virtiofs ", 1)[0])
        if not any(marker in line and "rw," in line for line in mount_lines) or any(
            target != str(repository)
            for target in host_targets
            if target.startswith(("/Users/", "/Volumes/", "/private/"))
        ):
            raise ContainerRuntimeError(
                "Colima profile must expose the configured repository, and no wider host path, via VirtioFS"
            )
        ssh_agent = self._ssh("printenv", "SSH_AUTH_SOCK", check=False)
        if ssh_agent.returncode == 0 and ssh_agent.stdout.strip():
            raise ContainerRuntimeError("Colima profile forwards an SSH agent")
        running = self.docker("container", "ls", "--format", "{{json .}}")
        for line in running.splitlines():
            try:
                labels = str(json.loads(line).get("Labels", ""))
            except (AttributeError, json.JSONDecodeError) as exc:
                raise ContainerRuntimeError("docker returned malformed workload identity") from exc
            if f"{_INSTANCE_LABEL}=" not in labels:
                raise ContainerRuntimeError("Colima profile contains a running workload not owned by Agents")
        self._ensure_network("agents-runs", "172.30.0.0/24", instance)
        self._ensure_network("agents-system", "172.30.1.0/28", instance)
        self._install_firewall(instance, api_port)

    def _ensure_network(self, name: str, subnet: str, instance: str) -> None:
        try:
            output = self.docker("network", "inspect", name)
        except ContainerRuntimeError as exc:
            if not _resource_missing(exc):
                raise
            self.docker(
                "network",
                "create",
                "--driver",
                "bridge",
                "--subnet",
                subnet,
                "--label",
                f"{_INSTANCE_LABEL}={instance}",
                name,
            )
            return
        values = json.loads(output)
        if (
            not isinstance(values, list)
            or len(values) != 1
            or values[0].get("IPAM", {}).get("Config", [{}])[0].get("Subnet") != subnet
            or values[0].get("Labels", {}).get(_INSTANCE_LABEL) != instance
            or values[0].get("EnableIPv6") is not False
        ):
            raise ContainerRuntimeError(f"existing Docker network {name!r} has an unexpected shape")

    def verify_api_reachable(self, api_port: int) -> None:
        result = self._ssh(
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "2",
            f"http://host.lima.internal:{api_port}/health",
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise ContainerRuntimeError(f"Colima cannot reach the loopback Agents listener: {detail}")

    def trim(self) -> None:
        _completed(("colima", "--profile", self.config.colima_profile, "ssh", "--", "sudo", "fstrim", "-a"))

    def _ssh(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ("colima", "--profile", self.config.colima_profile, "ssh", "--", *argv)
        if check:
            return _completed(command)
        return subprocess.run(command, capture_output=True, text=True, check=False, start_new_session=True)

    def _ensure_firewall_rule(
        self,
        rule: tuple[str, ...],
        *,
        chain: str = "DOCKER-USER",
        insert: bool = False,
    ) -> None:
        if self._ssh("sudo", "iptables", "-C", chain, *rule, check=False).returncode == 0:
            return
        action = "-I" if insert else "-A"
        position = ("1",) if insert else ()
        self._ssh("sudo", "iptables", action, chain, *position, *rule)

    def _host_gateway(self) -> str:
        output = self._ssh("getent", "ahostsv4", "host.lima.internal").stdout
        for line in output.splitlines():
            field = line.split(maxsplit=1)[0] if line.split() else ""
            try:
                address = ipaddress.IPv4Address(field)
            except ipaddress.AddressValueError:
                continue
            if not address.is_loopback and not address.is_unspecified and not address.is_multicast:
                return str(address)
        raise ContainerRuntimeError("Colima did not report a usable host gateway address")

    def _install_firewall(self, instance: str, api_port: int) -> None:
        comment = f"agents:{instance}"
        host_gateway = self._host_gateway()
        chain = f"AGENTS-{instance[:12].upper()}"
        self._ssh("sudo", "iptables", "-N", chain, check=False)
        stale_jump = ("-m", "comment", "--comment", comment, "-j", chain)
        while self._ssh("sudo", "iptables", "-D", "DOCKER-USER", *stale_jump, check=False).returncode == 0:
            pass
        self._ensure_firewall_rule(
            ("-s", "172.30.0.0/23", "-m", "comment", "--comment", comment, "-j", chain),
            insert=True,
        )
        self._ensure_firewall_rule(
            (
                "-s",
                "172.30.0.0/23",
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "REJECT",
            ),
            chain="INPUT",
            insert=True,
        )
        self._ensure_firewall_rule(
            (
                "-s",
                "172.30.0.0/23",
                "-m",
                "comment",
                "--comment",
                comment,
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            ),
            chain="INPUT",
            insert=True,
        )
        self._ssh("sudo", "iptables", "-F", chain)
        rules = [
            (
                "-m",
                "comment",
                "--comment",
                comment,
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            ),
            (
                "-m",
                "comment",
                "--comment",
                comment,
                "-s",
                "172.30.0.0/24",
                "-d",
                f"{host_gateway}/32",
                "-p",
                "tcp",
                "--dport",
                str(api_port),
                "-j",
                "ACCEPT",
            ),
            (
                "-m",
                "comment",
                "--comment",
                comment,
                "-s",
                "172.30.1.2/32",
                "-d",
                "172.30.1.3/32",
                "-p",
                "tcp",
                "--dport",
                "9891",
                "-j",
                "ACCEPT",
            ),
            (
                "-m",
                "comment",
                "--comment",
                comment,
                "-s",
                "172.30.0.0/24",
                "-d",
                "172.30.0.0/24",
                "-j",
                "REJECT",
            ),
        ]
        for destination in (
            "10.0.0.0/8",
            "100.64.0.0/10",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "224.0.0.0/4",
        ):
            rules.append(
                (
                    "-m",
                    "comment",
                    "--comment",
                    comment,
                    "-d",
                    destination,
                    "-j",
                    "REJECT",
                )
            )
        rules.append(("-m", "comment", "--comment", comment, "-j", "RETURN"))
        for rule in rules:
            self._ensure_firewall_rule(rule, chain=chain)


def _instance_id(config: AgentsConfig) -> str:
    try:
        connection = sqlite3.connect(config.db_path)
        try:
            row = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ExecutionUnavailable("instance_missing", "Agents project identity is unavailable") from exc
    if row is None or not row[0]:
        raise ExecutionUnavailable("instance_missing", "Agents project identity is unavailable")
    return str(row[0])


class ContainerGarbageCollector:
    def __init__(
        self, config: AgentsConfig, connection: sqlite3.Connection, runtime: ContainerRuntime | None = None
    ) -> None:
        if config.execution.container is None:
            raise ValueError("container configuration is required")
        self.config = config
        self.container = config.execution.container
        self.connection = connection
        self.runtime = runtime or ContainerRuntime(config.execution.container)
        self.instance = _instance_id(config)

    def collect(self) -> dict[str, Any]:
        active = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT tr.execution_name FROM terminal_runs tr "
                "WHERE tr.state IN ('reserved','creating','live','retained','ending') "
                "OR EXISTS (SELECT 1 FROM launch_attempts la WHERE la.terminal_run_id=tr.id "
                "AND la.state IN ('posting','uncertain'))"
            )
        }
        protected_images = {self.runtime.resolve_image_id(self.container.image)}
        removed_containers: list[str] = []
        rows = self.runtime.docker(
            "container",
            "ls",
            "--all",
            "--filter",
            f"label={_INSTANCE_LABEL}={self.instance}",
            "--format",
            "{{json .}}",
        )
        now = time.time()
        for line in rows.splitlines():
            item = json.loads(line)
            name = str(item.get("Names", ""))
            inspect = self.runtime.inspect_container(name)
            if inspect is None:
                continue
            labels = inspect.get("Config", {}).get("Labels", {})
            execution = labels.get("dev.agents.execution")
            retention = labels.get(_RETENTION_LABEL)
            image_id = str(inspect.get("Image", ""))
            if execution in active and image_id.startswith("sha256:"):
                protected_images.add(image_id)
            topology = labels.get("dev.agents.topology")
            state = inspect.get("State", {})
            finished = str(state.get("FinishedAt") or "")
            try:
                finished_epoch = calendar.timegm(time.strptime(finished[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                finished_epoch = now
            if (
                isinstance(execution, str)
                and execution
                and execution not in active
                and topology is None
                and retention == "ephemeral"
                and not bool(state.get("Running"))
                and now - finished_epoch >= self.container.gc_grace_seconds
            ):
                self.runtime.remove_container(name)
                removed_containers.append(name)
        volume_rows = self.runtime.docker(
            "volume",
            "ls",
            "--filter",
            f"label={_INSTANCE_LABEL}={self.instance}",
            "--filter",
            f"label={_RETENTION_LABEL}=ephemeral",
            "--format",
            "{{.Name}}",
        )
        removed_volumes: list[str] = []
        for name in volume_rows.splitlines():
            if not name:
                continue
            try:
                values = json.loads(self.runtime.docker("volume", "inspect", name))
            except json.JSONDecodeError as exc:
                raise ContainerRuntimeError(f"docker returned malformed volume identity for {name!r}") from exc
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                raise ContainerRuntimeError(f"docker returned an unexpected volume identity for {name!r}")
            volume = values[0]
            label_value = volume.get("Labels")
            labels = cast(Mapping[str, Any], label_value) if isinstance(label_value, dict) else {}
            created = str(volume.get("CreatedAt") or "")
            try:
                created_epoch = calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                created_epoch = now
            execution = labels.get("dev.agents.execution")
            if (
                labels.get(_INSTANCE_LABEL) == self.instance
                and labels.get(_RETENTION_LABEL) == "ephemeral"
                and labels.get("dev.agents.topology") is None
                and isinstance(execution, str)
                and execution
                and execution not in active
                and now - created_epoch >= self.container.gc_grace_seconds
            ):
                self.runtime.docker("volume", "rm", name)
                removed_volumes.append(name)
        cleanup_errors: list[str] = []
        workspaces = self.connection.execute(
            "SELECT DISTINCT tr.id terminal_run_id,e.worktree_path,"
            "COALESCE((SELECT s.commit_sha FROM submissions s WHERE s.execution_id=e.id "
            "ORDER BY s.id DESC LIMIT 1),e.base_sha) target_sha "
            "FROM terminal_runs tr JOIN assignments a ON a.terminal_run_id=tr.id "
            "JOIN executions e ON e.id=a.execution_id "
            "WHERE tr.state IN ('ended','failed') AND e.state IN ('closed','superseded') "
            "AND NOT EXISTS (SELECT 1 FROM launch_attempts la WHERE la.terminal_run_id=tr.id "
            "AND la.state IN ('posting','uncertain'))"
        )
        for row in workspaces:
            try:
                if self.connection.execute(
                    "SELECT 1 FROM launch_attempts WHERE terminal_run_id=? AND state IN ('posting','uncertain')",
                    (row["terminal_run_id"],),
                ).fetchone():
                    continue
                remove_recorded_workspace(
                    self.config,
                    self.config.project.path,
                    Path(str(row["worktree_path"])),
                    str(row["target_sha"]),
                )
            except (GitError, OSError) as exc:
                cleanup_errors.append(str(exc))
        for row in self.connection.execute(
            "SELECT tr.id,tr.agent_auth_id,tr.generation FROM terminal_runs tr "
            "WHERE tr.state IN ('ended','failed') AND tr.agent_auth_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM launch_attempts la WHERE la.terminal_run_id=tr.id "
            "AND la.state IN ('posting','uncertain'))"
        ):
            runtime_dir = self.config.state_dir / "runtime" / str(row["agent_auth_id"])
            expected = (self.config.state_dir / "runtime").resolve()
            try:
                if self.connection.execute(
                    "SELECT 1 FROM launch_attempts WHERE terminal_run_id=? AND state IN ('posting','uncertain')",
                    (row["id"],),
                ).fetchone():
                    continue
                if (
                    runtime_dir.is_symlink()
                    or runtime_dir.parent.resolve() != expected
                    or self.runtime.inspect_container(
                        container_name(self.instance, int(row["id"]), int(row["generation"]))
                    )
                    is not None
                ):
                    raise ContainerRuntimeError(f"refusing unsafe runtime cleanup for terminal {row['id']}")
                if runtime_dir.is_dir():
                    shutil.rmtree(runtime_dir)
            except (ContainerRuntimeError, OSError) as exc:
                cleanup_errors.append(str(exc))
        retention = self.container.build_cache_retention_hours
        self.runtime.docker("image", "prune", "--force", "--filter", f"until={retention}h")
        self.runtime.docker("builder", "prune", "--force", "--filter", f"until={retention}h")
        for image_id in protected_images:
            if self.runtime.resolve_image_id(image_id) != image_id:
                raise ContainerRuntimeError(f"garbage collection changed protected image {image_id}")
        trim_error = ""
        try:
            self.runtime.trim()
        except ContainerRuntimeError as exc:
            trim_error = str(exc)
        return {
            "containers": removed_containers,
            "volumes": removed_volumes,
            "trim_error": trim_error,
            "cleanup_errors": cleanup_errors,
        }


def container_name(instance: str, terminal_run_id: int, generation: int) -> str:
    return f"agents-{instance[:12]}-r{terminal_run_id}-g{generation}"


class ContainerizedHerdrBackend:
    def __init__(self, config: AgentsConfig, inner: HerdrBackend, runtime: ContainerRuntime | None = None) -> None:
        if config.execution.container is None:
            raise ValueError("container configuration is required")
        self.config = config
        self.container = config.execution.container
        self.inner = inner
        self.runtime = runtime or ContainerRuntime(self.container)
        self.instance = _instance_id(config)
        self.manifest_dir = config.state_dir / "runtime" / "containers"
        self.wrapper_dir = config.state_dir / "runtime" / "bin"

    def _manifest_path(self, execution_name: str) -> Path:
        digest = hashlib.sha256(execution_name.encode()).hexdigest()
        return self.manifest_dir / f"{digest}.json"

    def _read_manifest(self, execution_name: str) -> dict[str, Any] | None:
        path = self._manifest_path(execution_name)
        if not path.is_file() or path.is_symlink():
            return None
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ExecutionConflict("container_manifest", "container manifest is malformed") from exc
        return value if isinstance(value, dict) else None

    def _prepare_wrappers(self) -> None:
        self.wrapper_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        executable = shutil.which("agents-container-runner")
        candidate = Path(sys.executable).with_name("agents-container-runner")
        if executable is None and candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
        if executable is None:
            raise ExecutionUnavailable("container_runner_missing", "agents-container-runner is not installed")
        target = Path(executable).resolve()
        for name in ("opencode", "claude", "mock_cli"):
            link = self.wrapper_dir / name
            if link.is_symlink() and link.resolve() == target:
                continue
            if link.exists() or link.is_symlink():
                raise ExecutionConflict("container_wrapper", f"unsafe container wrapper path: {link}")
            link.symlink_to(target)

    def _verify(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        expected_labels = (
            cast(Mapping[str, Any], manifest["labels"]) if isinstance(manifest.get("labels"), dict) else {}
        )
        runtime_dir = Path(str(manifest.get("runtime_dir", "")))
        runtime_root = (self.config.state_dir / "runtime").resolve()
        if not runtime_dir.is_absolute() or runtime_dir.is_symlink() or runtime_dir.parent.resolve() != runtime_root:
            raise ExecutionConflict("container_manifest", "container runtime directory is outside the managed root")
        try:
            run_id = int(str(expected_labels["dev.agents.run_id"]))
            generation = int(str(expected_labels["dev.agents.generation"]))
        except (KeyError, ValueError) as exc:
            raise ExecutionConflict("container_manifest", "container manifest has invalid run identity") from exc
        expected_user = f"{os.getuid()}:{os.getgid()}"
        execution_name = manifest.get("execution_name")
        image_id = manifest.get("image_id")
        if (
            run_id <= 0
            or generation <= 0
            or not isinstance(execution_name, str)
            or not execution_name
            or not isinstance(image_id, str)
            or not image_id.startswith("sha256:")
            or manifest.get("container_name") != container_name(self.instance, run_id, generation)
            or manifest.get("user") != expected_user
            or expected_labels.get(_INSTANCE_LABEL) != self.instance
            or expected_labels.get("dev.agents.execution") != execution_name
            or expected_labels.get("dev.agents.image_id") != image_id
            or expected_labels.get(_RETENTION_LABEL) != "ephemeral"
            or expected_labels.get("dev.agents.cwd_sha256")
            != hashlib.sha256(str(manifest.get("cwd", "")).encode()).hexdigest()
        ):
            raise ExecutionConflict("container_manifest", "container manifest identity is inconsistent")
        name = str(manifest["container_name"])
        inspect = self.runtime.inspect_container(name)
        if inspect is None:
            raise ExecutionNotFound("container_not_found", f"container {name!r} is absent")
        config = cast(Mapping[str, Any], inspect["Config"]) if isinstance(inspect.get("Config"), dict) else {}
        host = cast(Mapping[str, Any], inspect["HostConfig"]) if isinstance(inspect.get("HostConfig"), dict) else {}
        labels = cast(Mapping[str, Any], config["Labels"]) if isinstance(config.get("Labels"), dict) else {}
        for key, expected in expected_labels.items():
            if labels.get(key) != expected:
                raise ExecutionConflict("container_identity", f"container {name!r} has mismatched label {key}")
        image_id = str(inspect.get("Image", ""))
        if image_id != manifest.get("image_id"):
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched image")
        if not bool(host.get("ReadonlyRootfs")):
            raise ExecutionConflict("container_identity", f"container {name!r} root filesystem is writable")
        if str(config.get("User", "")) != str(manifest.get("user", "")) or str(config.get("WorkingDir", "")) != str(
            manifest.get("cwd", "")
        ):
            raise ExecutionConflict(
                "container_identity", f"container {name!r} has a mismatched user or working directory"
            )
        if int(host.get("PidsLimit") or 0) != self.container.pids_limit:
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched PID limit")
        if int(host.get("NanoCpus") or 0) != int(self.container.cpus * 1_000_000_000):
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched CPU limit")
        if int(host.get("Memory") or 0) != self.container.memory_mb * 1024 * 1024:
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched memory limit")
        if host.get("NetworkMode") != "agents-runs":
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched network")
        security_options = host.get("SecurityOpt") or ()
        if "ALL" not in (host.get("CapDrop") or ()):
            raise ExecutionConflict("container_identity", f"container {name!r} retains Linux capabilities")
        if (
            "no-new-privileges" not in security_options
            or any(str(option).startswith("seccomp=unconfined") for option in security_options)
            or bool(host.get("Privileged"))
            or bool(host.get("Devices"))
            or bool(host.get("DeviceRequests"))
        ):
            raise ExecutionConflict(
                "container_identity", f"container {name!r} permits unsafe privilege or device access"
            )
        expected_tmpfs = {"/tmp", "/run"}
        tmpfs = host.get("Tmpfs")
        if (
            not isinstance(tmpfs, dict)
            or set(tmpfs) != expected_tmpfs
            or any(
                not {"rw", "noexec", "nosuid", "nodev"}.issubset(set(str(options).split(",")))
                for options in tmpfs.values()
            )
        ):
            raise ExecutionConflict("container_identity", f"container {name!r} has mismatched temporary filesystems")
        raw_mounts = inspect.get("Mounts")
        if not isinstance(raw_mounts, list):
            raise ExecutionConflict("container_identity", f"container {name!r} has malformed mounts")
        bind_mounts = {
            str(mount.get("Destination")): (str(mount.get("Source", "")), bool(mount.get("RW")))
            for mount in raw_mounts
            if isinstance(mount, dict) and mount.get("Type") == "bind"
        }
        expected_mounts = {
            str(manifest["cwd"]): (str(manifest["cwd"]), True),
            str(runtime_dir): (str(runtime_dir), True),
        }
        if bind_mounts != expected_mounts or any(
            isinstance(mount, dict) and mount.get("Type") not in {"bind", "tmpfs"} for mount in raw_mounts
        ):
            raise ExecutionConflict("container_identity", f"container {name!r} has mismatched bind mounts")
        return inspect

    def _verify_live(self, run: RunSnapshot) -> RunSnapshot:
        manifest = self._read_manifest(run.handle.name)
        if manifest is None:
            raise ExecutionConflict("container_manifest", f"container manifest for {run.handle.name!r} is absent")
        if manifest.get("execution_name") != run.handle.name:
            raise ExecutionConflict("container_manifest", f"container manifest for {run.handle.name!r} is mismatched")
        self._verify(manifest)
        return run

    def health(self) -> BackendHealth:
        inner = self.inner.health()
        if not inner.healthy:
            return inner
        try:
            self.runtime.status()
        except ContainerRuntimeError as exc:
            return BackendHealth(False, "container_runtime", message=str(exc))
        return inner

    def create_run(self, spec: RunSpec) -> RunSnapshot:
        self._prepare_wrappers()
        execution_id = spec.environment.get("AGENTS_EXECUTION_ID")
        if not execution_id:
            raise ExecutionConflict("container_identity", "run has no execution identity")
        runtime_dir = (self.config.state_dir / "runtime" / execution_id).resolve()
        home = runtime_dir / "home"
        provider = runtime_dir / "provider"
        for path in (runtime_dir, home, provider):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                raise ExecutionConflict("container_runtime", f"unsafe runtime directory: {path}")
        image_id = spec.container_image_id or self.runtime.resolve_image_id(self.container.image)
        if spec.container_image_id and image_id != spec.container_image_id:
            raise ExecutionConflict("container_image", "reserved container image identity changed")
        name = container_name(self.instance, spec.terminal_run_id, spec.generation)
        cwd = str(spec.cwd.resolve())
        cwd_hash = hashlib.sha256(cwd.encode()).hexdigest()
        labels = {
            _INSTANCE_LABEL: self.instance,
            "dev.agents.execution": spec.name,
            "dev.agents.run_id": str(spec.terminal_run_id),
            "dev.agents.generation": str(spec.generation),
            "dev.agents.cwd_sha256": cwd_hash,
            "dev.agents.image_id": image_id,
            _RETENTION_LABEL: "ephemeral",
        }
        manifest = {
            "execution_name": spec.name,
            "container_name": name,
            "image_id": image_id,
            "cwd": cwd,
            "runtime_dir": str(runtime_dir),
            "user": f"{os.getuid()}:{os.getgid()}",
            "labels": labels,
        }
        self.manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._manifest_path(spec.name)
        path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
        environment = dict(spec.env)
        environment.update(
            {
                "PATH": f"{self.wrapper_dir}:{environment.get('PATH', os.environ.get('PATH', ''))}",
                "HERDR_AGENT": spec.agent_kind,
                "AGENTS_CONTAINER_NAME": name,
                "AGENTS_CONTAINER_IMAGE_ID": image_id,
                "AGENTS_CONTAINER_PROFILE": self.container.colima_profile,
                "AGENTS_CONTAINER_CWD": cwd,
                "AGENTS_CONTAINER_RUNTIME": str(runtime_dir),
                "AGENTS_CONTAINER_USER": manifest["user"],
                "AGENTS_CONTAINER_CPUS": str(self.container.cpus),
                "AGENTS_CONTAINER_MEMORY_MB": str(self.container.memory_mb),
                "AGENTS_CONTAINER_PIDS": str(self.container.pids_limit),
                "AGENTS_CONTAINER_NETWORK": "agents-runs",
                "AGENTS_CONTAINER_LABELS": json.dumps(labels, sort_keys=True, separators=(",", ":")),
                "AGENTS_CONTAINER_ENV_NAMES": json.dumps(sorted(dict(spec.env))),
            }
        )
        argv = spec.argv
        if spec.mock:
            argv = (str(self.wrapper_dir / "mock_cli"), *spec.argv[1:])
        wrapped = RunSpec(
            spec.name,
            spec.terminal_run_id,
            spec.generation,
            spec.cwd,
            spec.agent_name,
            spec.agent_kind,
            argv,
            tuple(sorted(environment.items())),
            spec.provider,
            spec.mock,
            image_id,
        )
        run = self.inner.create_run(wrapped)
        for delay in (0.0, 0.1, 0.4, 1.0, 2.0, 3.0, 4.0):
            if delay:
                time.sleep(delay)
            try:
                self._verify(manifest)
                return run
            except ExecutionNotFound:
                continue
        raise ExecutionConflict("container_start", f"container {name!r} did not appear")

    def find_run(self, name: str) -> RunSnapshot | None:
        run = self.inner.find_run(name)
        return self._verify_live(run) if run is not None else None

    def get_run(self, handle: RunHandle, *, include_output: bool = False) -> RunSnapshot:
        return self._verify_live(self.inner.get_run(handle, include_output=include_output))

    def list_runs(self, prefix: str) -> tuple[RunSnapshot, ...]:
        return tuple(self._verify_live(run) for run in self.inner.list_runs(prefix))

    def send_message(self, handle: RunHandle, sender_id: str, message: str) -> str:
        self.get_run(handle)
        return self.inner.send_message(handle, sender_id, message)

    def send_input(self, handle: RunHandle, message: str) -> None:
        self.get_run(handle)
        self.inner.send_input(handle, message)

    def delete_run(self, handle: RunHandle) -> None:
        manifest = self._read_manifest(handle.name)
        runtime_dir: Path | None = None
        if manifest is not None:
            runtime_dir = Path(str(manifest.get("runtime_dir", "")))
            runtime_root = (self.config.state_dir / "runtime").resolve()
            if (
                not runtime_dir.is_absolute()
                or runtime_dir.is_symlink()
                or runtime_dir.parent.resolve() != runtime_root
            ):
                raise ExecutionConflict("container_manifest", "container runtime directory is outside the managed root")
            try:
                self._verify(manifest)
            except ExecutionNotFound:
                pass
            else:
                self.runtime.remove_container(str(manifest["container_name"]))
        self.inner.delete_run(handle)
        if manifest is not None and runtime_dir is not None:
            if runtime_dir.is_dir() and not runtime_dir.is_symlink():
                shutil.rmtree(runtime_dir)
            self._manifest_path(handle.name).unlink(missing_ok=True)

    def close(self) -> None:
        self.inner.close()

    def events(self):
        return self.inner.events()


def build_execution_backend(config: AgentsConfig) -> ExecutionBackend:
    inner = HerdrBackend.from_config(config)
    if config.execution.isolation is IsolationMode.HOST:
        return inner
    return ContainerizedHerdrBackend(config, inner)
