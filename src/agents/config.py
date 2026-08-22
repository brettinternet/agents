from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_PROVIDER_MAP = {"opencode": "opencode_cli", "claude": "claude_code", "mock": "mock_cli"}
_FORBIDDEN_ARG_CHARS = frozenset("$`; &|<>") - {" "}
_ALLOWED_ENV = {
    "AGENTS_CONFIG",
    "AGENTS_PROVIDER",
    "AGENTS_MODEL",
    "AGENTS_REASONING_EFFORT",
    "AGENTS_CAO_PORT",
    "AGENTS_WEB_PORT",
    "AGENTS_WEB_TOKEN",
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_REASONING_EFFORT = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


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
    reasoning_effort: str = ""


@dataclass(frozen=True)
class CaoConfig:
    version: str
    provider: str
    provider_id: str
    api_port: int
    models: tuple[ModelChoice, ...]


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
    cao: CaoConfig
    web: WebConfig
    actors: tuple[dict[str, Any], ...]
    actor_models: tuple[tuple[str, tuple[ModelChoice, ...]], ...] = ()

    def models_for(self, actor_slug: str) -> tuple[ModelChoice, ...]:
        for slug, models in self.actor_models:
            if slug == actor_slug:
                return models
        return self.cao.models

    @property
    def state_dir(self) -> Path:
        return self.root / ".agents"

    @property
    def cao_home(self) -> Path:
        return self.root / ".cao"


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


def _model_choice(model: object, reasoning_effort: object = "") -> ModelChoice:
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        raise ConfigError("cao model IDs must match ^[A-Za-z0-9._:/-]{1,128}$")
    if not isinstance(reasoning_effort, str) or (
        reasoning_effort and not _REASONING_EFFORT.fullmatch(reasoning_effort)
    ):
        raise ConfigError("cao reasoning_effort must match ^[A-Za-z0-9._-]{1,32}$")
    return ModelChoice(model, reasoning_effort)


def _models(
    section: dict[str, Any],
    values: Mapping[str, str],
    provider: str,
    name: str = "cao",
) -> tuple[ModelChoice, ...]:
    env_model = values.get("AGENTS_MODEL")
    env_reasoning = values.get("AGENTS_REASONING_EFFORT", "")
    if env_model:
        choices = (_model_choice(env_model, env_reasoning),)
    elif env_reasoning:
        raise ConfigError("AGENTS_REASONING_EFFORT requires AGENTS_MODEL")
    else:
        model = section.get("model")
        models = section.get("models")
        reasoning = section.get("reasoning_effort", "")
        if model is not None and models is not None:
            raise ConfigError(f"{name}.model and {name}.models are mutually exclusive")
        if models is not None:
            if reasoning:
                raise ConfigError(f"{name}.reasoning_effort cannot be combined with {name}.models")
            if not isinstance(models, list) or not models:
                raise ConfigError(f"{name}.models must be a nonempty array of model tables")
            parsed: list[ModelChoice] = []
            for entry in models:
                if not isinstance(entry, dict) or set(entry) - {"id", "reasoning_effort"} or "id" not in entry:
                    raise ConfigError(f"each {name}.models entry requires id and optionally reasoning_effort")
                parsed.append(_model_choice(entry["id"], entry.get("reasoning_effort", "")))
            choices = tuple(parsed)
        elif model is not None:
            choices = (_model_choice(model, reasoning),)
        elif reasoning:
            raise ConfigError(f"{name}.reasoning_effort requires {name}.model")
        else:
            choices = (ModelChoice(""),)
    if len(choices) != len(set(choices)):
        raise ConfigError(f"{name}.models contains duplicate model/reasoning choices")
    if provider != "opencode" and any(choice.reasoning_effort for choice in choices):
        raise ConfigError("reasoning_effort is supported only by the opencode provider")
    return choices


def _actor_models(
    actors: tuple[dict[str, Any], ...],
    values: Mapping[str, str],
    provider: str,
) -> tuple[tuple[str, tuple[ModelChoice, ...]], ...]:
    configured: list[tuple[str, tuple[ModelChoice, ...]]] = []
    for actor in actors:
        if not {"model", "models", "reasoning_effort"}.intersection(actor):
            continue
        slug = str(actor["slug"])
        if actor.get("kind") != "agent":
            raise ConfigError(f"actor {slug} model choices require kind='agent'")
        models = _models(actor, {}, provider, f"actor {slug}")
        if not values.get("AGENTS_MODEL"):
            configured.append((slug, models))
    return tuple(configured)


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
    cao_raw = raw.get("cao")
    web_raw = raw.get("web")
    actors = raw.get("actors")
    if (
        not isinstance(project_raw, dict)
        or not isinstance(runtime_raw, dict)
        or not isinstance(cao_raw, dict)
        or not isinstance(web_raw, dict)
        or not isinstance(actors, list)
    ):
        raise ConfigError("agents.toml is missing required sections")
    provider = values.get("AGENTS_PROVIDER", str(cao_raw.get("provider", "")))
    if provider not in _PROVIDER_MAP:
        raise ConfigError(f"unsupported provider: {provider}")
    cao_port_value: object = values["AGENTS_CAO_PORT"] if "AGENTS_CAO_PORT" in values else cao_raw.get("api_port", 0)
    web_port_value: object = values["AGENTS_WEB_PORT"] if "AGENTS_WEB_PORT" in values else web_raw.get("port", 0)
    try:
        cao_port = _integer(int(cast(str | int, cao_port_value)), "cao.api_port", maximum=65535)
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
            )
        ),
        cao=CaoConfig(
            str(cao_raw.get("version", "")),
            provider,
            _PROVIDER_MAP[provider],
            cao_port,
            _models(cao_raw, values, provider),
        ),
        web=WebConfig(host, web_port),
        actors=actor_rows,
        actor_models=_actor_models(actor_rows, values, provider),
    )
