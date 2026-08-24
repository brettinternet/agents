from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import httpx

from agents import mcp_server


class _AsyncResponseStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


class McpBoundaryTests(unittest.TestCase):
    def test_rejects_non_loopback_api(self):
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTS_API_URL": "https://example.com",
                    "AGENTS_AGENT_TOKEN": "secret",
                    "AGENTS_EXECUTION_ID": "execution",
                },
                clear=True,
            ),
            self.assertRaises(RuntimeError),
        ):
            mcp_server.backlog_get("AGENT-0001")

    def test_sends_bearer_execution_id_and_bounded_json(self):
        response = httpx.Response(
            200,
            json={"ok": True, "data": {"id": "AGENT-0001"}},
            request=httpx.Request("POST", "http://127.0.0.1:9890/agent/v1/backlog"),
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTS_API_URL": "http://127.0.0.1:9890",
                    "AGENTS_AGENT_TOKEN": "secret",
                    "AGENTS_EXECUTION_ID": "execution",
                },
                clear=True,
            ),
            patch("httpx.request", return_value=response) as request,
        ):
            data = mcp_server.backlog_create("request-1", "story", "Title", "Problem", "Outcome")
            self.assertEqual(data["id"], "AGENT-0001")
            headers = request.call_args.kwargs["headers"]
            self.assertEqual(headers["Authorization"], "Bearer secret")
            self.assertEqual(headers["X-Agents-Execution-ID"], "execution")
            self.assertNotIn("secret", str(request.call_args.kwargs["json"]))

    def test_managed_secret_tools_send_only_nonsecret_metadata(self):
        response = httpx.Response(
            200,
            json={"ok": True, "data": {"id": "request", "name": "SERVICE_TOKEN", "state": "pending"}},
            request=httpx.Request("POST", "http://127.0.0.1:9890/agent/v1/secrets/requests"),
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTS_API_URL": "http://127.0.0.1:9890",
                    "AGENTS_AGENT_TOKEN": "agent-token",
                    "AGENTS_EXECUTION_ID": "execution",
                },
                clear=True,
            ),
            patch("httpx.request", return_value=response) as request,
        ):
            mcp_server.request_managed_secret_set("SERVICE_TOKEN")
            self.assertEqual(request.call_args.args[:2], ("POST", "http://127.0.0.1:9890/agent/v1/secrets/requests"))
            self.assertEqual(request.call_args.kwargs["json"], {"name": "SERVICE_TOKEN"})

    def test_set_managed_secret_sends_name_and_value(self):
        response = httpx.Response(
            200,
            json={"ok": True, "data": {"name": "SERVICE_TOKEN", "state": "set"}},
            request=httpx.Request("POST", "http://127.0.0.1:9890/agent/v1/secrets"),
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTS_API_URL": "http://127.0.0.1:9890",
                    "AGENTS_AGENT_TOKEN": "agent-token",
                    "AGENTS_EXECUTION_ID": "execution",
                },
                clear=True,
            ),
            patch("httpx.request", return_value=response) as request,
        ):
            result = mcp_server.set_managed_secret("SERVICE_TOKEN", "chosen-value")
            self.assertEqual(request.call_args.args[:2], ("POST", "http://127.0.0.1:9890/agent/v1/secrets"))
            self.assertEqual(request.call_args.kwargs["json"], {"name": "SERVICE_TOKEN", "value": "chosen-value"})
            self.assertEqual(result, {"name": "SERVICE_TOKEN", "state": "set"})

    def test_query_parameters_are_encoded_and_cursor_is_forwarded(self):
        response = httpx.Response(
            200,
            json={"ok": True, "data": []},
            request=httpx.Request("GET", "http://127.0.0.1:9890/agent/v1/conversations/history"),
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTS_API_URL": "http://127.0.0.1:9890",
                    "AGENTS_AGENT_TOKEN": "secret",
                    "AGENTS_EXECUTION_ID": "execution",
                },
                clear=True,
            ),
            patch("httpx.request", return_value=response) as request,
        ):
            mcp_server.conversation_history("#general", before_id=4, limit=10)
            mcp_server.backlog_list(after_id="AGENT-0004", limit=10)
            mcp_server.repository_list("memory/research notes")
            urls = [call.args[1] for call in request.call_args_list]
            self.assertIn("address=%23general", urls[0])
            self.assertIn("before_id=4", urls[0])
            self.assertIn("after_id=AGENT-0004", urls[1])
            self.assertIn("path=memory%2Fresearch+notes", urls[2])

    def test_rejects_oversized_payload_and_bad_envelope(self):
        with patch.dict(
            os.environ,
            {
                "AGENTS_API_URL": "http://127.0.0.1:9890",
                "AGENTS_AGENT_TOKEN": "secret",
                "AGENTS_EXECUTION_ID": "execution",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                mcp_server.post_message("request-1", "#findings", "x" * (64 * 1024))
            response = httpx.Response(
                200,
                json={"ok": False, "error": {"code": "no"}},
                request=httpx.Request("GET", "http://127.0.0.1:9890/agent/v1/backlog/AGENT-0001"),
            )
            with patch("httpx.request", return_value=response), self.assertRaises(RuntimeError):
                mcp_server.backlog_get("AGENT-0001")

    def _mock_client(self, response: httpx.Response) -> MagicMock:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=response)
        stream.__aexit__ = AsyncMock(return_value=False)
        client.stream.return_value = stream
        return client

    def _stream_response(
        self,
        status: int,
        content: bytes,
        *,
        headers: dict[str, str] | None = None,
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status,
            headers=headers,
            stream=_AsyncResponseStream(content),
            request=request,
        )

    def test_search_web_parses_results_and_uses_fixed_endpoint(self):
        response = self._stream_response(
            200,
            b"""
            <div class="result">
              <a class="result__a" href="https://example.com/first">First &amp; Result</a>
              <a class="result__snippet">Summary &amp; detail</a>
            </div>
            <div class="result">
              <a class="result__a" href="https://example.org/second">Second result</a>
              <a class="result__snippet">Another summary</a>
            </div>
            """,
            request=httpx.Request("GET", "https://html.duckduckgo.com/html/"),
        )
        client = self._mock_client(response)
        with patch("agents.mcp_server.httpx.AsyncClient", return_value=client):
            results = mcp_server.search_web("safe query", limit=2)

        self.assertEqual(
            results,
            [
                {
                    "title": "First & Result",
                    "link": "https://example.com/first",
                    "snippet": "Summary & detail",
                },
                {
                    "title": "Second result",
                    "link": "https://example.org/second",
                    "snippet": "Another summary",
                },
            ],
        )
        request = client.stream.call_args
        self.assertEqual(request.args[0], "GET")
        self.assertEqual(request.args[1], "https://html.duckduckgo.com/html/")
        self.assertEqual(request.kwargs["params"], {"q": "safe query"})

    def test_fetch_url_uses_fixed_reader_proxy_and_returns_source(self):
        source_url = "https://example.com/articles/read?q=public"
        response = self._stream_response(
            200,
            b"# Article\n\nPublic source text.",
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/articles/read?q=public"),
        )
        client = self._mock_client(response)
        with patch("agents.mcp_server.httpx.AsyncClient", return_value=client):
            result = mcp_server.fetch_url(source_url)

        self.assertEqual(result, {"source_url": source_url, "text": "# Article\n\nPublic source text."})
        request = client.stream.call_args
        request_url = request.args[1]
        self.assertEqual(urlsplit(request_url).netloc, "r.jina.ai")
        self.assertNotEqual(request_url, source_url)
        self.assertEqual(request_url, f"https://r.jina.ai/{source_url}")

    def test_internet_tools_reject_invalid_inputs_before_http(self):
        client = MagicMock()
        with patch("agents.mcp_server.httpx.AsyncClient", return_value=client) as client_factory:
            for bad_url in (
                "ftp://example.com/document",
                "https://user:password@example.com/document",
                "https://example.com/" + ("x" * 10_000),
            ):
                with self.subTest(bad_url=bad_url[:20]), self.assertRaises(ValueError):
                    mcp_server.fetch_url(bad_url)

            with self.assertRaises(ValueError):
                mcp_server.search_web("x" * 10_000)
            with self.assertRaises(ValueError):
                mcp_server.search_web("valid query", limit=0)
            with self.assertRaises(ValueError):
                mcp_server.search_web("valid query", limit=1_000)

        client_factory.assert_not_called()

    def test_fetch_url_propagates_status_failure(self):
        response = self._stream_response(
            503,
            b"upstream unavailable",
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/document"),
        )
        client = self._mock_client(response)
        with (
            patch("agents.mcp_server.httpx.AsyncClient", return_value=client),
            self.assertRaises(httpx.HTTPStatusError),
        ):
            mcp_server.fetch_url("https://example.com/document")

    def test_fetch_url_rejects_response_over_one_mib(self):
        response = self._stream_response(
            200,
            b"x" * (1_048_576 + 1),
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/document"),
        )
        client = self._mock_client(response)
        with patch("agents.mcp_server.httpx.AsyncClient", return_value=client), self.assertRaises(RuntimeError):
            mcp_server.fetch_url("https://example.com/document")

    def test_fetch_url_rejects_slow_drip_after_total_stream_deadline(self):
        response = self._stream_response(
            200,
            b"",
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/document"),
        )
        now = [0.0]

        async def slow_drip():
            yield b"x"
            now[0] = mcp_server._INTERNET_STREAM_TIMEOUT + 0.1
            yield b"y"

        client = self._mock_client(response)
        with (
            patch.object(response, "aiter_raw", return_value=slow_drip()),
            patch("agents.mcp_server.httpx.AsyncClient", return_value=client),
            patch("agents.mcp_server.time.monotonic", side_effect=lambda: now[0]),
            self.assertRaisesRegex(RuntimeError, "stream timed out"),
        ):
            mcp_server.fetch_url("https://example.com/document")

    def test_internet_client_uses_bounded_connect_and_read_timeouts(self):
        response = self._stream_response(
            200,
            b"bounded response",
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/document"),
        )
        client = self._mock_client(response)
        with patch("agents.mcp_server.httpx.AsyncClient", return_value=client) as client_factory:
            mcp_server.fetch_url("https://example.com/document")

        timeout = client_factory.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, mcp_server._INTERNET_TIMEOUT)
        self.assertLess(timeout.read, timeout.connect)
        headers = client_factory.call_args.kwargs["headers"]
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertLessEqual(timeout.read, mcp_server._INTERNET_STREAM_TIMEOUT)

    def test_fetch_url_rejects_total_timeout_during_response_header_acquisition(self):
        response = self._stream_response(
            200,
            b"",
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/document"),
        )
        client = self._mock_client(response)
        client.stream.return_value.__aenter__ = AsyncMock(side_effect=TimeoutError("header stalled"))
        with (
            patch("agents.mcp_server.httpx.AsyncClient", return_value=client),
            self.assertRaisesRegex(RuntimeError, "stream timed out"),
        ):
            mcp_server.fetch_url("https://example.com/document")

    def test_fetch_url_rejects_non_identity_content_encoding(self):
        response = self._stream_response(
            200,
            b"encoded bytes",
            headers={"Content-Encoding": "gzip"},
            request=httpx.Request("GET", "https://r.jina.ai/https://example.com/document"),
        )
        client = self._mock_client(response)
        with (
            patch("agents.mcp_server.httpx.AsyncClient", return_value=client),
            self.assertRaisesRegex(RuntimeError, "encoded responses are not allowed"),
        ):
            mcp_server.fetch_url("https://example.com/document")
