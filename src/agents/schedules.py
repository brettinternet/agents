from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from .config import AgentsConfig, ScheduleConfig
from .db import canonical_json
from .messages import Messaging
from .workflow import Workflow


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _config_hash(schedule: ScheduleConfig) -> str:
    body = {
        "cron": schedule.cron,
        "every_seconds": schedule.every_seconds,
        "message": schedule.message,
        "overlap": schedule.overlap,
        "timezone": schedule.timezone,
        "to": schedule.to,
        "work": (
            {
                "kind": schedule.work.kind,
                "title": schedule.work.title,
                "problem": schedule.work.problem,
                "outcome": schedule.work.outcome,
            }
            if schedule.work
            else None
        ),
    }
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def _initial_run(schedule: ScheduleConfig, now: datetime) -> datetime:
    if schedule.cron:
        local = now.astimezone(ZoneInfo(schedule.timezone))
        return (
            croniter(schedule.cron, local - timedelta(minutes=1), ret_type=datetime).get_next(datetime).astimezone(UTC)
        )
    return now + timedelta(seconds=schedule.every_seconds)


def _next_run(schedule: ScheduleConfig, scheduled_for: datetime, now: datetime) -> datetime:
    if schedule.cron:
        local = now.astimezone(ZoneInfo(schedule.timezone))
        return croniter(schedule.cron, local, ret_type=datetime).get_next(datetime).astimezone(UTC)
    candidate = scheduled_for + timedelta(seconds=schedule.every_seconds)
    return candidate if candidate > now else now + timedelta(seconds=schedule.every_seconds)


class Scheduler:
    def __init__(self, config: AgentsConfig, connection: sqlite3.Connection) -> None:
        self.config = config
        self.connection = connection

    def dispatch_due(self, now: datetime | None = None) -> list[dict[str, object]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        results: list[dict[str, object]] = []
        for schedule in self.config.schedules:
            state = self._state(schedule, current)
            scheduled_for = _parse_timestamp(str(state["next_run_at"]))
            if scheduled_for > current:
                continue
            if self._has_active_occurrence(schedule):
                self._record(schedule, scheduled_for, current, "skipped", None, None)
                results.append({"slug": schedule.slug, "scheduled_for": _timestamp(scheduled_for), "state": "skipped"})
                continue
            request_id = f"schedule:{schedule.slug}:{_timestamp(scheduled_for)}"
            if schedule.work is not None:
                created = Workflow(self.connection).create_work(
                    request_id,
                    "system",
                    parent_id=None,
                    kind=schedule.work.kind,
                    title=schedule.work.title,
                    problem=schedule.work.problem,
                    outcome=schedule.work.outcome,
                )
                work_id = str(created["id"])
                self._record(schedule, scheduled_for, current, "created", None, work_id)
                results.append(
                    {
                        "slug": schedule.slug,
                        "scheduled_for": _timestamp(scheduled_for),
                        "state": "created",
                        "work_id": work_id,
                    }
                )
            else:
                posted = Messaging(self.connection).post(
                    request_id,
                    "system",
                    schedule.to,
                    schedule.message,
                )
                message_id = int(posted["id"])
                self._record(schedule, scheduled_for, current, "posted", message_id, None)
                results.append(
                    {
                        "slug": schedule.slug,
                        "scheduled_for": _timestamp(scheduled_for),
                        "state": "posted",
                        "message_id": message_id,
                    }
                )
        return results

    def _state(self, schedule: ScheduleConfig, now: datetime) -> sqlite3.Row:
        digest = _config_hash(schedule)
        state = self.connection.execute("SELECT * FROM schedule_states WHERE slug=?", (schedule.slug,)).fetchone()
        if state is not None and state["config_hash"] == digest:
            return state
        next_run = _timestamp(_initial_run(schedule, now))
        updated_at = _timestamp(now)
        with self.connection:
            self.connection.execute(
                "INSERT INTO schedule_states(slug,config_hash,next_run_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET config_hash=excluded.config_hash,"
                "next_run_at=excluded.next_run_at,updated_at=excluded.updated_at",
                (schedule.slug, digest, next_run, updated_at),
            )
        refreshed = self.connection.execute("SELECT * FROM schedule_states WHERE slug=?", (schedule.slug,)).fetchone()
        assert refreshed is not None
        return refreshed

    def _has_active_occurrence(self, schedule: ScheduleConfig) -> bool:
        if schedule.work is not None:
            return (
                self.connection.execute(
                    "SELECT 1 FROM schedule_runs sr JOIN work_items w ON w.id=sr.work_id "
                    "WHERE sr.schedule_slug=? AND sr.state='created' "
                    "AND w.status NOT IN ('delivered','cancelled') LIMIT 1",
                    (schedule.slug,),
                ).fetchone()
                is not None
            )
        return (
            self.connection.execute(
                "SELECT 1 FROM schedule_runs sr JOIN deliveries d ON d.message_id=sr.message_id "
                "WHERE sr.schedule_slug=? AND sr.state='posted' AND d.state='pending' LIMIT 1",
                (schedule.slug,),
            ).fetchone()
            is not None
        )

    def _record(
        self,
        schedule: ScheduleConfig,
        scheduled_for: datetime,
        now: datetime,
        state: str,
        message_id: int | None,
        work_id: str | None,
    ) -> None:
        next_run = _next_run(schedule, scheduled_for, now)
        timestamp = _timestamp(now)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO schedule_runs("
                "schedule_slug,scheduled_for,state,message_id,work_id,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (schedule.slug, _timestamp(scheduled_for), state, message_id, work_id, timestamp),
            )
            self.connection.execute(
                "UPDATE schedule_states SET next_run_at=?,updated_at=? WHERE slug=?",
                (_timestamp(next_run), timestamp, schedule.slug),
            )
