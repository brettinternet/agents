from __future__ import annotations

import json
import unittest

import httpx

from agents.cao_client import CaoClient, CaoNotFound, CaoUnavailable


class CaoClientTests(unittest.TestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            values = {
                "/health": {"ok": True},
                "/openapi.json": {"paths": {}},
                "/sessions": [],
                "/sessions/name": {"name": "name"},
                "/sessions/name/terminals": [{"id": "terminal"}],
                "/terminals/terminal": {"id": "terminal", "status": "idle"},
                "/terminals/terminal/working-directory": {"working_directory": "/repo"},
                "/terminals/terminal/output": {"output": "text"},
                "/terminals/terminal/inbox/messages": {"message_id": 7},
                "/terminals/terminal/input": {"success": True},
            }
            if request.method == "POST" and path == "/sessions":
                return httpx.Response(201, json={"id": "terminal"})
            if request.method == "DELETE" and path == "/sessions/name":
                return httpx.Response(204)
            return httpx.Response(200, json=values[path])

        self.http = httpx.Client(base_url="http://127.0.0.1:9889", transport=httpx.MockTransport(handler))
        self.client = CaoClient(9889, client=self.http)

    def tearDown(self):
        self.http.close()

    def test_all_public_routes_and_shapes(self):
        self.assertTrue(self.client.health())
        self.assertEqual(self.client.openapi(), {"paths": {}})
        self.assertEqual(self.client.list_sessions(), [])
        self.assertEqual(self.client.get_session("name")["name"], "name")
        self.assertEqual(self.client.list_terminals("name")[0]["id"], "terminal")
        self.assertEqual(self.client.get_terminal("terminal")["status"], "idle")
        self.assertEqual(self.client.get_working_directory("terminal"), "/repo")
        self.assertEqual(self.client.get_output("terminal"), "text")
        self.assertEqual(self.client.enqueue_wake("terminal", "terminal", "wake"), "7")
        self.assertTrue(self.client.send_input("terminal", "answer"))
        self.client.delete_session("name")
        self.assertIn(
            "mode=full", str(next(request.url for request in self.requests if request.url.path.endswith("/output")))
        )
        self.assertIn(
            "sender_id=terminal",
            str(next(request.url for request in self.requests if request.url.path.endswith("/inbox/messages"))),
        )

    def test_create_session_uses_json_env_query_contract_and_long_timeout(self):
        value = self.client.create_session(
            profile="profile",
            provider="mock_cli",
            session_name="cao-agents-name",
            working_directory="/repo",
            allowed_tools=["fs_read", "fs_list"],
            env_vars={"TOKEN": "secret"},
            model="model",
        )
        self.assertEqual(value["id"], "terminal")
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/sessions")
        self.assertEqual(request.url.params["session_name"], "cao-agents-name")
        self.assertEqual(request.url.params["model"], "model")
        self.assertEqual(json.loads(request.content), {"env_vars": {"TOKEN": "secret"}})
        self.assertEqual(request.extensions["timeout"]["read"], 120.0)

    def test_path_identifiers_are_percent_encoded(self):
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(200, json={"id": "terminal", "status": "idle"})

        http = httpx.Client(
            base_url="http://127.0.0.1:9889",
            transport=httpx.MockTransport(handler),
        )
        client = CaoClient(9889, client=http)
        client.get_terminal("id/with?reserved#chars")
        self.assertEqual(
            seen,
            [b"/terminals/id%2Fwith%3Freserved%23chars"],
        )
        http.close()

    def test_not_found_and_malformed_shapes_fail_closed(self):
        missing = httpx.Client(
            base_url="http://127.0.0.1:9889",
            transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})),
        )
        client = CaoClient(9889, client=missing)
        with self.assertRaises(CaoNotFound):
            client.get_session("missing")
        missing.close()
        malformed = httpx.Client(
            base_url="http://127.0.0.1:9889",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )
        client = CaoClient(9889, client=malformed)
        with self.assertRaises(CaoUnavailable):
            client.list_sessions()
        malformed.close()
