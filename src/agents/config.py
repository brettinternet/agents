from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

_PROVIDER_MAP = {"opencode": "opencode_cli", "claude": "claude_code", "mock": "mock_cli"}
_FORBIDDEN_ARG_CHARS = frozenset("$`; &|<>") - {" "}
_ALLOWED_ENV = {
    "AGENTS_CONFIG",
    "AGENTS_PROVIDER",
    "AGENTS_MODEL",
    "AGENTS_EFFORT",
    "AGENTS_WEB_PORT",
    "AGENTS_WEB_TOKEN",
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")

_EFFORT = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SCHEDULE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SCHEDULE_DURATION = re.compile(r"^([1-9][0-9]*)([mhd])$")
_SCHEDULE_CHANNELS = frozenset({"#general", "#findings", "#publishing", "#coordination", "#incidents"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    path: Path
    default_branch: str
    verify: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class RuntimeConfig:
    poll_seconds: int
    stall_seconds: int
    launch_budget_per_hour: int
    max_agents: int
    max_consultations: int
    worker_grace_seconds: int


@dataclass(frozen=True)
class ModelChoice:
    id: str
    effort: str = ""


@dataclass(frozen=True)
class ExecutionConfig:
    backend: str
    version: str
    session: str | None
    provider: str
    provider_id: str
    models: tuple[ModelChoice, ...]


@dataclass(frozen=True)
class ScheduledWorkConfig:
    kind: str
    title: str
    problem: str
    outcome: str


@dataclass(frozen=True)
class ScheduleConfig:
    slug: str
    timezone: str
    cron: str = ""
    every_seconds: int = 0
    overlap: str = "skip"
    to: str = ""
    message: str = ""
    work: ScheduledWorkConfig | None = None


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int


@dataclass(frozen=True)
class AgentsConfig:
    source: Path
    root: Path
    project: ProjectConfig
    runtime: RuntimeConfig
    execution: ExecutionConfig
    web: WebConfig
    actors: tuple[dict[str, Any], ...]
    schedules: tuple[ScheduleConfig, ...] = ()
    actor_models: tuple[tuple[str, tuple[ModelChoice, ...]], ...] = ()

    def models_for(self, actor_slug: str) -> tuple[ModelChoice, ...]:
        for slug, models in self.actor_models:
            if slug == actor_slug:
                return models
        return self.execution.models

    @property
    def state_dir(self) -> Path:
        return self.root / ".agents"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "agents.db"

    @property
    def herdr_config(self) -> Path:
        return self.state_dir / "herdr.toml"

    @property
    def execution_session(self) -> str | None:
        return resolve_execution_session(self)


def resolve_execution_session(config: AgentsConfig, connection: sqlite3.Connection | None = None) -> str | None:
    """Return the configured session or derive it from the persisted project identity."""
    configured = config.execution.session
    if configured:
        return configured
    if connection is not None:
        row = connection.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
        return f"agents-{row[0]}" if row is not None and row[0] else None
    if not config.db_path.is_file():
        return None
    try:
        with sqlite3.connect(config.db_path) as local:
            row = local.execute("SELECT instance_id FROM project WHERE id=1").fetchone()
    except sqlite3.Error:
        return None
    return f"agents-{row[0]}" if row is not None and row[0] else None


def _integer(value: object, name: str, minimum: int = 1, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ConfigError(f"{name} must be an integer in range {minimum}..{maximum or 'unbounded'}")
    return value


def _verify(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("project.verify must be a nonempty array of argv arrays")
    commands: list[tuple[str, ...]] = []
    for entry in value:
        if not isinstance(entry, list) or not entry:
            raise ConfigError("each project.verify entry must be a nonempty argv array")
        argv: list[str] = []
        for arg in entry:
            if (
                not isinstance(arg, str)
                or not arg
                or any(token in arg for token in ("\x00", "\r", "\n", "$", "`", ";", "&", "|", "<", ">"))
            ):
                raise ConfigError("verification arguments must be nonempty literal strings without shell syntax")
            argv.append(arg)
        commands.append(tuple(argv))
    return tuple(commands)


def _model_choice(model: object, effort: object = "") -> ModelChoice:
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        raise ConfigError("execution model IDs must match ^[A-Za-z0-9._:/-]{1,128}$")
    if not isinstance(effort, str) or (effort and not _EFFORT.fullmatch(effort)):
        raise ConfigError("execution effort must match ^[A-Za-z0-9._-]{1,32}$")
    return ModelChoice(model, effort)


def _models(
    section: dict[str, Any],
    values: Mapping[str, str],
    provider: str,
    name: str = "execution",
) -> tuple[ModelChoice, ...]:
    if "AGENTS_REASONING_EFFORT" in values:
        raise ConfigError("AGENTS_REASONING_EFFORT was renamed to AGENTS_EFFORT")
    if "reasoning_effort" in section:
        raise ConfigError(f"{name}.reasoning_effort was renamed to {name}.effort")
    models = section.get("models")
    if isinstance(models, list) and any(isinstance(entry, dict) and "reasoning_effort" in entry for entry in models):
        raise ConfigError(f"{name}.models reasoning_effort was renamed to effort")
    env_model = values.get("AGENTS_MODEL")
    env_effort = values.get("AGENTS_EFFORT", "")
    if env_model:
        choices = (_model_choice(env_model, env_effort),)
    elif env_effort:
        raise ConfigError("AGENTS_EFFORT requires AGENTS_MODEL")
    else:
        model = section.get("model")
        effort = section.get("effort", "")
        if model is not None and models is not None:
            raise ConfigError(f"{name}.model and {name}.models are mutually exclusive")
        if models is not None:
            if effort:
                raise ConfigError(f"{name}.effort cannot be combined with {name}.models")
            if not isinstance(models, list) or not models:
                raise ConfigError(f"{name}.models must be a nonempty array of model tables")
            parsed: list[ModelChoice] = []
            for entry in models:
                if not isinstance(entry, dict) or set(entry) - {"id", "effort"} or "id" not in entry:
                    raise ConfigError(f"each {name}.models entry requires id and optionally effort")
                parsed.append(_model_choice(entry["id"], entry.get("effort", "")))
            choices = tuple(parsed)
        elif model is not None:
            choices = (_model_choice(model, effort),)
        elif effort:
            raise ConfigError(f"{name}.effort requires {name}.model")
        else:
            choices = (ModelChoice(""),)
    if len(choices) != len(set(choices)):
        raise ConfigError(f"{name}.models contains duplicate model/effort choices")
    if provider != "opencode" and any(choice.effort for choice in choices):
        raise ConfigError("effort is supported only by the opencode provider")
    return choices


def _actor_models(
    actors: tuple[dict[str, Any], ...],
    values: Mapping[str, str],
    provider: str,
) -> tuple[tuple[str, tuple[ModelChoice, ...]], ...]:
    configured: list[tuple[str, tuple[ModelChoice, ...]]] = []
    for actor in actors:
        if not {"model", "models", "effort", "reasoning_effort"}.intersection(actor):
            continue
        slug = str(actor["slug"])
        if actor.get("kind") != "agent":
            raise ConfigError(f"actor {slug} model choices require kind='agent'")
        models = _models(actor, {}, provider, f"actor {slug}")
        if not values.get("AGENTS_MODEL"):
            configured.append((slug, models))
    return tuple(configured)


def _schedules(value: object, actors: tuple[dict[str, Any], ...]) -> tuple[ScheduleConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("schedules must be an array of tables")
    persistent = {str(actor["slug"]) for actor in actors if actor.get("kind") == "agent" and actor.get("persistent")}
    parsed: list[ScheduleConfig] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError("each schedule must be a table")
        unknown = set(entry) - {"slug", "to", "message", "work", "timezone", "cron", "every", "overlap"}
        if unknown:
            raise ConfigError(f"schedule contains unknown field: {sorted(unknown)[0]}")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SCHEDULE_SLUG.fullmatch(slug):
            raise ConfigError("schedule.slug must use 1-64 lowercase letters, digits, or hyphens")
        work_raw = entry.get("work")
        work: ScheduledWorkConfig | None = None
        to = entry.get("to", "")
        message = entry.get("message", "")
        if work_raw is not None:
            if to or message or not isinstance(work_raw, dict):
                raise ConfigError(f"schedule {slug}.work cannot be combined with to or message")
            if set(work_raw) != {"kind", "title", "problem", "outcome"}:
                raise ConfigError(f"schedule {slug}.work requires kind, title, problem, and outcome")
            if not isinstance(work_raw["kind"], str) or work_raw["kind"] not in {"story", "bug", "task", "spike"}:
                raise ConfigError(f"schedule {slug}.work.kind is invalid")
            for name in ("title", "problem", "outcome"):
                text = work_raw[name]
                maximum = 200 if name == "title" else 16 * 1024
                if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= maximum or "\x00" in text:
                    raise ConfigError(f"schedule {slug}.work.{name} must be 1..{maximum} UTF-8 bytes without NUL")
            work = ScheduledWorkConfig(
                kind=work_raw["kind"],
                title=work_raw["title"],
                problem=work_raw["problem"],
                outcome=work_raw["outcome"],
            )
        else:
            if not isinstance(to, str) or (
                (to.startswith("@") and to[1:] not in persistent)
                or (to.startswith("#") and to not in _SCHEDULE_CHANNELS)
                or not to.startswith(("@", "#"))
            ):
                raise ConfigError(f"schedule {slug}.to must name a persistent agent or known channel")
            if not isinstance(message, str) or not 1 <= len(message.encode("utf-8")) <= 16 * 1024 or "\x00" in message:
                raise ConfigError(f"schedule {slug}.message must be 1..16384 UTF-8 bytes without NUL")
        timezone = entry.get("timezone", "UTC")
        if not isinstance(timezone, str):
            raise ConfigError(f"schedule {slug}.timezone must be an IANA timezone")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigError(f"schedule {slug}.timezone must be an IANA timezone") from exc
        has_cron = "cron" in entry
        has_every = "every" in entry
        if has_cron == has_every:
            raise ConfigError(f"schedule {slug} requires exactly one of cron or every")
        expression = entry.get("cron", "")
        every = entry.get("every", "")
        every_seconds = 0
        if has_cron:
            if not isinstance(expression, str) or len(expression.split()) != 5 or not croniter.is_valid(expression):
                raise ConfigError(f"schedule {slug}.cron must be a valid five-field cron expression")
        else:
            match = _SCHEDULE_DURATION.fullmatch(every) if isinstance(every, str) else None
            if match is None:
                raise ConfigError(f"schedule {slug}.every must be a positive duration such as 30m, 1h, or 1d")
            multiplier = {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
            every_seconds = int(match.group(1)) * multiplier
            if every_seconds > 365 * 86400:
                raise ConfigError(f"schedule {slug}.every cannot exceed 365d")
        overlap = entry.get("overlap", "skip")
        if overlap != "skip":
            raise ConfigError(f"schedule {slug}.overlap currently supports only 'skip'")
        parsed.append(
            ScheduleConfig(
                slug=slug,
                to=to,
                message=message,
                work=work,
                timezone=timezone,
                cron=expression,
                every_seconds=every_seconds,
                overlap=overlap,
            )
        )
    slugs = [schedule.slug for schedule in parsed]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("schedules must have unique slugs")
    return tuple(parsed)


def load(path: Path | None = None, env: dict[str, str] | None = None) -> AgentsConfig:
    values = os.environ if env is None else env
    source = Path(path or values.get("AGENTS_CONFIG", "agents.toml")).expanduser().resolve()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"unable to load {source}: {exc}") from exc
    root = source.parent
    project_raw = raw.get("project")
    runtime_raw = raw.get("runtime")
    execution_raw = raw.get("execution")
    web_raw = raw.get("web")
    actors = raw.get("actors")
    if (
        not isinstance(project_raw, dict)
        or not isinstance(runtime_raw, dict)
        or not isinstance(execution_raw, dict)
        or not isinstance(web_raw, dict)
        or not isinstance(actors, list)
    ):
        raise ConfigError("agents.toml is missing required sections")
    backend = str(execution_raw.get("backend", "herdr"))
    if backend != "herdr":
        raise ConfigError(f"unsupported execution backend: {backend}")
    version = execution_raw.get("version", "0.8.2")
    if not isinstance(version, str) or not version:
        raise ConfigError("execution.version must be a nonempty string")
    session_value = execution_raw.get("session")
    if session_value == "":
        session_value = None
    if session_value is not None and (
        not isinstance(session_value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", session_value)
    ):
        raise ConfigError("execution.session must be a Herdr session name")
    provider = values.get("AGENTS_PROVIDER", str(execution_raw.get("provider", "")))
    if provider not in _PROVIDER_MAP:
        raise ConfigError(f"unsupported provider: {provider}")
    web_port_value: object = values["AGENTS_WEB_PORT"] if "AGENTS_WEB_PORT" in values else web_raw.get("port", 0)
    try:
        web_port = _integer(int(cast(str | int, web_port_value)), "web.port", maximum=65535)
    except (TypeError, ValueError) as exc:
        raise ConfigError("configured ports must be integers") from exc
    host = str(web_raw.get("host", ""))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError("web.host must be loopback")
    project_path = (root / str(project_raw.get("path", ""))).resolve()
    actor_rows = tuple(dict(row) for row in actors if isinstance(row, dict))
    slugs = [str(row.get("slug", "")) for row in actor_rows]
    if len(actor_rows) != len(actors) or not slugs or len(slugs) != len(set(slugs)) or any(not slug for slug in slugs):
        raise ConfigError("actors must have unique nonempty slugs")
    return AgentsConfig(
        source=source,
        root=root,
        project=ProjectConfig(
            str(project_raw.get("name", "")),
            project_path,
            str(project_raw.get("default_branch", "")),
            _verify(project_raw.get("verify")),
        ),
        runtime=RuntimeConfig(
            *(
                _integer(runtime_raw[name], f"runtime.{name}")
                for name in (
                    "poll_seconds",
                    "stall_seconds",
                    "launch_budget_per_hour",
                    "max_agents",
                    "max_consultations",
                    "worker_grace_seconds",
                )
            ),
        ),
        execution=ExecutionConfig(
            backend=backend,
            version=version,
            session=session_value,
            provider=provider,
            provider_id=_PROVIDER_MAP[provider],
            models=_models(execution_raw, values, provider),
        ),
        web=WebConfig(host, web_port),
        actors=actor_rows,
        schedules=_schedules(raw.get("schedules"), actor_rows),
        actor_models=_actor_models(actor_rows, values, provider),
    )
