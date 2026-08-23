from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.auth import AgentContext
from agents.config import AgentsConfig, ExecutionConfig, ModelChoice, ProjectConfig, RuntimeConfig, WebConfig
from agents.db import connect, migrate, utc_now
from agents.messages import Messaging
from agents.store import Store
from agents.web import create_app


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        repo = root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        state = root / ".agents"
        state.mkdir(mode=0o700)
        (state / "web-token").write_text("w" * 64)
        (state / "web-token").chmod(0o600)
        (state / "agent-auth-key").write_text("6b" * 32)
        (state / "agent-auth-key").chmod(0o600)
        self.connection = connect(state / "agents.db")
        migrate(self.connection)
        self.config = AgentsConfig(
            root / "agents.toml",
            root,
            ProjectConfig("test", repo, "main", (("task", "check"),)),
            RuntimeConfig(5, 1800, 12, 4, 3, 86400),
            ExecutionConfig("herdr", "0.8.2", None, "mock", "mock_cli", (ModelChoice(""),)),
            WebConfig("127.0.0.1", 9890),
            (
                {"slug": "human", "kind": "human", "persistent": True, "capacity": 1},
                {"slug": "system", "kind": "system", "persistent": True, "capacity": 1},
                {
                    "slug": "manager",
                    "kind": "agent",
                    "reports_to": "human",
                    "profile_template": "manager",
                    "persistent": True,
                    "capacity": 1,
                },
                {
                    "slug": "researcher",
                    "kind": "agent",
                    "reports_to": "manager",
                    "profile_template": "researcher",
                    "specialty": "research",
                    "persistent": True,
                    "capacity": 3,
                },
                {
                    "slug": "writer",
                    "kind": "agent",
                    "reports_to": "manager",
                    "profile_template": "writer",
                    "specialty": "publishing",
                    "persistent": True,
                    "capacity": 1,
                },
            ),
        )
        Store(self.connection).initialize(self.config)
        self.client = TestClient(create_app(self.config, self.connection), base_url="http://testserver")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def _terminal_run(self, purpose_kind: str, purpose_id: str, execution_name: str) -> int:
        now = utc_now()
        run_id = self.connection.execute(
            "INSERT INTO terminal_runs("
            "execution_name,profile_name,mcp_name,profile_sha256,provider,model,generation,"
            "actor_slug,purpose_kind,purpose_id,working_directory,token_digest,profile_state,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                execution_name,
                "profile",
                "mcp",
                "sha",
                "mock",
                "model",
                1,
                "researcher",
                purpose_kind,
                purpose_id,
                ".",
                "digest",
                "installed",
                "live",
                now,
                now,
            ),
        ).lastrowid
        assert run_id is not None
        return int(run_id)

    def test_public_surface_and_login_origin(self):
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 401)
        self.assertEqual(self.client.post("/auth/login", data={"token": "w" * 64}).status_code, 403)
        response = self.client.post(
            "/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 200)

    def test_mutation_requires_csrf(self):
        self.client.post("/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"})
        self.assertEqual(self.client.post("/auth/logout", headers={"Origin": "http://testserver"}).status_code, 403)
        csrf = self.client.cookies.get("agents_csrf")
        self.assertEqual(
            self.client.post(
                "/auth/logout",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "logout-1",
                },
            ).status_code,
            200,
        )

    def test_response_headers(self):
        response = self.client.get("/login")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "same-origin")

    def test_json_errors_and_idempotency_are_stable(self):
        self.client.post("/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"})
        csrf = self.client.cookies.get("agents_csrf")
        base = {"Origin": "http://testserver", "X-CSRF-Token": csrf}
        malformed = self.client.post(
            "/api/v1/intake",
            headers={**base, "Idempotency-Key": "malformed-json"},
            content="{",
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], "malformed_json")
        missing = self.client.post(
            "/api/v1/intake",
            headers=base,
            json={"kind": "story", "title": "T", "problem": "P", "outcome": "O"},
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "missing_idempotency_key")
        first = self.client.post(
            "/api/v1/intake",
            headers={**base, "Idempotency-Key": "reused-key"},
            json={"kind": "story", "title": "First", "problem": "P", "outcome": "O"},
        )
        self.assertEqual(first.status_code, 200)
        conflict = self.client.post(
            "/api/v1/intake",
            headers={**base, "Idempotency-Key": "reused-key"},
            json={"kind": "story", "title": "Different", "problem": "P", "outcome": "O"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")

    def test_human_intake_message_search_and_pagination(self):
        self.client.post("/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"})
        csrf = self.client.cookies.get("agents_csrf")
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "human-intake-1",
        }
        intake = self.client.post(
            "/api/v1/intake",
            headers=headers,
            json={"kind": "story", "title": "Need feature", "problem": "P", "outcome": "O"},
        )
        self.assertEqual(intake.status_code, 200)
        self.assertEqual(intake.json()["data"]["id"], "AGENT-0001")
        message_headers = dict(headers)
        message_headers["Idempotency-Key"] = "human-message-1"
        posted = self.client.post(
            "/api/v1/messages",
            headers=message_headers,
            json={"to": "#findings", "body": "Need feature discussion"},
        )
        self.assertEqual(posted.status_code, 200)
        search = self.client.get("/api/v1/search", params={"query": "feature"})
        self.assertTrue(any(row["body"] == "Need feature discussion" for row in search.json()["data"]))
        snapshot = self.client.get("/api/v1/snapshot").json()["data"]
        self.assertEqual(snapshot["board"][0]["id"], "AGENT-0001")
        conversation_id = next(row["id"] for row in snapshot["conversations"] if row["address"] == "#findings")
        page = self.client.get(f"/api/v1/conversations/{conversation_id}/messages", params={"limit": 1})
        self.assertEqual(len(page.json()["data"]), 1)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_agent_inbox_routes_use_terminal_context(self):
        persistent_run_id = self._terminal_run("persistent", "researcher", "researcher-persistent")
        work_run_id = self._terminal_run("work", "AGENT-0001", "researcher-work")
        targeted = Messaging(self.connection).post("web-targeted", "human", "@researcher", "work assignment")
        self.connection.execute(
            "UPDATE deliveries SET terminal_run_id=? WHERE message_id=? AND actor_slug='researcher'",
            (work_run_id, targeted["id"]),
        )
        self.connection.commit()
        generic = Messaging(self.connection).post("web-generic", "human", "@researcher", "persistent notice")

        def authenticate(_, execution_id, *__):
            if execution_id == "persistent-execution":
                return AgentContext(persistent_run_id, "researcher", "persistent", "researcher", True)
            if execution_id == "work-execution":
                return AgentContext(work_run_id, "researcher", "work", "AGENT-0001", False)
            raise AssertionError(f"unexpected execution {execution_id}")

        def headers(execution_id: str) -> dict[str, str]:
            return {
                "Authorization": "Bearer test-token",
                "X-Agents-Execution-ID": execution_id,
            }

        with patch("agents.web.authenticate_agent", side_effect=authenticate):
            persistent_inbox = self.client.get("/agent/v1/inbox", headers=headers("persistent-execution"))
            self.assertEqual(persistent_inbox.status_code, 200)
            self.assertEqual(
                [row["body"] for row in persistent_inbox.json()["data"]],
                ["persistent notice"],
            )
            denied = self.client.post(
                "/agent/v1/inbox/ack",
                headers=headers("persistent-execution"),
                json={"request_id": "web-targeted-persistent", "message_ids": [targeted["id"]]},
            )
            self.assertEqual(denied.status_code, 403)

            work_inbox = self.client.get("/agent/v1/inbox", headers=headers("work-execution"))
            self.assertEqual(work_inbox.status_code, 200)
            self.assertEqual([row["body"] for row in work_inbox.json()["data"]], ["work assignment"])
            acknowledged = self.client.post(
                "/agent/v1/inbox/ack",
                headers=headers("work-execution"),
                json={"request_id": "web-targeted-work", "message_ids": [targeted["id"]]},
            )
            self.assertEqual(acknowledged.status_code, 200)
            generic_denied = self.client.post(
                "/agent/v1/inbox/ack",
                headers=headers("work-execution"),
                json={"request_id": "web-generic-work", "message_ids": [generic["id"]]},
            )
            self.assertEqual(generic_denied.status_code, 403)
            generic_ack = self.client.post(
                "/agent/v1/inbox/ack",
                headers=headers("persistent-execution"),
                json={"request_id": "web-generic-persistent", "message_ids": [generic["id"]]},
            )
            self.assertEqual(generic_ack.status_code, 200)

    def test_snapshot_roster_exposes_terminal_purpose(self):
        self.client.post("/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"})
        self._terminal_run("work", "W-18", "researcher-work-18")
        data = self.client.get("/api/v1/snapshot").json()["data"]
        researcher = next(row for row in data["roster"] if row["slug"] == "researcher")
        self.assertEqual(researcher["terminal_purpose_kind"], "work")
        self.assertEqual(researcher["terminal_purpose_id"], "W-18")
        self.assertEqual(researcher["terminal_run_id"], 1)
        manager = next(row for row in data["roster"] if row["slug"] == "manager")
        self.assertIsNone(manager["terminal_run_id"])
        self.assertIsNone(manager["terminal_purpose_kind"])
        self.assertIsNone(manager["terminal_purpose_id"])

    def test_dashboard_assets_and_work_evidence_contract(self):
        self.client.post("/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"})
        csrf = self.client.cookies.get("agents_csrf")
        intake = self.client.post(
            "/api/v1/intake",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "dashboard-detail",
            },
            json={"kind": "story", "title": "Inspect", "problem": "P", "outcome": "O"},
        ).json()["data"]
        page = self.client.get("/")
        self.assertIn('id="board"', page.text)
        self.assertIn('id="intake-dialog"', page.text)
        self.assertIn('id="incident-dialog"', page.text)
        self.assertIn("Internet exploration", page.text)
        self.assertIn('option value="research">Research</option>', page.text)
        self.assertIn('option value="publishing">Publishing</option>', page.text)
        self.assertIn('name="review_gate" value="publishing"', page.text)
        self.assertIn('name="review_gate" value="coordination"', page.text)
        self.assertIn("Research (required)", page.text)
        self.assertNotIn("Engineering operations", page.text)
        self.assertNotIn('value="backend"', page.text)
        self.assertNotIn('value="frontend"', page.text)
        self.assertNotIn('value="platform"', page.text)
        self.assertNotIn('value="security"', page.text)
        self.assertNotIn('value="architecture"', page.text)
        self.assertNotIn('value="design"', page.text)
        detail = self.client.get(f"/api/v1/work/{intake['id']}").json()["data"]
        self.assertEqual(
            set(detail),
            {
                "work",
                "criteria",
                "dependencies",
                "consultations",
                "decisions",
                "executions",
                "submissions",
                "checks",
                "reviews",
                "blockers",
            },
        )

    def test_terminal_answer_requires_open_prompt_and_is_one_shot(self):
        self.client.post("/auth/login", data={"token": "w" * 64}, headers={"Origin": "http://testserver"})
        csrf = self.client.cookies.get("agents_csrf")
        now = utc_now()
        terminal_id = self.connection.execute(
            "INSERT INTO terminal_runs(execution_name,profile_name,mcp_name,profile_sha256,provider,model,"
            "generation,actor_slug,purpose_kind,purpose_id,working_directory,token_digest,backend_terminal_id,"
            "agent_auth_id,profile_state,state,created_at,updated_at) VALUES("
            "'session','profile','mcp','sha','mock','model',1,'manager','persistent','manager','.',"
            "'digest','terminal','profile','installed','live',?,?)",
            (now, now),
        ).lastrowid
        self.assertIsNotNone(terminal_id)
        self.connection.execute(
            "INSERT INTO blockers(target_kind,target_id,terminal_run_id,kind,reason,requested_role,"
            "actor_slug,state,created_at,updated_at) VALUES("
            "'persistent','manager',?,'waiting_user_answer','Question','human','manager','open',?,?)",
            (terminal_id, now, now),
        )
        self.connection.commit()
        headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf, "Idempotency-Key": "answer-1"}
        first = self.client.post(f"/api/v1/terminals/{terminal_id}/answer", headers=headers, json={"body": "Proceed"})
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/api/v1/terminals/{terminal_id}/answer",
            headers={**headers, "Idempotency-Key": "answer-2"},
            json={"body": "Proceed again"},
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "conflict")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM terminal_inputs WHERE terminal_run_id=?", (terminal_id,)
            ).fetchone()[0],
            1,
        )
