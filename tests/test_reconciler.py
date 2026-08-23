from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import connect, migrate, utc_now
from agents.delivery import Delivery
from agents.execution import (
    BackendHealth,
    ExecutionBusy,
    ExecutionConflict,
    ExecutionNotFound,
    ExecutionStatus,
    ExecutionTimeout,
    RunHandle,
    RunSnapshot,
    RunSpec,
)
from agents.reconciler import Reconciler, bootstrap_persistent_agents, reserve_terminal
from agents.store import Store


class FakeBackend:
    def __init__(self):
        self.created: list[dict] = []
        self.terminals: dict[str, list[dict]] = {}
        self.outputs: dict[str, str] = {}
        self.status = "idle"
        self.wakes = 0
        self.wake_targets: list[str] = []
        self.deleted: list[str] = []

    def health(self):
        return BackendHealth(True, "0.8.2", 20, True)

    def _snapshot(self, name: str, terminal: dict, *, output: str = "") -> RunSnapshot:
        pane_id = str(terminal.get("id") or terminal.get("terminal_id"))
        cwd = Path(str(terminal.get("working_directory") or self.created[0]["working_directory"])).resolve()
        status = ExecutionStatus(self.status) if self.status in set(ExecutionStatus) else ExecutionStatus.UNKNOWN
        return RunSnapshot(
            RunHandle(name, str(terminal.get("workspace_id") or f"workspace-{pane_id}"), pane_id),
            status,
            name,
            cwd,
            terminal.get("agent_name"),
            terminal.get("agent_kind"),
            int(terminal.get("revision", 1)),
            output,
        )

    def create_run(self, spec: RunSpec):
        model = ""
        if "--model" in spec.argv:
            model = spec.argv[spec.argv.index("--model") + 1]
        self.created.append({"working_directory": str(spec.cwd), "model": model, "spec": spec})
        self.terminals[spec.name] = [
            {
                "id": "terminal-1",
                "workspace_id": "workspace-1",
                "working_directory": str(spec.cwd),
                "agent_name": None if spec.mock else spec.agent_name,
                "agent_kind": None if spec.mock else spec.agent_kind,
                "revision": 1,
            }
        ]
        return self._snapshot(spec.name, self.terminals[spec.name][0])

    def find_run(self, name: str):
        terminals = self.terminals.get(name, [])
        if not terminals:
            return None
        if len(terminals) > 1:
            raise ExecutionConflict("duplicate_label", name)
        return self._snapshot(name, terminals[0])

    def get_run(self, handle: RunHandle, *, include_output: bool = False):
        terminals = self.terminals.get(handle.name, [])
        if not terminals:
            raise ExecutionNotFound("not_found", handle.name)
        terminal = terminals[0]
        return self._snapshot(
            handle.name,
            terminal,
            output=self.outputs.get(handle.terminal_id, "") if include_output else "",
        )

    def list_runs(self, prefix: str):
        return tuple(
            self._snapshot(name, terminal)
            for name, terminals in self.terminals.items()
            if name.startswith(prefix)
            for terminal in terminals
        )

    def send_message(self, handle: RunHandle, sender_id: str, message: str):
        self.wakes += 1
        self.wake_targets.append(handle.terminal_id)
        return str(self.wakes)

    def send_input(self, handle: RunHandle, message: str):
        return None

    def delete_run(self, handle: RunHandle):
        self.deleted.append(handle.terminal_id)
        terminals = self.terminals.get(handle.name, [])
        remaining = [
            terminal
            for terminal in terminals
            if str(terminal.get("id") or terminal.get("terminal_id")) != handle.terminal_id
        ]
        if remaining:
            self.terminals[handle.name] = remaining
        else:
            self.terminals.pop(handle.name, None)

    async def events(self):
        if False:
            yield

    def close(self):
        return None


class ReconcilerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        shutil.copytree(Path(__file__).parents[1] / "agents", self.root / "agents")
        state = self.root / ".agents"
        state.mkdir(mode=0o700)
        (state / "agent-auth-key").write_text("6b" * 32)
        (state / "agent-auth-key").chmod(0o600)
        self.connection = connect(state / "agents.db")
        migrate(self.connection)
        actors = (
            {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
            {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
            {
                "slug": "elder",
                "kind": "agent",
                "reports_to": "human",
                "profile_template": "elder",
                "specialty": "",
                "persistent": True,
                "capacity": 1,
            },
            {
                "slug": "explorer",
                "kind": "agent",
                "reports_to": "elder",
                "profile_template": "explorer",
                "specialty": "research",
                "persistent": True,
                "capacity": 3,
            },
            {
                "slug": "yapper",
                "kind": "agent",
                "reports_to": "elder",
                "profile_template": "yapper",
                "specialty": "publishing",
                "persistent": True,
                "capacity": 1,
            },
        )
        self.config = AgentsConfig(
            self.root / "agents.toml",
            self.root,
            ProjectConfig("test", repo, "main", (("task", "check"),)),
            RuntimeConfig(
                poll_seconds=5,
                stall_seconds=1800,
                launch_budget_per_hour=2,
                max_agents=4,
                max_consultations=3,
                worker_grace_seconds=86400,
            ),
            ExecutionConfig("herdr", "0.8.2", None, "mock", "mock_cli", (ModelChoice(""),)),
            WebConfig("127.0.0.1", 9890),
            actors,
        )
        Store(self.connection).initialize(self.config)
        self.fake = FakeBackend()
        self.reconciler = Reconciler(self.config, self.connection, self.fake)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def reserve(self):
        return reserve_terminal(
            self.connection,
            self.config,
            actor="elder",
            purpose_kind="persistent",
            purpose_id="elder",
            working_directory=self.config.project.path,
            budget_exempt=True,
        )

    def git_base_sha(self) -> str:
        repo = self.config.project.path
        (repo / "file").write_text("base")
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _make_live_fake_terminal(self, run, status: str):
        terminal_id = f"terminal-{run['id']}"
        working_directory = Path(str(run["working_directory"]))
        self.fake.created.append({"working_directory": str(working_directory)})
        self.fake.status = status
        self.fake.terminals[run["execution_name"]] = [
            {
                "id": terminal_id,
                "workspace_id": f"workspace-{run['id']}",
                "working_directory": str(working_directory),
                "revision": 1,
            }
        ]
        self.fake.outputs[terminal_id] = ""
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,agent_auth_id=COALESCE(agent_auth_id,profile_name),updated_at=? WHERE id=?",
            (f"workspace-{run['id']}", terminal_id, utc_now(), run["id"]),
        )
        return terminal_id

    def _make_work_terminal(self, status: str):
        created = Store(self.connection).create_work(
            actor="human",
            parent_id=None,
            kind="task",
            title=f"Work {status}",
            problem="P",
            outcome="O",
        )
        item_id = str(created["id"])
        now = utc_now()
        self.connection.execute(
            "UPDATE work_items SET status='in_progress',specialty='research',version=version+1,updated_at=? WHERE id=?",
            (now, item_id),
        )
        execution = self.connection.execute(
            "INSERT INTO executions(work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at)"
            "VALUES(?,1,'base',?,?, 'active',?,?)",
            (item_id, f"agents/{item_id.lower()}/1", str(self.root / f".worktrees/{item_id}-1"), now, now),
        ).lastrowid
        assert execution is not None
        self.connection.execute("UPDATE work_items SET active_execution_id=? WHERE id=?", (execution, item_id))
        run = reserve_terminal(
            self.connection,
            self.config,
            actor="explorer",
            purpose_kind="work",
            purpose_id=item_id,
            working_directory=self.root / f".worktrees/{item_id}-1",
            budget_exempt=True,
        )
        self.connection.execute(
            "INSERT INTO assignments(work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at)"
            "VALUES(?,?,? ,?,'open',?,?)",
            (item_id, execution, "explorer", run["id"], now, now),
        )
        self._make_live_fake_terminal(run, status)
        return item_id, run

    def _make_review_terminal(self, status: str):
        created = Store(self.connection).create_work(
            actor="human",
            parent_id=None,
            kind="task",
            title=f"Review {status}",
            problem="P",
            outcome="O",
        )
        item_id = str(created["id"])
        now = utc_now()
        self.connection.execute(
            "UPDATE work_items SET status='in_progress',specialty='research',version=version+1,updated_at=? WHERE id=?",
            (now, item_id),
        )
        execution = self.connection.execute(
            "INSERT INTO executions(work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at)"
            "VALUES(?,1,'base',?,?, 'active',?,?)",
            (item_id, f"agents/{item_id.lower()}/1", str(self.root / f".worktrees/{item_id}-1"), now, now),
        ).lastrowid
        assert execution is not None
        self.connection.execute("UPDATE work_items SET active_execution_id=? WHERE id=?", (execution, item_id))
        submission = self.connection.execute(
            "INSERT INTO submissions(execution_id,revision,commit_sha,summary,state,created_at,updated_at)"
            "VALUES(?,1,'base','submitted','reviewing',?,?)",
            (execution, now, now),
        ).lastrowid
        assert submission is not None
        self.connection.execute("INSERT INTO review_requirements(work_id,gate) VALUES(?,'research')", (item_id,))
        run = reserve_terminal(
            self.connection,
            self.config,
            actor="explorer",
            purpose_kind="review",
            purpose_id=f"{submission}-research",
            working_directory=self.root / f".worktrees/review/{submission}-research",
            budget_exempt=True,
        )
        self.connection.execute(
            "INSERT INTO reviews(submission_id,gate,actor_slug,terminal_run_id,worktree_path,verdict,created_at,updated_at)"
            "VALUES(?,'research','explorer',?,?,'pending',?,?)",
            (
                submission,
                run["id"],
                str(self.root / f".worktrees/review/{submission}-research"),
                now,
                now,
            ),
        )
        self._make_live_fake_terminal(run, status)
        return item_id, int(submission), run

    def test_bootstrap_reserves_all_persistent_agents_once(self):
        first = bootstrap_persistent_agents(self.connection, self.config)
        self.assertEqual(len(first), 3)
        runs = self.connection.execute(
            "SELECT actor_slug,purpose_kind,purpose_id FROM terminal_runs ORDER BY actor_slug"
        ).fetchall()
        self.assertEqual(
            {(row["actor_slug"], row["purpose_kind"], row["purpose_id"]) for row in runs},
            {
                ("elder", "persistent", "elder"),
                ("explorer", "persistent", "explorer"),
                ("yapper", "persistent", "yapper"),
            },
        )
        for actor in ("elder", "explorer", "yapper"):
            self.assertEqual(
                self.connection.execute(
                    "SELECT COUNT(*) FROM actor_leases WHERE actor_slug=? AND released_at IS NULL", (actor,)
                ).fetchone()[0],
                1,
            )
        self.assertEqual(bootstrap_persistent_agents(self.connection, self.config), [])

    def test_bootstrap_retries_after_stale_project_directory_failures(self):
        for _ in range(3):
            run = self.reserve()
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_runs SET state='failed',error=?,token_revoked_at=?,updated_at=? WHERE id=?",
                (
                    "CAO terminal working directory mismatch: expected /new, got /old",
                    now,
                    now,
                    run["id"],
                ),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='failed',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
        reserved = bootstrap_persistent_agents(self.connection, self.config)
        replacement = self.connection.execute(
            "SELECT generation,state FROM terminal_runs WHERE id IN ({}) AND actor_slug='elder'".format(
                ",".join("?" for _ in reserved)
            ),
            tuple(reserved),
        ).fetchone()
        self.assertEqual(tuple(replacement), (4, "reserved"))

    def test_bootstrap_retries_after_transient_herdr_cutover_failures(self):
        errors = (
            "transient Herdr cutover: agent_pane_busy: agent target pane w1:p1 is not an available shell",
            "transient Herdr cutover: backend run identity, occupant, or cwd mismatch",
        )
        for index in range(6):
            run = self.reserve()
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_runs SET state='failed',error=?,token_revoked_at=?,updated_at=? WHERE id=?",
                (errors[index % len(errors)], now, now, run["id"]),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='failed',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
        reserved = bootstrap_persistent_agents(self.connection, self.config)
        replacement = self.connection.execute(
            "SELECT generation,state FROM terminal_runs WHERE id IN ({}) AND actor_slug='elder'".format(
                ",".join("?" for _ in reserved)
            ),
            tuple(reserved),
        ).fetchone()
        self.assertEqual(tuple(replacement), (7, "reserved"))

    def test_bootstrap_bounds_repeated_stale_directory_failures(self):
        for _ in range(12):
            run = self.reserve()
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_runs SET state='failed',error=?,token_revoked_at=?,updated_at=? WHERE id=?",
                (
                    "CAO terminal working directory mismatch: expected /new, got /old",
                    now,
                    now,
                    run["id"],
                ),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='failed',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            self.connection.execute(
                "UPDATE actor_leases SET released_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
        reserved = bootstrap_persistent_agents(self.connection, self.config)
        self.assertFalse(
            self.connection.execute(
                "SELECT 1 FROM terminal_runs WHERE id IN ({}) AND actor_slug='elder'".format(
                    ",".join("?" for _ in reserved)
                ),
                tuple(reserved),
            ).fetchone()
        )

    def test_discard_profile_removes_fragment_artifact(self):
        profile = self.config.state_dir / "profiles/test.md"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text("profile")
        profile.chmod(0o600)
        provider = self.root / ".aws/opencode/opencode.json"
        provider.parent.mkdir(parents=True)
        provider.write_text('{"mcp":{"owned":{"command":"agents"},"other":{"command":"keep"}}}')
        provider.chmod(0o600)
        expected = '{"command":"agents"}'
        artifacts = [
            {"path": str(profile), "sha256": hashlib.sha256(b"profile").hexdigest()},
            {
                "path": str(provider),
                "sha256": hashlib.sha256(expected.encode()).hexdigest(),
                "fragment_key": "mcp:owned",
                "expected_json_redacted": expected,
                "secret_fields_json": "{}",
            },
        ]
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            self.reconciler._discard_profile(SimpleNamespace(path=profile, name="test"), artifacts)
        self.assertFalse(profile.exists())
        self.assertEqual(json.loads(provider.read_text()), {"mcp": {"other": {"command": "keep"}}})

    async def test_reservation_fixed_identity_and_single_post(self):
        run = self.reserve()
        self.assertRegex(run["profile_name"], r"r\d{10}-g\d{4}$")
        self.assertRegex(run["mcp_name"], r"r\d{10}-g\d{4}$")
        self.assertTrue(run["execution_name"].startswith("agents-"))
        self.assertNotEqual(run["token_digest"], "")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}):
            await self.reconciler._launch(run["id"])
            await self.reconciler._launch(run["id"])
        self.assertEqual(len(self.fake.created), 1)
        saved = self.connection.execute(
            "SELECT state,backend_terminal_id,token_digest FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual((saved["state"], saved["backend_terminal_id"]), ("live", "terminal-1"))
        attempt = self.connection.execute(
            "SELECT state,counted FROM launch_attempts WHERE terminal_run_id=?", (run["id"],)
        ).fetchone()
        self.assertEqual((attempt["state"], attempt["counted"]), ("succeeded", 1))
        self.assertTrue(
            all(
                Path(row[0]).is_file()
                for row in self.connection.execute(
                    "SELECT path FROM terminal_artifacts WHERE terminal_run_id=?", (run["id"],)
                )
            )
        )

    async def test_transient_agent_pane_busy_preserves_launch_for_retry(self):
        run = self.reserve()
        with (
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}),
            patch.object(
                self.fake,
                "create_run",
                side_effect=ExecutionBusy(
                    "agent_pane_busy",
                    "agent target pane w1:p1 is not an available shell",
                ),
            ),
        ):
            await self.reconciler._launch(run["id"])
        saved = self.connection.execute(
            "SELECT state,error FROM terminal_runs WHERE id=?",
            (run["id"],),
        ).fetchone()
        attempt = self.connection.execute(
            "SELECT state,error FROM launch_attempts WHERE terminal_run_id=?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(saved["state"], "reserved")
        self.assertIsNone(saved["error"])
        self.assertEqual(attempt["state"], "reserved")
        self.assertIn("agent_pane_busy", attempt["error"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM blockers").fetchone()[0], 0)

    async def test_actor_model_choice_is_selected_once_and_persisted(self):
        selected = ModelChoice("mock/actor-second")
        actor_models = (ModelChoice("mock/actor-first"), selected)
        self.config = replace(
            self.config,
            execution=ExecutionConfig(
                "herdr",
                "0.8.2",
                None,
                "mock",
                "mock_cli",
                (ModelChoice("mock/global"),),
            ),
            actor_models=(("elder", actor_models),),
        )
        self.reconciler = Reconciler(self.config, self.connection, self.fake)
        with patch("agents.reconciler.secrets.choice", return_value=selected) as choose:
            run = self.reserve()
            self.assertEqual((run["model"], run["reasoning_effort"]), ("mock/actor-second", ""))
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}):
                await self.reconciler._launch(run["id"])
                await self.reconciler._launch(run["id"])
        choose.assert_called_once_with(actor_models)

    async def test_output_digest_prompt_and_wake_are_durable(self):
        run = self.reserve()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}):
            await self.reconciler._launch(run["id"])
        self.fake.status = "waiting_user_answer"
        self.fake.outputs["terminal-1"] = "Need human answer"
        await self.reconciler._poll(run["id"])
        saved = self.connection.execute(
            "SELECT output_digest,output_tail FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(len(saved["output_digest"]), 64)
        self.assertEqual(saved["output_tail"], "Need human answer")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM incidents WHERE kind='provider_prompt'").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM blockers WHERE terminal_run_id=? AND kind='waiting_user_answer'",
                (run["id"],),
            ).fetchone()[0],
            "open",
        )
        now = utc_now()
        self.connection.execute(
            "INSERT INTO terminal_inputs(terminal_run_id,actor_slug,body,state,created_at,updated_at)"
            "VALUES(?,'human','answer','sent',?,?)",
            (run["id"], now, now),
        )
        self.fake.status = "processing"
        await self.reconciler._poll(run["id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM blockers WHERE terminal_run_id=? AND kind='waiting_user_answer'",
                (run["id"],),
            ).fetchone()[0],
            "resolved",
        )
        findings = self.connection.execute("SELECT id FROM conversations WHERE address='#findings'").fetchone()[0]
        message = self.connection.execute(
            "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)VALUES(?,'human','wake','normal',?)",
            (findings, utc_now()),
        ).lastrowid
        delivery = self.connection.execute(
            "INSERT INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
            "VALUES(?,'elder',NULL,'pending',0,?)",
            (message, utc_now()),
        ).lastrowid
        self.assertIsNotNone(delivery)
        await self.reconciler._wake(cast(int, delivery))
        self.assertEqual(self.fake.wakes, 1)
        self.assertEqual(
            self.connection.execute("SELECT result FROM wake_attempts WHERE delivery_id=?", (delivery,)).fetchone()[0],
            "accepted",
        )

    async def test_uncertain_wake_fences_run_before_delivery_can_retry(self):
        run = self.reserve()
        self._make_live_fake_terminal(run, "idle")
        findings = self.connection.execute("SELECT id FROM conversations WHERE address='#findings'").fetchone()[0]
        message = self.connection.execute(
            "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
            "VALUES(?,'human','wake','normal',?)",
            (findings, utc_now()),
        ).lastrowid
        delivery = self.connection.execute(
            "INSERT INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
            "VALUES(?,'elder',NULL,'pending',0,?)",
            (message, utc_now()),
        ).lastrowid
        with patch.object(
            self.fake,
            "send_message",
            side_effect=ExecutionTimeout("disconnected", "unknown", outcome_unknown=True),
        ):
            await self.reconciler._wake(cast(int, delivery))
        saved = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(saved["state"], "ending")
        self.assertIsNotNone(saved["token_revoked_at"])
        self.assertEqual(
            self.connection.execute("SELECT result FROM wake_attempts WHERE delivery_id=?", (delivery,)).fetchone()[0],
            "uncertain",
        )

    async def test_terminal_status_transition_emits_one_event(self):
        item_id, run = self._make_work_terminal("idle")
        await self.reconciler._poll(run["id"])
        events = self.connection.execute(
            "SELECT actor_slug,kind,entity_kind,entity_id,metadata_json FROM events "
            "WHERE kind='terminal.status_changed' AND entity_id=? ORDER BY id",
            (f"terminal:{run['id']}",),
        ).fetchall()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["actor_slug"], "explorer")
        self.assertEqual(event["entity_kind"], "terminal")
        self.assertEqual(
            json.loads(event["metadata_json"]),
            {
                "previous_status": "",
                "status": "idle",
                "state": "live",
                "purpose_kind": "work",
                "purpose_id": item_id,
            },
        )
        await self.reconciler._poll(run["id"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM events WHERE kind='terminal.status_changed'").fetchone()[0],
            1,
        )
        self.fake.status = "processing"
        await self.reconciler._poll(run["id"])
        rows = self.connection.execute(
            "SELECT metadata_json FROM events WHERE kind='terminal.status_changed' ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[1]["metadata_json"])["previous_status"], "idle")
        self.assertEqual(json.loads(rows[1]["metadata_json"])["status"], "processing")

    async def test_terminal_status_event_shares_transaction_with_update(self):
        _, run = self._make_work_terminal("idle")
        with (
            self.connection,
            patch("agents.reconciler.canonical_json", side_effect=RuntimeError("metadata failed")),
            self.assertRaises(RuntimeError),
        ):
            await self.reconciler._poll(run["id"])
        saved = self.connection.execute("SELECT status FROM terminal_runs WHERE id=?", (run["id"],)).fetchone()
        self.assertIsNone(saved["status"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM events WHERE kind='terminal.status_changed'").fetchone()[0],
            0,
        )

    async def test_assignment_delivery_waits_for_work_terminal_and_routes_participant_to_persistent(self):
        work = Store(self.connection).create_work(
            actor="human",
            parent_id=None,
            kind="task",
            title="Assigned work",
            problem="P",
            outcome="O",
        )
        work_id = str(work["id"])
        persistent = reserve_terminal(
            self.connection,
            self.config,
            actor="yapper",
            purpose_kind="persistent",
            purpose_id="yapper",
            working_directory=self.config.project.path,
            budget_exempt=True,
        )
        work_run = reserve_terminal(
            self.connection,
            self.config,
            actor="yapper",
            purpose_kind="work",
            purpose_id=work_id,
            working_directory=self.config.project.path,
            budget_exempt=True,
        )
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            (f"workspace-{persistent['id']}", "yapper-persistent", now, persistent["id"]),
        )
        execution = self.connection.execute(
            "INSERT INTO executions(work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at)"
            "VALUES(?,1,'base','branch',?,'active',?,?)",
            (work_id, str(self.root / "worktree"), now, now),
        ).lastrowid
        self.assertIsNotNone(execution)
        self.connection.execute(
            "UPDATE work_items SET status='in_progress',active_execution_id=?,updated_at=? WHERE id=?",
            (execution, now, work_id),
        )
        self.connection.execute(
            "INSERT INTO assignments(work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at)"
            "VALUES(?,?,?,?,'open',?,?)",
            (work_id, execution, "yapper", work_run["id"], now, now),
        )
        conversation = self.connection.execute(
            "SELECT id FROM conversations WHERE address=?", (f"work:{work_id}",)
        ).fetchone()
        self.assertIsNotNone(conversation)
        assignment_message = self.connection.execute(
            "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
            "VALUES(?,'system','assignment','normal',?)",
            (conversation["id"], now),
        ).lastrowid
        assignment_delivery = self.connection.execute(
            "INSERT INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
            "VALUES(?,'yapper',?,'pending',0,?)",
            (assignment_message, work_run["id"], now),
        ).lastrowid
        self.assertIsNotNone(assignment_delivery)
        persisted = self.connection.execute(
            "SELECT actor_slug,terminal_run_id FROM deliveries WHERE id=?", (assignment_delivery,)
        ).fetchone()
        self.assertEqual((persisted["actor_slug"], persisted["terminal_run_id"]), ("yapper", work_run["id"]))

        await self.reconciler._wake(cast(int, assignment_delivery))
        delivery = self.connection.execute(
            "SELECT state,attempts FROM deliveries WHERE id=?", (assignment_delivery,)
        ).fetchone()
        self.assertEqual((delivery["state"], delivery["attempts"]), ("pending", 0))

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM wake_attempts").fetchone()[0], 0)
        self.assertEqual(self.fake.wakes, 0)
        self.assertEqual(self.fake.wake_targets, [])

        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            (f"workspace-{work_run['id']}", "yapper-work", utc_now(), work_run["id"]),
        )
        await self.reconciler._wake(cast(int, assignment_delivery))
        self.assertEqual(self.fake.wakes, 1)
        self.assertEqual(self.fake.wake_targets, ["yapper-work"])
        delivery = self.connection.execute(
            "SELECT state,attempts FROM deliveries WHERE id=?", (assignment_delivery,)
        ).fetchone()
        self.assertEqual((delivery["state"], delivery["attempts"]), ("pending", 1))
        wake = self.connection.execute(
            "SELECT terminal_run_id,result FROM wake_attempts WHERE delivery_id=?",
            (assignment_delivery,),
        ).fetchone()
        self.assertEqual((wake["terminal_run_id"], wake["result"]), (work_run["id"], "accepted"))

        participant = self.reserve()
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            (f"workspace-{participant['id']}", "elder-persistent", utc_now(), participant["id"]),
        )
        participant_message = self.connection.execute(
            "INSERT INTO messages(conversation_id,sender_slug,body,urgency,created_at)"
            "VALUES(?,'system','participant','normal',?)",
            (conversation["id"], utc_now()),
        ).lastrowid
        participant_delivery = self.connection.execute(
            "INSERT INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
            "VALUES(?,'elder',NULL,'pending',0,?)",
            (participant_message, utc_now()),
        ).lastrowid
        self.assertIsNotNone(participant_delivery)
        self.assertIsNone(
            self.connection.execute(
                "SELECT terminal_run_id FROM deliveries WHERE id=?", (participant_delivery,)
            ).fetchone()[0]
        )
        await self.reconciler._wake(cast(int, participant_delivery))
        self.assertEqual(self.fake.wake_targets, ["yapper-work", "elder-persistent"])
        participant_wake = self.connection.execute(
            "SELECT terminal_run_id,result FROM wake_attempts WHERE delivery_id=?",
            (participant_delivery,),
        ).fetchone()
        self.assertEqual(
            (participant_wake["terminal_run_id"], participant_wake["result"]), (participant["id"], "accepted")
        )

    async def test_pending_input_posts_once(self):
        run = self.reserve()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}):
            await self.reconciler._launch(run["id"])
        value = self.connection.execute(
            "INSERT INTO terminal_inputs(terminal_run_id,actor_slug,body,state,created_at,updated_at)VALUES(?,'human','answer','pending',?,?)",
            (run["id"], utc_now(), utc_now()),
        ).lastrowid
        self.assertIsNotNone(value)
        await self.reconciler._send_input(cast(int, value))
        self.assertEqual(
            self.connection.execute("SELECT state FROM terminal_inputs WHERE id=?", (value,)).fetchone()[0], "sent"
        )

    def test_reservation_rolls_back_all_rows_on_secret_failure(self):
        with (
            self.connection,
            patch("agents.reconciler.read_private_secret", side_effect=RuntimeError("secret failed")),
            self.assertRaisesRegex(RuntimeError, "secret failed"),
        ):
            reserve_terminal(
                self.connection,
                self.config,
                actor="elder",
                purpose_kind="persistent",
                purpose_id="elder",
                working_directory=self.config.project.path,
                budget_exempt=True,
            )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM terminal_runs").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM actor_leases").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM launch_attempts").fetchone()[0], 0)

    def test_reopen_fences_retained_work_run_before_new_reservation(self):
        from agents.reconciler import reserve_terminal as reserve

        created = Store(self.connection).create_work(
            actor="human",
            parent_id=None,
            kind="task",
            title="Retained",
            problem="P",
            outcome="O",
        )
        item_id = str(created["id"])
        self.connection.execute(
            "UPDATE work_items SET status='in_progress',updated_at=? WHERE id=?",
            (utc_now(), item_id),
        )
        old = reserve(
            self.connection,
            self.config,
            actor="explorer",
            purpose_kind="work",
            purpose_id=item_id,
            working_directory=self.config.project.path,
            budget_exempt=True,
        )
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='retained',token_revoked_at=?,updated_at=? WHERE id=?",
            (now, now, old["id"]),
        )
        self.connection.execute(
            "UPDATE launch_attempts SET state='succeeded',counted=1,updated_at=? WHERE terminal_run_id=?",
            (now, old["id"]),
        )
        reopened = Store(self.connection).reopen(
            actor="human", item_id=item_id, expected_version=1, reason="replace retained run"
        )
        self.assertEqual(reopened["status"], "refining")
        fenced = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (old["id"],)
        ).fetchone()
        self.assertEqual(fenced["state"], "ending")
        replacement = reserve(
            self.connection,
            self.config,
            actor="explorer",
            purpose_kind="work",
            purpose_id=item_id,
            working_directory=self.config.project.path,
            budget_exempt=True,
        )
        self.assertEqual(replacement["generation"], 2)

    async def test_revocation_during_profile_install_prevents_post(self):
        run = self.reserve()
        import agents.reconciler as reconciler_module

        real_install = reconciler_module.install_profile

        def install_and_revoke(*args, **kwargs):
            artifacts = real_install(*args, **kwargs)
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_runs SET state='ending',token_revoked_at=?,updated_at=? WHERE id=?",
                (now, now, run["id"]),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='aborted',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            return artifacts

        with (
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}),
            patch("agents.reconciler.install_profile", side_effect=install_and_revoke),
        ):
            await self.reconciler._launch(run["id"])
        self.assertEqual(len(self.fake.created), 0)
        saved = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(saved["state"], "ending")
        self.assertIsNotNone(saved["token_revoked_at"])

    async def test_revocation_after_post_deletes_session_without_live_or_wake(self):
        run = self.reserve()
        real_create = self.fake.create_run

        def create_and_revoke(spec):
            result = real_create(spec)
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_runs SET state='ending',token_revoked_at=?,updated_at=? WHERE id=?",
                (now, now, run["id"]),
            )
            return result

        with (
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}),
            patch.object(self.fake, "create_run", side_effect=create_and_revoke),
        ):
            await self.reconciler._launch(run["id"])
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(self.fake.terminals, {})
        saved = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(saved["state"], "ending")
        self.assertIsNotNone(saved["token_revoked_at"])

    async def test_cleanup_releases_actor_lease_before_ending_run(self):
        run = self.reserve()
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='ending',token_revoked_at=?,updated_at=? WHERE id=?",
            (now, now, run["id"]),
        )
        await self.reconciler._cleanup_terminal(int(run["id"]))
        self.assertEqual(
            self.connection.execute("SELECT state FROM terminal_runs WHERE id=?", (run["id"],)).fetchone()[0],
            "ended",
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM actor_leases WHERE terminal_run_id=? AND released_at IS NULL",
                (run["id"],),
            ).fetchone()
        )

    async def test_failed_recorded_name_is_unmapped_and_deleted(self):
        run = self.reserve()
        now = utc_now()
        self.fake.terminals[run["execution_name"]] = [
            {
                "id": "late-terminal",
                "workspace_id": "late-workspace",
                "working_directory": run["working_directory"],
            }
        ]
        self.connection.execute(
            "UPDATE terminal_runs SET state='failed',token_revoked_at=?,updated_at=? WHERE id=?",
            (now, now, run["id"]),
        )
        await self.reconciler._remove_unmapped_sessions()
        self.assertNotIn(run["execution_name"], self.fake.terminals)

    async def test_unmapped_agents_label_with_wrong_cwd_is_not_deleted(self):
        run = self.reserve()
        self.fake.terminals[run["execution_name"]] = [
            {
                "id": "foreign-terminal",
                "workspace_id": "foreign-workspace",
                "working_directory": str(self.root / "foreign"),
            }
        ]
        self.connection.execute(
            "UPDATE terminal_runs SET state='failed',token_revoked_at=?,updated_at=? WHERE id=?",
            (utc_now(), utc_now(), run["id"]),
        )
        await self.reconciler._remove_unmapped_sessions()
        self.assertIn(run["execution_name"], self.fake.terminals)
        self.assertEqual(self.fake.deleted, [])

    async def test_mapped_stale_cwd_run_is_deleted_and_retried_once(self):
        run = self.reserve()
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='creating',updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        self.connection.execute(
            "UPDATE launch_attempts SET state='uncertain',updated_at=? WHERE terminal_run_id=?",
            (now, run["id"]),
        )
        self.fake.terminals[run["execution_name"]] = [
            {
                "id": "stale-terminal",
                "workspace_id": "stale-workspace",
                "working_directory": str(self.root / "foreign"),
            }
        ]
        await self.reconciler._adopt(int(run["id"]))
        reset = self.connection.execute(
            "SELECT state,error FROM terminal_runs WHERE id=?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(reset["state"], "reserved")
        self.assertIn("stale backend workspace replaced", reset["error"])
        self.assertEqual(self.fake.deleted, ["stale-terminal"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM blockers").fetchone()[0], 0)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}):
            await self.reconciler._launch(int(run["id"]))
        live = self.connection.execute(
            "SELECT state,error FROM terminal_runs WHERE id=?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(tuple(live), ("live", None))

    async def test_mapped_stale_cwd_replacement_is_bounded(self):
        run = self.reserve()

        def install_stale(attempt: int) -> None:
            now = utc_now()
            self.connection.execute(
                "UPDATE terminal_runs SET state='creating',updated_at=? WHERE id=?",
                (now, run["id"]),
            )
            self.connection.execute(
                "UPDATE launch_attempts SET state='uncertain',updated_at=? WHERE terminal_run_id=?",
                (now, run["id"]),
            )
            self.fake.terminals[run["execution_name"]] = [
                {
                    "id": f"stale-terminal-{attempt}",
                    "workspace_id": f"stale-workspace-{attempt}",
                    "working_directory": str(self.root / "foreign"),
                }
            ]

        install_stale(0)
        await self.reconciler._adopt(int(run["id"]))
        install_stale(1)
        with patch.object(
            self.fake,
            "create_run",
            side_effect=ExecutionConflict("cwd_mismatch", "workspace has an unexpected cwd"),
        ):
            await self.reconciler._adopt(int(run["id"]))
        saved = self.connection.execute(
            "SELECT state FROM terminal_runs WHERE id=?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(saved["state"], "ending")
        self.assertEqual(self.fake.deleted, ["stale-terminal-0"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM blockers").fetchone()[0], 1)

    async def test_duplicate_uncertain_runs_clean_only_expected_cwd(self):
        run = self.reserve()
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='creating',updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        self.connection.execute(
            "UPDATE launch_attempts SET state='uncertain',updated_at=? WHERE terminal_run_id=?",
            (now, run["id"]),
        )
        self.fake.terminals[run["execution_name"]] = [
            {
                "id": "expected-one",
                "workspace_id": "expected-workspace-one",
                "working_directory": run["working_directory"],
            },
            {
                "id": "expected-two",
                "workspace_id": "expected-workspace-two",
                "working_directory": run["working_directory"],
            },
            {
                "id": "foreign",
                "workspace_id": "foreign-workspace",
                "working_directory": str(self.root / "foreign"),
            },
        ]
        await self.reconciler._adopt(int(run["id"]))
        self.assertEqual(self.fake.deleted, ["expected-one", "expected-two"])
        self.assertEqual(
            [terminal["id"] for terminal in self.fake.terminals[run["execution_name"]]],
            ["foreign"],
        )
        saved = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(saved["state"], "ending")
        self.assertIsNotNone(saved["token_revoked_at"])

    async def test_missing_work_terminal_blocks_and_restart_dispatches(self):
        base_sha = self.git_base_sha()
        created = Store(self.connection).create_work(
            actor="human",
            parent_id=None,
            kind="task",
            title="Missing work terminal",
            problem="P",
            outcome="O",
        )
        item_id = str(created["id"])
        now = utc_now()
        self.connection.execute(
            "UPDATE work_items SET status='in_progress',specialty='research',version=version+1,updated_at=? WHERE id=?",
            (now, item_id),
        )
        execution = self.connection.execute(
            "INSERT INTO executions(work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at)"
            "VALUES(?,1,?,'agents/missing-work/1',?,'active',?,?)",
            (item_id, base_sha, str(self.root / ".worktrees/work/missing-1"), now, now),
        ).lastrowid
        self.assertIsNotNone(execution)
        self.connection.execute("UPDATE work_items SET active_execution_id=? WHERE id=?", (execution, item_id))
        run = reserve_terminal(
            self.connection,
            self.config,
            actor="explorer",
            purpose_kind="work",
            purpose_id=item_id,
            working_directory=self.root / ".worktrees/work/missing-1",
            budget_exempt=True,
        )
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            ("missing-workspace", "missing-work-terminal", now, run["id"]),
        )
        self.connection.execute(
            "INSERT INTO assignments(work_id,execution_id,actor_slug,terminal_run_id,state,created_at,updated_at)"
            "VALUES(?,?,? ,?,'open',?,?)",
            (item_id, execution, "explorer", run["id"], now, now),
        )

        await self.reconciler._poll(run["id"])
        work = self.connection.execute(
            "SELECT status,blocked_from,active_execution_id FROM work_items WHERE id=?", (item_id,)
        ).fetchone()
        self.assertEqual(
            (work["status"], work["blocked_from"], work["active_execution_id"]), ("blocked", "in_progress", execution)
        )
        blocker = self.connection.execute(
            "SELECT id,resume_state,state FROM blockers WHERE target_kind='work' AND target_id=?",
            (item_id,),
        ).fetchone()
        self.assertIsNotNone(blocker)
        self.assertEqual((blocker["resume_state"], blocker["state"]), ("in_progress", "open"))
        version = int(
            self.connection.execute("SELECT version FROM work_items WHERE id=?", (item_id,)).fetchone()["version"]
        )

        await self.reconciler._poll(run["id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM blockers WHERE target_kind='work' AND target_id=?", (item_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT version FROM work_items WHERE id=?", (item_id,)).fetchone()["version"],
            version,
        )

        restarted = Delivery(self.config, self.connection, backend=self.fake).resolve_blocker(
            "human", int(blocker["id"]), "replace missing terminal", "restart"
        )
        self.assertEqual(restarted["state"], "resolved")
        replacement = self.connection.execute(
            "SELECT id,generation,state FROM terminal_runs WHERE purpose_kind='work' AND purpose_id=? AND id<>? "
            "ORDER BY id DESC LIMIT 1",
            (item_id, run["id"]),
        ).fetchone()
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["generation"], 2)
        self.assertEqual(
            tuple(
                self.connection.execute("SELECT status,blocked_from FROM work_items WHERE id=?", (item_id,)).fetchone()
            ),
            ("in_progress", None),
        )

    async def test_missing_review_terminal_retires_pending_gate_and_requeues_replacement(self):
        base_sha = self.git_base_sha()
        created = Store(self.connection).create_work(
            actor="human",
            parent_id=None,
            kind="task",
            title="Missing review terminal",
            problem="P",
            outcome="O",
        )
        item_id = str(created["id"])
        now = utc_now()
        self.connection.execute(
            "UPDATE work_items SET status='in_progress',specialty='research',version=version+1,updated_at=? WHERE id=?",
            (now, item_id),
        )
        execution = self.connection.execute(
            "INSERT INTO executions(work_id,number,base_sha,branch,worktree_path,state,created_at,updated_at)"
            "VALUES(?,1,?,'agents/missing-review/1',?,'active',?,?)",
            (item_id, base_sha, str(self.root / ".worktrees/work/missing-review-1"), now, now),
        ).lastrowid
        self.assertIsNotNone(execution)
        self.connection.execute("UPDATE work_items SET active_execution_id=? WHERE id=?", (execution, item_id))
        submission = self.connection.execute(
            "INSERT INTO submissions(execution_id,revision,commit_sha,summary,state,created_at,updated_at)"
            "VALUES(?,1,?,'submitted','reviewing',?,?)",
            (execution, base_sha, now, now),
        ).lastrowid
        self.assertIsNotNone(submission)
        self.connection.execute("INSERT INTO review_requirements(work_id,gate) VALUES(?,'research')", (item_id,))
        reviewtree = self.root / ".worktrees/review" / f"{submission}-research"
        subprocess.run(
            ["git", "-C", str(self.config.project.path), "worktree", "add", "--detach", str(reviewtree), base_sha],
            check=True,
            capture_output=True,
        )
        run = reserve_terminal(
            self.connection,
            self.config,
            actor="explorer",
            purpose_kind="review",
            purpose_id=f"{submission}-research",
            working_directory=reviewtree,
            budget_exempt=True,
        )
        self.connection.execute(
            "UPDATE terminal_runs SET state='live',profile_state='installed',backend_run_id=?,"
            "backend_terminal_id=?,updated_at=? WHERE id=?",
            ("missing-review-workspace", "missing-review-terminal", now, run["id"]),
        )
        self.connection.execute(
            "INSERT INTO reviews(submission_id,gate,actor_slug,terminal_run_id,worktree_path,verdict,created_at,updated_at)"
            "VALUES(?,'research','explorer',?,?,'pending',?,?)",
            (submission, run["id"], str(reviewtree), now, now),
        )

        await self.reconciler._poll(run["id"])
        stale = self.connection.execute(
            "SELECT verdict,body FROM reviews WHERE submission_id=? AND gate='research' AND terminal_run_id=?",
            (submission, run["id"]),
        ).fetchone()
        self.assertEqual(stale["verdict"], "superseded")
        self.assertIn("superseded", stale["body"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM approvals WHERE submission_id=?", (submission,)).fetchone()[
                0
            ],
            0,
        )

        await self.reconciler._advance_delivery(provider_healthy=True)
        reviews = self.connection.execute(
            "SELECT verdict,terminal_run_id FROM reviews WHERE submission_id=? AND gate='research' ORDER BY id",
            (submission,),
        ).fetchall()
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["verdict"], "superseded")
        self.assertEqual(reviews[1]["verdict"], "pending")
        self.assertNotEqual(reviews[1]["terminal_run_id"], run["id"])
        self.assertEqual(
            self.connection.execute("SELECT status FROM work_items WHERE id=?", (item_id,)).fetchone()["status"],
            "in_progress",
        )

        await self.reconciler._advance_delivery(provider_healthy=True)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE submission_id=? AND gate='research'", (submission,)
            ).fetchone()[0],
            2,
        )

        current_version = int(
            self.connection.execute("SELECT version FROM work_items WHERE id=?", (item_id,)).fetchone()["version"]
        )
        assert submission is not None
        reviewed = Delivery(self.config, self.connection).submit_review(
            "explorer",
            item_id,
            submission,
            current_version,
            "research",
            "changes_requested",
            "needs changes",
            int(reviews[1]["terminal_run_id"]),
        )
        self.assertEqual(reviewed["state"], "changes_requested")
        await self.reconciler._advance_delivery(provider_healthy=True)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE submission_id=? AND gate='research'", (submission,)
            ).fetchone()[0],
            2,
        )

    async def test_error_work_terminal_blocks_and_revokes(self):
        item_id, run = self._make_work_terminal("error")
        await self.reconciler._poll(run["id"])
        work = self.connection.execute("SELECT status,blocked_from FROM work_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual((work["status"], work["blocked_from"]), ("blocked", "in_progress"))
        terminal = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(terminal["state"], "failed")
        self.assertIsNotNone(terminal["token_revoked_at"])
        blocker = self.connection.execute(
            "SELECT kind,state FROM blockers WHERE target_kind='work' AND target_id=?", (item_id,)
        ).fetchone()
        self.assertEqual((blocker["kind"], blocker["state"]), ("terminal_failure", "open"))

        version = self.connection.execute("SELECT version FROM work_items WHERE id=?", (item_id,)).fetchone()["version"]
        await self.reconciler._poll(run["id"])
        self.assertEqual(
            self.connection.execute("SELECT version FROM work_items WHERE id=?", (item_id,)).fetchone()["version"],
            version,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM blockers WHERE target_kind='work' AND target_id=?", (item_id,)
            ).fetchone()[0],
            1,
        )

    async def test_missing_outcome_work_terminal_blocks_and_revokes(self):
        item_id, run = self._make_work_terminal("completed")
        self.connection.execute(
            "UPDATE terminal_runs SET output_digest=?,digest_since=? WHERE id=?",
            (hashlib.sha256(b"").hexdigest(), "2000-01-01T00:00:00.000Z", run["id"]),
        )
        await self.reconciler._poll(run["id"])
        work = self.connection.execute("SELECT status,blocked_from FROM work_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual((work["status"], work["blocked_from"]), ("blocked", "in_progress"))
        blocker = self.connection.execute(
            "SELECT kind,state FROM blockers WHERE target_kind='work' AND target_id=?", (item_id,)
        ).fetchone()
        self.assertEqual((blocker["kind"], blocker["state"]), ("missing_outcome", "open"))

    async def test_error_review_terminal_supersedes_pending_assignment(self):
        item_id, submission_id, run = self._make_review_terminal("error")
        await self.reconciler._poll(run["id"])
        review = self.connection.execute(
            "SELECT verdict,body FROM reviews WHERE submission_id=? AND gate='research'", (submission_id,)
        ).fetchone()
        self.assertEqual(review["verdict"], "superseded")
        self.assertIn("superseded", review["body"])
        blocker = self.connection.execute(
            "SELECT kind,state FROM blockers WHERE target_kind='review' AND target_id=?",
            (run["purpose_id"],),
        ).fetchone()
        self.assertEqual((blocker["kind"], blocker["state"]), ("terminal_failure", "open"))
        self.assertEqual(
            self.connection.execute("SELECT status FROM work_items WHERE id=?", (item_id,)).fetchone()["status"],
            "in_progress",
        )

    async def test_missing_outcome_review_terminal_supersedes_pending_assignment(self):
        _, submission_id, run = self._make_review_terminal("completed")
        self.connection.execute(
            "UPDATE terminal_runs SET output_digest=?,digest_since=? WHERE id=?",
            (hashlib.sha256(b"").hexdigest(), "2000-01-01T00:00:00.000Z", run["id"]),
        )
        await self.reconciler._poll(run["id"])
        review = self.connection.execute(
            "SELECT verdict FROM reviews WHERE submission_id=? AND gate='research'", (submission_id,)
        ).fetchone()
        self.assertEqual(review["verdict"], "superseded")
        blocker = self.connection.execute(
            "SELECT kind,state FROM blockers WHERE target_kind='review' AND target_id=?",
            (run["purpose_id"],),
        ).fetchone()
        self.assertEqual((blocker["kind"], blocker["state"]), ("missing_outcome", "open"))

    def test_stale_provider_terminal_is_fenced_before_bootstrap(self):
        run = self.reserve()
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET provider='codex',state='live',profile_state='installed',"
            "backend_terminal_id='stale-provider-terminal',updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        self.reconciler._fence_stale_provider_runs()
        stale = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(stale["state"], "ending")
        self.assertIsNotNone(stale["token_revoked_at"])
        replacement = bootstrap_persistent_agents(self.connection, self.config)
        self.assertIn(
            self.connection.execute(
                "SELECT id FROM terminal_runs WHERE actor_slug='elder' AND purpose_kind='persistent' "
                "AND generation=2 AND state='reserved'"
            ).fetchone()["id"],
            replacement,
        )

    def test_stale_provider_creating_terminal_is_fenced_before_adoption(self):
        run = self.reserve()
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET provider='codex',state='creating',profile_state='installed',"
            "backend_terminal_id='stale-creating-provider-terminal',updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        self.connection.execute(
            "UPDATE launch_attempts SET state='posting',counted=1,updated_at=? WHERE terminal_run_id=?",
            (now, run["id"]),
        )
        self.reconciler._fence_stale_provider_runs()
        stale = self.connection.execute(
            "SELECT state,token_revoked_at FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(stale["state"], "ending")
        self.assertIsNotNone(stale["token_revoked_at"])
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM launch_attempts WHERE terminal_run_id=?", (run["id"],)
            ).fetchone()["state"],
            "failed",
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM actor_leases WHERE terminal_run_id=? AND released_at IS NULL", (run["id"],)
            ).fetchone()
        )
        replacement = bootstrap_persistent_agents(self.connection, self.config)
        self.assertIn(
            self.connection.execute(
                "SELECT id FROM terminal_runs WHERE actor_slug='elder' AND purpose_kind='persistent' "
                "AND generation=2 AND state='reserved'"
            ).fetchone()["id"],
            replacement,
        )

    def test_prepost_work_failure_uses_terminal_recovery(self):
        item_id, run = self._make_work_terminal("idle")
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='reserved',profile_state='staged',backend_terminal_id=NULL,updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        self.reconciler._abort_prepost(run["id"], "profile staging failed")
        work = self.connection.execute("SELECT status,blocked_from FROM work_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual((work["status"], work["blocked_from"]), ("blocked", "in_progress"))
        terminal = self.connection.execute(
            "SELECT state,profile_state,token_revoked_at FROM terminal_runs WHERE id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(terminal["state"], "failed")
        self.assertEqual(terminal["profile_state"], "failed")
        self.assertIsNotNone(terminal["token_revoked_at"])
        attempt = self.connection.execute(
            "SELECT state,counted FROM launch_attempts WHERE terminal_run_id=?", (run["id"],)
        ).fetchone()
        self.assertEqual((attempt["state"], attempt["counted"]), ("aborted", 0))
        blocker = self.connection.execute(
            "SELECT kind,state FROM blockers WHERE target_kind='work' AND target_id=?", (item_id,)
        ).fetchone()
        self.assertEqual((blocker["kind"], blocker["state"]), ("terminal_failure", "open"))

    def test_prepost_review_failure_supersedes_pending_assignment(self):
        item_id, submission_id, run = self._make_review_terminal("idle")
        now = utc_now()
        self.connection.execute(
            "UPDATE terminal_runs SET state='reserved',profile_state='staged',backend_terminal_id=NULL,updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        self.reconciler._abort_prepost(run["id"], "profile staging failed")
        review = self.connection.execute(
            "SELECT verdict,body FROM reviews WHERE submission_id=? AND gate='research'", (submission_id,)
        ).fetchone()
        self.assertEqual(review["verdict"], "superseded")
        self.assertIn("superseded", review["body"])
        blocker = self.connection.execute(
            "SELECT kind,state FROM blockers WHERE target_kind='review' AND target_id=?",
            (run["purpose_id"],),
        ).fetchone()
        self.assertEqual((blocker["kind"], blocker["state"]), ("terminal_failure", "open"))
        self.assertEqual(
            self.connection.execute("SELECT status FROM work_items WHERE id=?", (item_id,)).fetchone()["status"],
            "in_progress",
        )
