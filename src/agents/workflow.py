from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .db import canonical_json, mutation
from .store import Store


def _identity(actor: str) -> str:
    return "human" if actor == "human" else f"agent:{actor}"


def _hash(body: object) -> str:
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


class Workflow:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_work(
        self, request_id: str, actor: str, *, parent_id: str | None, kind: str, title: str, problem: str, outcome: str
    ) -> dict[str, Any]:
        body = {"parent_id": parent_id, "kind": kind, "title": title, "problem": problem, "outcome": outcome}
        return mutation(
            self.connection,
            _identity(actor),
            request_id,
            "work.created",
            "work:new",
            _hash(body),
            lambda connection: Store(connection).create_work(actor=actor, **body),
        )

    def start_refinement(self, request_id: str, actor: str, item_id: str, expected_version: int) -> dict[str, Any]:
        body = {"item_id": item_id, "expected_version": expected_version}
        return mutation(
            self.connection,
            _identity(actor),
            request_id,
            "work.refinement_started",
            f"work:{item_id}",
            _hash(body),
            lambda connection: Store(connection).transition(
                actor=actor, item_id=item_id, expected_version=expected_version, target="refining"
            ),
        )

    def refine(self, request_id: str, actor: str, item_id: str, expected_version: int, **scope: Any) -> dict[str, Any]:
        body = {"item_id": item_id, "expected_version": expected_version, **scope}
        return mutation(
            self.connection,
            _identity(actor),
            request_id,
            "work.refined",
            f"work:{item_id}",
            _hash(body),
            lambda connection: Store(connection).refine(
                actor=actor, item_id=item_id, expected_version=expected_version, **scope
            ),
        )

    def mark_ready(self, request_id: str, actor: str, item_id: str, expected_version: int) -> dict[str, Any]:
        body = {"item_id": item_id, "expected_version": expected_version}
        return mutation(
            self.connection,
            _identity(actor),
            request_id,
            "work.ready",
            f"work:{item_id}",
            _hash(body),
            lambda connection: Store(connection).transition(
                actor=actor, item_id=item_id, expected_version=expected_version, target="ready"
            ),
        )

    def reopen(self, request_id: str, actor: str, item_id: str, expected_version: int, reason: str) -> dict[str, Any]:
        body = {"item_id": item_id, "expected_version": expected_version, "reason": reason}
        return mutation(
            self.connection,
            _identity(actor),
            request_id,
            "work.reopened",
            f"work:{item_id}",
            _hash(body),
            lambda connection: Store(connection).reopen(
                actor=actor, item_id=item_id, expected_version=expected_version, reason=reason
            ),
        )

    def cancel(self, request_id: str, actor: str, item_id: str, expected_version: int, reason: str) -> dict[str, Any]:
        body = {"item_id": item_id, "expected_version": expected_version, "reason": reason}
        return mutation(
            self.connection,
            _identity(actor),
            request_id,
            "work.cancelled",
            f"work:{item_id}",
            _hash(body),
            lambda connection: Store(connection).cancel(
                actor=actor, item_id=item_id, expected_version=expected_version, reason=reason
            ),
        )


async def run_verification(argv: Sequence[str], cwd: Path) -> int:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "CI": "1"}
    )
    return await process.wait()
