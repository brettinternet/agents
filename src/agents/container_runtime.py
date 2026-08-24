from __future__ import annotations

import calendar
import hashlib
import ipaddress
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .config import AgentsConfig, ContainerConfig, IsolationMode
from .execution import (
    BackendHealth,
    ExecutionBackend,
    ExecutionConflict,
    ExecutionNotFound,
    ExecutionTerminated,
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
    try:
        result = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ContainerRuntimeError(f"{argv[0]} is unavailable: {exc.strerror}") from None
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ContainerRuntimeError(f"{argv[0]} failed: {message}")
    return result


def _resource_missing(exc: ContainerRuntimeError) -> bool:
    message = str(exc).lower()
    return "no such" in message or "not found" in message


def _path_has_symlink(path: Path) -> bool:
    current = path.absolute()
    while True:
        if current.is_symlink():
            return True
        if current == current.parent:
            return False
        current = current.parent


class ContainerRuntime:
    def __init__(self, config: ContainerConfig) -> None:
        self.config = config

    def validate_colima_version(self) -> None:
        result = _completed(("colima", "version"))
        first_line = result.stdout.splitlines()[0].split() if result.stdout.splitlines() else []
        if (
            len(first_line) != 3
            or first_line[:2] != ["colima", "version"]
            or first_line[2].removeprefix("v") != "0.10.3"
        ):
            raise ContainerRuntimeError(
                "Colima 0.10.3 is required; operator action: install the pinned version with `mise install colima@0.10.3`"
            )

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
        environment = os.environ.copy()
        environment.pop("DOCKER_CONTEXT", None)
        environment["DOCKER_HOST"] = socket if socket.startswith("unix://") else f"unix://{socket}"
        return environment

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

    def remove_container(self, name: str, expected_id: str) -> None:
        current = self.inspect_container(name)
        if current is None or current.get("Id") != expected_id:
            raise ContainerRuntimeError(f"container {name!r} identity changed before removal")
        self.docker("container", "rm", "--force", expected_id)
        if self.inspect_container(expected_id) is not None:
            raise ContainerRuntimeError(f"container {name!r} still exists after removal")

    def initialize(self, repository: Path, instance: str, api_port: int) -> None:
        self.validate_colima_version()
        profile_state = self.profile_state()
        profile_exists = profile_state is not None
        if profile_state == "Stopped":
            _completed(
                (
                    "colima",
                    "--profile",
                    self.config.colima_profile,
                    "start",
                    "--activate=false",
                    "--ssh-agent=false",
                    "--network-address=false",
                    "--save-config=false",
                )
            )
        elif profile_state is None:
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
        elif profile_state != "Running":
            raise ContainerRuntimeError(f"Colima profile is in unsupported state {profile_state!r}")
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
        if not any(marker in line and "rw," in line for line in mount_lines) or set(host_targets) != {str(repository)}:
            raise ContainerRuntimeError(
                "Colima profile must expose only the configured repository via writable VirtioFS"
            )
        ssh_agent = self._ssh("printenv", "SSH_AUTH_SOCK", check=False)
        if ssh_agent.returncode == 0 and ssh_agent.stdout.strip():
            raise ContainerRuntimeError("Colima profile forwards an SSH agent")
        container_ids = self.docker("container", "ls", "--all", "--quiet")
        for container_id in container_ids.splitlines():
            inspected = self.inspect_container(container_id)
            labels = inspected.get("Config", {}).get("Labels") if inspected is not None else None
            if not isinstance(labels, dict) or labels.get(_INSTANCE_LABEL) != instance:
                raise ContainerRuntimeError("Colima profile contains a workload not owned by this Agents instance")
        if not profile_exists:
            self._ssh_input(
                b"net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n",
                "sudo",
                "tee",
                "/etc/sysctl.d/99-agents-disable-ipv6.conf",
            )
            self._ssh("sudo", "sysctl", "--system")
        for key in ("net.ipv6.conf.all.disable_ipv6", "net.ipv6.conf.default.disable_ipv6"):
            if self._ssh("sysctl", "-n", key).stdout.strip() != "1":
                raise ContainerRuntimeError("existing Colima profile has IPv6 enabled")
        persisted_ipv6 = self._ssh("cat", "/etc/sysctl.d/99-agents-disable-ipv6.conf", check=False)
        if (
            persisted_ipv6.returncode
            or persisted_ipv6.stdout != "net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n"
        ):
            raise ContainerRuntimeError("existing Colima profile has no persistent IPv6-disable configuration")
        self._ensure_network("agents-runs", "172.30.0.0/24", instance, create=not profile_exists)
        self._ensure_network("agents-system", "172.30.1.0/28", instance, create=not profile_exists)
        self._install_firewall(instance, api_port, create=not profile_exists)

    def profile_state(self) -> str | None:
        result = _completed(("colima", "list", "--json"))
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContainerRuntimeError("colima returned malformed profile list JSON") from exc
            if isinstance(value, dict) and value.get("name") == self.config.colima_profile:
                status = value.get("status")
                if not isinstance(status, str):
                    raise ContainerRuntimeError("colima profile has no status")
                return status
        return None

    def _profile_exists(self) -> bool:
        return self.profile_state() is not None

    def _ensure_network(self, name: str, subnet: str, instance: str, *, create: bool) -> None:
        try:
            output = self.docker("network", "inspect", name)
        except ContainerRuntimeError as exc:
            if not _resource_missing(exc):
                raise
            if not create:
                raise ContainerRuntimeError(f"existing Docker network {name!r} is missing") from exc
            self.docker(
                "network",
                "create",
                "--driver",
                "bridge",
                "--ipv6=false",
                "--subnet",
                subnet,
                "--label",
                f"{_INSTANCE_LABEL}={instance}",
                name,
            )
            return
        values = json.loads(output)
        expected_gateway = str(next(ipaddress.ip_network(subnet).hosts()))
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ContainerRuntimeError(f"existing Docker network {name!r} has an unexpected shape")
        network = values[0]
        ipam = network.get("IPAM")
        ipam_config = ipam.get("Config") if isinstance(ipam, dict) else None
        labels = network.get("Labels")
        if (
            network.get("Driver") != "bridge"
            or not isinstance(ipam_config, list)
            or len(ipam_config) != 1
            or not isinstance(ipam_config[0], dict)
            or ipam_config[0].get("Subnet") != subnet
            or ipam_config[0].get("Gateway") != expected_gateway
            or not isinstance(labels, dict)
            or labels.get(_INSTANCE_LABEL) != instance
            or network.get("EnableIPv6") is not False
        ):
            raise ContainerRuntimeError(f"existing Docker network {name!r} has an unexpected shape")

    def verify_api_reachable(self, api_port: int, instance: str) -> None:
        try:
            self.docker(
                "run",
                "--rm",
                "--label",
                f"{_INSTANCE_LABEL}={instance}",
                "--network",
                "agents-runs",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "/opt/agents/.venv/bin/python",
                self.config.image,
                "-c",
                (
                    "import urllib.request;"
                    f"urllib.request.urlopen('http://host.docker.internal:{api_port}/health',timeout=2)"
                ),
            )
        except ContainerRuntimeError as exc:
            raise ContainerRuntimeError(f"provider container cannot reach the loopback Agents listener: {exc}") from exc

    def trim(self) -> None:
        _completed(("colima", "--profile", self.config.colima_profile, "ssh", "--", "sudo", "fstrim", "-a"))

    def _ssh(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ("colima", "--profile", self.config.colima_profile, "ssh", "--", *argv)
        if check:
            return _completed(command)
        return subprocess.run(command, capture_output=True, text=True, check=False, start_new_session=True)

    def _ssh_input(self, data: bytes, *argv: str) -> subprocess.CompletedProcess[bytes]:
        command = ("colima", "--profile", self.config.colima_profile, "ssh", "--", *argv)
        result = subprocess.run(
            command,
            input=data,
            capture_output=True,
            check=False,
            start_new_session=True,
        )
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip() or f"exit {result.returncode}"
            raise ContainerRuntimeError(f"{argv[0]} failed: {detail}")
        return result

    def _ensure_firewall_rule(
        self,
        rule: tuple[str, ...],
        *,
        chain: str = "DOCKER-USER",
        insert: bool = False,
        command: str = "iptables",
        create: bool = True,
    ) -> None:
        if self._ssh("sudo", command, "-C", chain, *rule, check=False).returncode == 0:
            return
        if not create:
            raise ContainerRuntimeError(f"existing Colima profile is missing an expected {command} rule")
        action = "-I" if insert else "-A"
        position = ("1",) if insert else ()
        self._ssh("sudo", command, action, chain, *position, *rule)

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

    def _install_firewall(self, instance: str, api_port: int, *, create: bool) -> None:
        comment = f"agents:{instance}"
        host_gateway = self._host_gateway()
        chain = f"AGENTS-{instance[:12].upper()}"
        if create:
            self._ssh("sudo", "iptables", "-N", chain)
            self._ssh("sudo", "ip6tables", "-N", f"AGENTS6-{instance[:12].upper()}")
        elif (
            self._ssh("sudo", "iptables", "-S", chain, check=False).returncode
            or self._ssh("sudo", "ip6tables", "-S", f"AGENTS6-{instance[:12].upper()}", check=False).returncode
        ):
            raise ContainerRuntimeError("existing Colima profile is missing an Agents firewall chain")
        ipv4_jump = ("-s", "172.30.0.0/23", "-m", "comment", "--comment", comment, "-j", chain)
        self._ensure_firewall_rule(ipv4_jump, insert=True, create=create)
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
            create=create,
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
            create=create,
        )
        self._ensure_firewall_rule(
            (
                "-s",
                "172.30.0.0/24",
                "-p",
                "tcp",
                "--dport",
                str(api_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ),
            chain="INPUT",
            insert=True,
            create=create,
        )
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
            self._ensure_firewall_rule(rule, chain=chain, create=create)
        ipv6_chain = f"AGENTS6-{instance[:12].upper()}"
        ipv6_jump = ("-m", "comment", "--comment", comment, "-j", ipv6_chain)
        self._ensure_firewall_rule(ipv6_jump, insert=True, command="ip6tables", create=create)
        self._ensure_firewall_rule(
            ("-m", "comment", "--comment", comment, "-j", "REJECT"),
            chain=ipv6_chain,
            command="ip6tables",
            create=create,
        )

        def chain_rules(command: str, target: str) -> list[tuple[str, ...]]:
            output = self._ssh("sudo", command, "-S", target).stdout
            parsed: list[tuple[str, ...]] = []
            for line in output.splitlines():
                tokens = shlex.split(line)
                if len(tokens) >= 2 and tokens[:2] == ["-A", target]:
                    parsed.append(tuple(tokens[2:]))
            return parsed

        def signature(rule: tuple[str, ...]) -> tuple[str, str, str, str, str]:
            def value(option: str) -> str:
                try:
                    return rule[rule.index(option) + 1]
                except ValueError, IndexError:
                    return ""

            return value("-s"), value("-d"), value("--dport"), value("--ctstate"), value("-j")

        def owned(rule: tuple[str, ...]) -> bool:
            try:
                return rule[rule.index("--comment") + 1] == comment
            except ValueError, IndexError:
                return False

        actual_chain = chain_rules("iptables", chain)
        if (
            len(actual_chain) != len(rules)
            or not all(owned(rule) for rule in actual_chain)
            or [signature(rule) for rule in actual_chain] != [signature(rule) for rule in rules]
        ):
            raise ContainerRuntimeError("existing Colima profile has an unexpected Agents IPv4 firewall chain")
        docker_user = chain_rules("iptables", "DOCKER-USER")
        if not docker_user or not owned(docker_user[0]) or signature(docker_user[0]) != signature(ipv4_jump):
            raise ContainerRuntimeError("existing Colima profile has an unsafe DOCKER-USER firewall order")
        expected_input = [
            (
                "-s",
                "172.30.0.0/24",
                "-p",
                "tcp",
                "--dport",
                str(api_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ),
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
        ]
        actual_input = chain_rules("iptables", "INPUT")[:3]
        if (
            len(actual_input) != 3
            or not all(owned(rule) for rule in actual_input)
            or [signature(rule) for rule in actual_input] != [signature(rule) for rule in expected_input]
        ):
            raise ContainerRuntimeError("existing Colima profile has an unsafe INPUT firewall order")
        ipv6_rules = chain_rules("ip6tables", ipv6_chain)
        if len(ipv6_rules) != 1 or not owned(ipv6_rules[0]) or signature(ipv6_rules[0])[-1] != "REJECT":
            raise ContainerRuntimeError("existing Colima profile has an unexpected Agents IPv6 firewall chain")
        ip6_user = chain_rules("ip6tables", "DOCKER-USER")
        if not ip6_user or not owned(ip6_user[0]) or signature(ip6_user[0]) != signature(ipv6_jump):
            raise ContainerRuntimeError("existing Colima profile has an unsafe IPv6 DOCKER-USER firewall order")


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
        protected_images.update(
            str(row[0])
            for row in self.connection.execute(
                "SELECT tr.container_image_id FROM terminal_runs tr "
                "WHERE tr.execution_backend='herdr-container' AND tr.container_image_id LIKE 'sha256:%' "
                "AND (tr.state IN ('reserved','creating','live','retained','ending') "
                "OR EXISTS (SELECT 1 FROM launch_attempts la WHERE la.terminal_run_id=tr.id "
                "AND la.state IN ('posting','uncertain')))"
            )
        )
        all_containers = self.runtime.docker(
            "container",
            "ls",
            "--all",
            "--filter",
            f"label={_INSTANCE_LABEL}={self.instance}",
            "--format",
            "{{json .}}",
        )
        owned_containers: list[tuple[str, dict[str, Any]]] = []
        for line in all_containers.splitlines():
            item = json.loads(line)
            name = str(item.get("Names", ""))
            inspect = self.runtime.inspect_container(name)
            if inspect is None:
                continue
            owned_containers.append((name, inspect))
            image_id = str(inspect.get("Image", ""))
            if image_id.startswith("sha256:"):
                protected_images.add(image_id)
        removed_containers: list[str] = []
        now = time.time()
        for name, inspect in owned_containers:
            labels = inspect.get("Config", {}).get("Labels", {})
            owned_instance = isinstance(labels, dict) and labels.get(_INSTANCE_LABEL) == self.instance
            execution = labels.get("dev.agents.execution")
            retention = labels.get(_RETENTION_LABEL)
            image_id = str(inspect.get("Image", ""))
            if execution in active and image_id.startswith("sha256:"):
                protected_images.add(image_id)
            topology = labels.get("dev.agents.topology")
            state = inspect.get("State", {})
            running = bool(state.get("Running"))
            age_value = str(
                inspect.get("Created") if running else state.get("FinishedAt") or inspect.get("Created") or ""
            )
            try:
                age_epoch = calendar.timegm(time.strptime(age_value[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                age_epoch = now
            if (
                owned_instance
                and isinstance(execution, str)
                and execution
                and execution not in active
                and topology is None
                and retention == "ephemeral"
                and now - age_epoch >= self.container.gc_grace_seconds
            ):
                container_id = inspect.get("Id")
                if not isinstance(container_id, str) or not container_id:
                    raise ContainerRuntimeError(f"container {name!r} has no immutable identity")
                self.runtime.remove_container(name, container_id)
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
                try:
                    confirmed = json.loads(self.runtime.docker("volume", "inspect", name))
                except json.JSONDecodeError as exc:
                    raise ContainerRuntimeError(f"docker returned malformed volume identity for {name!r}") from exc
                if confirmed != values:
                    continue
                self.runtime.docker("volume", "rm", name)
                removed_volumes.append(name)
        cleanup_errors: list[str] = []
        workspaces = self.connection.execute(
            "SELECT DISTINCT tr.id terminal_run_id,tr.execution_backend,e.worktree_path,"
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
                isolation = (
                    IsolationMode.CONTAINER if row["execution_backend"] == "herdr-container" else IsolationMode.HOST
                )
                cleanup_config = replace(
                    self.config,
                    execution=replace(self.config.execution, isolation=isolation),
                )
                remove_recorded_workspace(
                    cleanup_config,
                    cleanup_config.project.path,
                    Path(str(row["worktree_path"])),
                    str(row["target_sha"]),
                )
            except (GitError, OSError) as exc:
                cleanup_errors.append(str(exc))
        for row in self.connection.execute(
            "SELECT tr.id,tr.execution_name,tr.agent_auth_id,tr.generation FROM terminal_runs tr "
            "WHERE tr.state IN ('ended','failed') AND tr.agent_auth_id IS NOT NULL "
            "AND tr.execution_backend='herdr-container' "
            "AND NOT EXISTS (SELECT 1 FROM launch_attempts la WHERE la.terminal_run_id=tr.id "
            "AND la.state IN ('posting','uncertain'))"
        ):
            runtime_root = self.config.state_dir / "runtime"
            runtime_dir = runtime_root / str(row["agent_auth_id"])
            try:
                if self.connection.execute(
                    "SELECT 1 FROM launch_attempts WHERE terminal_run_id=? AND state IN ('posting','uncertain')",
                    (row["id"],),
                ).fetchone():
                    continue
                if (
                    runtime_root.is_symlink()
                    or not runtime_root.is_dir()
                    or runtime_dir.parent != runtime_root
                    or runtime_dir.is_symlink()
                    or self.runtime.inspect_container(
                        container_name(self.instance, int(row["id"]), int(row["generation"]))
                    )
                    is not None
                ):
                    raise ContainerRuntimeError(f"refusing unsafe runtime cleanup for terminal {row['id']}")
                execution_name = str(row["execution_name"])
                manifest_dir = runtime_root / "containers"
                manifest_path = manifest_dir / f"{hashlib.sha256(execution_name.encode()).hexdigest()}.json"
                if (
                    manifest_dir.is_symlink()
                    or (manifest_dir.exists() and not manifest_dir.is_dir())
                    or manifest_path.parent != manifest_dir
                    or manifest_path.is_symlink()
                ):
                    raise ContainerRuntimeError(f"refusing unsafe manifest cleanup for terminal {row['id']}")
                if runtime_dir.is_dir() and not manifest_path.exists():
                    raise ContainerRuntimeError(f"refusing runtime cleanup without manifest for terminal {row['id']}")
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    labels = manifest.get("labels") if isinstance(manifest, dict) else None
                    if (
                        not isinstance(labels, dict)
                        or manifest.get("execution_name") != execution_name
                        or manifest.get("container_name")
                        != container_name(self.instance, int(row["id"]), int(row["generation"]))
                        or manifest.get("runtime_dir") != str(runtime_dir.resolve())
                        or labels.get(_INSTANCE_LABEL) != self.instance
                        or labels.get("dev.agents.execution") != execution_name
                        or labels.get("dev.agents.run_id") != str(row["id"])
                        or labels.get("dev.agents.generation") != str(row["generation"])
                    ):
                        raise ContainerRuntimeError(f"refusing mismatched manifest cleanup for terminal {row['id']}")
                if runtime_dir.is_dir():
                    shutil.rmtree(runtime_dir)
                if manifest_path.exists():
                    manifest_path.unlink()
            except (ContainerRuntimeError, OSError, json.JSONDecodeError) as exc:
                cleanup_errors.append(str(exc))
        retention = self.container.build_cache_retention_hours
        removed_images: list[str] = []
        image_rows = self.runtime.docker(
            "image",
            "ls",
            "--filter",
            "dangling=true",
            "--format",
            "{{json .}}",
        )
        for line in image_rows.splitlines():
            try:
                item = json.loads(line)
                values = json.loads(self.runtime.docker("image", "inspect", str(item["ID"])))
            except (KeyError, json.JSONDecodeError) as exc:
                raise ContainerRuntimeError("docker returned malformed dangling image identity") from exc
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                raise ContainerRuntimeError("docker returned an unexpected dangling image identity")
            image = values[0]
            image_id = str(image.get("Id") or "")
            created = str(image.get("Created") or "")
            if not image_id.startswith("sha256:"):
                raise ContainerRuntimeError("docker returned a dangling image without immutable identity")
            try:
                created_epoch = calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError as exc:
                raise ContainerRuntimeError(f"docker returned malformed creation time for image {image_id}") from exc
            if image_id not in protected_images and now - created_epoch >= retention * 3600:
                self.runtime.docker("image", "rm", image_id)
                removed_images.append(image_id)
        self.runtime.docker("builder", "prune", "--force", "--filter", f"until={retention}h")
        for image_id in protected_images:
            if self.runtime.resolve_image_id(image_id) != image_id:
                raise ContainerRuntimeError(f"garbage collection changed protected image {image_id}")
        trim_error = ""
        try:
            self.runtime.trim()
        except (ContainerRuntimeError, OSError) as exc:
            trim_error = str(exc)
        return {
            "containers": removed_containers,
            "volumes": removed_volumes,
            "trim_error": trim_error,
            "images": removed_images,
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
        runtime_root = self.config.state_dir / "runtime"
        if _path_has_symlink(runtime_root):
            raise ExecutionConflict("container_wrapper", "managed runtime root is unsafe")
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.wrapper_dir.is_symlink() or (self.wrapper_dir.exists() and not self.wrapper_dir.is_dir()):
            raise ExecutionConflict("container_wrapper", "container wrapper directory is unsafe")
        self.wrapper_dir.mkdir(mode=0o700, exist_ok=True)
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
        try:
            pids_limit = int(host.get("PidsLimit") or 0)
            nano_cpus = int(host.get("NanoCpus") or 0)
            memory = int(host.get("Memory") or 0)
        except (TypeError, ValueError) as exc:
            raise ExecutionConflict("container_identity", f"container {name!r} has malformed resource limits") from exc
        if pids_limit != self.container.pids_limit:
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched PID limit")
        if nano_cpus != int(self.container.cpus * 1_000_000_000):
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched CPU limit")
        if memory != self.container.memory_mb * 1024 * 1024:
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched memory limit")
        if host.get("NetworkMode") != "agents-runs":
            raise ExecutionConflict("container_identity", f"container {name!r} has a mismatched network")
        security_options = host.get("SecurityOpt") or ()
        if "ALL" not in (host.get("CapDrop") or ()):
            raise ExecutionConflict("container_identity", f"container {name!r} retains Linux capabilities")
        if (
            "no-new-privileges" not in security_options
            or any(str(option).startswith("seccomp=") for option in security_options)
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

    def _verify_running(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        inspect = self._verify(manifest)
        state = inspect.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise ExecutionTerminated(
                "container_exited",
                f"container {manifest.get('container_name')!r} is not running",
            )
        return inspect

    def _verify_live(self, run: RunSnapshot) -> RunSnapshot:
        manifest = self._read_manifest(run.handle.name)
        if manifest is None:
            raise ExecutionConflict("container_manifest", f"container manifest for {run.handle.name!r} is absent")
        if manifest.get("execution_name") != run.handle.name:
            raise ExecutionConflict("container_manifest", f"container manifest for {run.handle.name!r} is mismatched")
        self._verify(manifest)
        return run

    def verified_container_name(
        self,
        execution_name: str,
        terminal_run_id: int,
        generation: int,
        image_id: str | None = None,
    ) -> str:
        manifest = self._read_manifest(execution_name)
        expected = container_name(self.instance, terminal_run_id, generation)
        if (
            manifest is None
            or manifest.get("execution_name") != execution_name
            or manifest.get("container_name") != expected
            or (image_id is not None and manifest.get("image_id") != image_id)
        ):
            raise ExecutionConflict("container_manifest", f"container manifest for {execution_name!r} is mismatched")
        self._verify_running(manifest)
        return expected

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
        if Path(execution_id).name != execution_id:
            raise ExecutionConflict("container_identity", "run has an invalid execution identity")
        runtime_root_path = self.config.state_dir / "runtime"
        if _path_has_symlink(runtime_root_path):
            raise ExecutionConflict("container_runtime", "managed runtime root is unsafe")
        runtime_root_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime_root = runtime_root_path.resolve()
        raw_runtime_dir = runtime_root_path / execution_id
        if _path_has_symlink(raw_runtime_dir):
            raise ExecutionConflict("container_runtime", f"unsafe runtime directory: {raw_runtime_dir}")
        raw_runtime_dir.mkdir(mode=0o700, exist_ok=True)
        runtime_dir = raw_runtime_dir.resolve()
        if runtime_dir.parent != runtime_root:
            raise ExecutionConflict("container_runtime", "runtime directory escaped the managed root")
        home = runtime_dir / "home"
        provider = runtime_dir / "provider"
        for path in (home, provider):
            if _path_has_symlink(path):
                raise ExecutionConflict("container_runtime", f"unsafe runtime directory: {path}")
            path.mkdir(mode=0o700, exist_ok=True)
        image_id = spec.container_image_id or self.runtime.resolve_image_id(self.container.image)
        if spec.container_image_id and image_id != spec.container_image_id:
            raise ExecutionConflict("container_image", "reserved container image identity changed")
        name = container_name(self.instance, spec.terminal_run_id, spec.generation)
        raw_cwd = spec.cwd
        if _path_has_symlink(raw_cwd) or not raw_cwd.is_dir():
            raise ExecutionConflict("container_runtime", f"unsafe execution workspace: {raw_cwd}")
        cwd = str(raw_cwd.resolve())
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
        if self.manifest_dir.is_symlink() or (self.manifest_dir.exists() and not self.manifest_dir.is_dir()):
            raise ExecutionConflict("container_manifest", "container manifest directory is unsafe")
        self.manifest_dir.mkdir(mode=0o700, exist_ok=True)
        path = self._manifest_path(spec.name)
        existing = self._read_manifest(spec.name)
        if existing is not None:
            if existing != manifest:
                raise ExecutionConflict("container_manifest", "existing container manifest identity differs")
            if self.runtime.inspect_container(name) is not None:
                self._verify_running(existing)
        else:
            if self.runtime.inspect_container(name) is not None:
                raise ExecutionConflict(
                    "container_identity",
                    f"container name {name!r} is already occupied without an ownership manifest",
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
            except OSError as exc:
                raise ExecutionConflict("container_manifest", "container manifest path is unsafe") from exc
            try:
                os.write(descriptor, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
            finally:
                os.close(descriptor)
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
                self._verify_running(manifest)
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
        self.cleanup_run(handle.name, handle)

    def cleanup_run(
        self,
        execution_name: str,
        handle: RunHandle | None = None,
        *,
        remove_runtime: bool = True,
    ) -> None:
        """Remove an exact manifest-owned container before closing its Herdr workspace."""
        manifest = self._read_manifest(execution_name)
        if manifest is None:
            if handle is None:
                return
            raise ExecutionConflict(
                "container_manifest",
                f"container manifest for {execution_name!r} is absent; refusing to close the backing run",
            )
        runtime_dir = Path(str(manifest.get("runtime_dir", "")))
        runtime_root = (self.config.state_dir / "runtime").resolve()
        if not runtime_dir.is_absolute() or runtime_dir.is_symlink() or runtime_dir.parent.resolve() != runtime_root:
            raise ExecutionConflict("container_manifest", "container runtime directory is outside the managed root")
        try:
            inspect = self._verify(manifest)
        except ExecutionNotFound:
            pass
        else:
            container_id = inspect.get("Id")
            if not isinstance(container_id, str) or not container_id:
                raise ExecutionConflict("container_identity", "container has no immutable identity")
            self.runtime.remove_container(str(manifest["container_name"]), container_id)
        target = handle
        if target is None:
            snapshot = self.inner.find_run(execution_name)
            target = snapshot.handle if snapshot is not None else None
        if target is not None:
            self.inner.delete_run(target)
        if remove_runtime and runtime_dir.is_dir() and not runtime_dir.is_symlink():
            shutil.rmtree(runtime_dir)
        self._manifest_path(execution_name).unlink(missing_ok=True)

    def close(self) -> None:
        self.inner.close()

    def events(self):
        return self.inner.events()


def build_execution_backend(config: AgentsConfig) -> ExecutionBackend:
    inner = HerdrBackend.from_config(config)
    if config.execution.isolation is IsolationMode.HOST:
        return inner
    return ContainerizedHerdrBackend(config, inner)
