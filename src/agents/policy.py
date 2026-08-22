from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WORK_KINDS = frozenset({"story", "bug", "task", "spike"})
PRIORITIES = ("urgent", "high", "normal", "low")
IMPLEMENTATION_SPECIALTIES = frozenset({"research", "publishing"})
CONSULTATION_SPECIALTIES = IMPLEMENTATION_SPECIALTIES | {"coordination"}
REVIEW_GATES = frozenset({"research", "publishing", "coordination"})
TERMINAL_STATES = frozenset({"delivered", "cancelled"})
WORK_STATES = frozenset(
    {
        "intake",
        "refining",
        "ready",
        "in_progress",
        "verifying",
        "awaiting_approval",
        "accepted",
        "delivered",
        "blocked",
        "cancelled",
    }
)


@dataclass(eq=False)
class DomainError(RuntimeError):
    code: str
    message: str
    current: Any | None = None

    def __str__(self) -> str:
        return self.message


def validate_text(value: object, name: str, minimum: int = 1, maximum: int = 16 * 1024) -> str:
    if not isinstance(value, str) or not minimum <= len(value.encode("utf-8")) <= maximum:
        raise DomainError("validation_failed", f"{name} must be {minimum}..{maximum} UTF-8 bytes")
    return value


def validate_title(value: object) -> str:
    return validate_text(value, "title", 1, 200)


def validate_request_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(not 0x20 <= ord(char) <= 0x7E for char in value):
        raise DomainError("invalid_request_id", "request_id must be 1-128 printable ASCII characters")
    return value


def validate_page(limit: int, maximum: int = 100) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise DomainError("validation_failed", f"limit must be 1..{maximum}")
    return limit


def validate_scope(
    *,
    kind: str,
    title: str,
    problem: str,
    outcome: str,
    priority: str,
    specialty: str,
    criteria: list[str],
    dependencies: list[str],
    gates: list[str],
) -> None:
    if kind not in WORK_KINDS:
        raise DomainError("validation_failed", "invalid work kind")
    validate_title(title)
    validate_text(problem, "problem")
    validate_text(outcome, "outcome")
    if priority not in PRIORITIES:
        raise DomainError("validation_failed", "invalid priority")
    if specialty not in IMPLEMENTATION_SPECIALTIES:
        raise DomainError("validation_failed", "invalid implementation specialty")
    if not 1 <= len(criteria) <= 50 or any(
        not isinstance(item, str) or not 1 <= len(item.encode()) <= 2048 for item in criteria
    ):
        raise DomainError("validation_failed", "acceptance criteria must contain 1-50 entries of at most 2 KiB")
    if len(dependencies) > 32 or len(dependencies) != len(set(dependencies)):
        raise DomainError("validation_failed", "dependencies must contain at most 32 unique IDs")
    if len(gates) != len(set(gates)) or not set(gates) <= REVIEW_GATES:
        raise DomainError("validation_failed", "review gates must be unique valid gates")


def authorize_transition(actor: str, source: str, target: str, *, assigned_actor: str | None = None) -> None:
    if source not in WORK_STATES or target not in WORK_STATES or source in TERMINAL_STATES:
        raise DomainError("invalid_state", "invalid work transition")
    allowed = False
    if (source, target) == ("intake", "refining") or (source, target) == ("refining", "ready"):
        allowed = actor in {"human", "elder"}
    elif (source, target) == ("ready", "in_progress"):
        allowed = actor == "system:dispatcher"
    elif (source, target) == ("in_progress", "verifying"):
        allowed = actor == assigned_actor
    elif (source, target) == ("verifying", "in_progress"):
        allowed = actor in {"system:checks", "system:reviews", "human"}
    elif (source, target) == ("verifying", "awaiting_approval"):
        allowed = actor == "system:reconciler"
    elif (source, target) == ("awaiting_approval", "in_progress") or (source, target) == (
        "awaiting_approval",
        "accepted",
    ):
        allowed = actor == "human"
    elif (source, target) == ("accepted", "delivered"):
        allowed = actor == "system:reconciler"
    elif target == "blocked":
        allowed = actor in {"human", "elder", "system:reconciler", assigned_actor}
    elif source == "blocked":
        allowed = actor in {"human", "elder"}
    elif target == "cancelled":
        allowed = actor == "human" or (actor == "elder" and source in {"intake", "refining", "ready"})
    if not allowed:
        raise DomainError("unauthorized", f"{actor} cannot transition {source} to {target}")


def authorize_scope_mutation(actor: str, state: str) -> None:
    if actor not in {"human", "elder"}:
        raise DomainError("unauthorized", "only the human or elder may refine scope")
    if state != "refining":
        raise DomainError("scope_frozen", "scope may be changed only while refining")


def authorize_reopen(actor: str, state: str, has_execution: bool) -> None:
    if state == "ready" and not has_execution and actor in {"human", "elder"}:
        return
    if state in {"in_progress", "verifying", "awaiting_approval", "accepted", "blocked"} and actor == "human":
        return
    raise DomainError("unauthorized", f"{actor} cannot reopen work from {state}")
