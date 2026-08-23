from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agents.config import (
    AgentsConfig,
    ExecutionConfig,
    ModelChoice,
    ProjectConfig,
    RuntimeConfig,
    ScheduleConfig,
    ScheduledWorkConfig,
    WebConfig,
)
from agents.db import connect, migrate
from agents.messages import Messaging
from agents.schedules import Scheduler
from agents.store import Store


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        self.connection = connect(self.root / "agents.db")
        migrate(self.connection)
        self.config = AgentsConfig(
            source=self.root / "agents.toml",
            root=self.root,
            project=ProjectConfig("test", repo, "main", (("task", "check"),)),
            runtime=RuntimeConfig(5, 1800, 12, 4, 3, 86400),
            execution=ExecutionConfig("herdr", "0.8.2", None, "mock", "mock_cli", (ModelChoice(""),)),
            web=WebConfig("127.0.0.1", 9890),
            actors=(
                {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
                {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
                {
                    "slug": "researcher",
                    "kind": "agent",
                    "persistent": True,
                    "specialty": "research",
                    "capacity": 1,
                },
            ),
        )
        Store(self.connection).initialize(self.config)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_cron_posts_once_at_due_time(self) -> None:
        schedule = ScheduleConfig(
            slug="daily-scout",
            cron="0 9 * * *",
            timezone="UTC",
            to="@researcher",
            message="Explore and commit a public-safe memory.",
        )
        scheduler = Scheduler(replace(self.config, schedules=(schedule,)), self.connection)
        due = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

        first = scheduler.dispatch_due(due)
        second = scheduler.dispatch_due(due)

        self.assertEqual(first[0]["state"], "posted")
        self.assertEqual(second, [])
        message = self.connection.execute(
            "SELECT sender_slug,body FROM messages WHERE id=?", (first[0]["message_id"],)
        ).fetchone()
        self.assertEqual(tuple(message), ("system", schedule.message))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM deliveries WHERE state='pending'").fetchone()[0], 1
        )

    def test_interval_skips_while_prior_occurrence_is_unacknowledged(self) -> None:
        schedule = ScheduleConfig(
            slug="hourly-scout",
            every_seconds=3600,
            timezone="UTC",
            to="@researcher",
            message="Check for meaningful changes.",
        )
        scheduler = Scheduler(replace(self.config, schedules=(schedule,)), self.connection)
        start = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
        self.assertEqual(scheduler.dispatch_due(start), [])
        posted = scheduler.dispatch_due(datetime(2026, 8, 22, 1, 0, tzinfo=UTC))
        skipped = scheduler.dispatch_due(datetime(2026, 8, 22, 2, 0, tzinfo=UTC))
        self.connection.execute(
            "UPDATE deliveries SET state='acknowledged' WHERE message_id=?", (posted[0]["message_id"],)
        )
        resumed = scheduler.dispatch_due(datetime(2026, 8, 22, 3, 0, tzinfo=UTC))

        self.assertEqual(
            [posted[0]["state"], skipped[0]["state"], resumed[0]["state"]], ["posted", "skipped", "posted"]
        )
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT state FROM schedule_runs ORDER BY scheduled_for")],
            ["posted", "skipped", "posted"],
        )

    def test_replays_post_idempotently_after_interrupted_checkpoint(self) -> None:
        schedule = ScheduleConfig(
            slug="daily-scout",
            cron="0 9 * * *",
            timezone="UTC",
            to="#findings",
            message="Post findings.",
        )
        config = replace(self.config, schedules=(schedule,))
        scheduler = Scheduler(config, self.connection)
        due = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
        scheduler._state(schedule, due)
        request_id = "schedule:daily-scout:2026-08-22T09:00:00.000Z"
        prior = Messaging(self.connection).post(request_id, "system", schedule.to, schedule.message)

        result = scheduler.dispatch_due(due)

        self.assertEqual(result[0]["message_id"], prior["id"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM messages WHERE body=?", (schedule.message,)).fetchone()[0], 1
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0], 1)

    def test_interval_creates_fresh_spikes_without_overlap(self) -> None:
        schedule = ScheduleConfig(
            slug="daily-memory",
            every_seconds=86400,
            timezone="UTC",
            work=ScheduledWorkConfig(
                kind="spike",
                title="Daily exploration",
                problem="Find useful public developments.",
                outcome="Commit a dated public-safe memory with sources and recommendations.",
            ),
        )
        scheduler = Scheduler(replace(self.config, schedules=(schedule,)), self.connection)
        start = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
        scheduler.dispatch_due(start)
        created = scheduler.dispatch_due(datetime(2026, 8, 23, 0, 0, tzinfo=UTC))
        skipped = scheduler.dispatch_due(datetime(2026, 8, 24, 0, 0, tzinfo=UTC))
        self.connection.execute("UPDATE work_items SET status='delivered' WHERE id=?", (created[0]["work_id"],))
        resumed = scheduler.dispatch_due(datetime(2026, 8, 25, 0, 0, tzinfo=UTC))

        self.assertEqual(
            [created[0]["state"], skipped[0]["state"], resumed[0]["state"]], ["created", "skipped", "created"]
        )
        work = self.connection.execute("SELECT kind,title,status FROM work_items ORDER BY seq").fetchall()
        self.assertEqual(
            [tuple(row) for row in work],
            [("spike", "Daily exploration", "delivered"), ("spike", "Daily exploration", "intake")],
        )
