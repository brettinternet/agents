from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import httpx
from fastmcp import FastMCP

from .delivery import DecisionOption

mcp = FastMCP("Agents")


def _headers() -> dict[str, str]:
    token = os.environ.get("AGENTS_AGENT_TOKEN", "")
    execution_id = os.environ.get("AGENTS_EXECUTION_ID", "")
    if not token or not execution_id:
        raise RuntimeError("Agents agent environment is incomplete")
    return {"Authorization": f"Bearer {token}", "X-Agents-Execution-ID": execution_id}


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    base = os.environ.get("AGENTS_API_URL", "")
    if not base.startswith("http://127.0.0.1:"):
        raise RuntimeError("AGENTS_API_URL must be loopback HTTP")
    if body is not None and len(str(body).encode()) > 64 * 1024:
        raise ValueError("request exceeds 64 KiB")
    response = httpx.request(method, base.rstrip("/") + "/agent/v1" + path, headers=_headers(), json=body, timeout=30)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError("Agents API returned an invalid envelope")
    return value.get("data")


_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
_JINA_READER_ENDPOINT = "https://r.jina.ai/"
_INTERNET_TIMEOUT = 20.0
_INTERNET_READ_TIMEOUT = 5.0
_INTERNET_STREAM_TIMEOUT = 30.0
_INTERNET_HEADERS = {
    "Accept": "text/html, text/plain, text/markdown",
    "Accept-Encoding": "identity",
    "User-Agent": "agents-mcp/0.1 (+https://github.com/openai/agents)",
}
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_QUERY_CHARS = 500
_MAX_URL_BYTES = 2 * 1024
_MAX_SEARCH_LIMIT = 10
_MAX_RESULT_FIELD_CHARS = 4 * 1024


def _internet_timeout() -> httpx.Timeout:
    return httpx.Timeout(_INTERNET_TIMEOUT, read=min(_INTERNET_TIMEOUT, _INTERNET_READ_TIMEOUT))


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if not query.strip():
        raise ValueError("query must not be empty")
    if len(query) > _MAX_QUERY_CHARS:
        raise ValueError("query exceeds 500 characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in query):
        raise ValueError("query contains control characters")
    return query


def _validate_url(url: str) -> None:
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    if not url:
        raise ValueError("url must not be empty")
    if len(url.encode("utf-8")) > _MAX_URL_BYTES:
        raise ValueError("url exceeds 2 KiB")
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in url):
        raise ValueError("url contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url is malformed") from exc
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("url must use http or https and include a host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("url port is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url credentials are not allowed")
    host = hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local hosts are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("local or reserved hosts are not allowed")


def _check_response_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError("response stream timed out (total deadline exceeded)")


async def _response_bytes(response: httpx.Response, deadline: float) -> bytes:
    content_encoding = response.headers.get("content-encoding", "")
    if any(encoding.strip().lower() != "identity" for encoding in content_encoding.split(",") if encoding.strip()):
        raise RuntimeError("encoded responses are not allowed")

    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise RuntimeError("response has an invalid content length") from exc
        if declared_length < 0 or declared_length > _MAX_RESPONSE_BYTES:
            raise RuntimeError("response exceeds 1 MiB")

    chunks: list[bytes] = []
    total = 0
    iterator = response.aiter_raw().__aiter__()
    while True:
        _check_response_deadline(deadline)
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            _check_response_deadline(deadline)
            break
        _check_response_deadline(deadline)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise RuntimeError("response yielded invalid bytes")
        chunk_bytes = bytes(chunk)
        total += len(chunk_bytes)
        if total > _MAX_RESPONSE_BYTES:
            raise RuntimeError("response exceeds 1 MiB")
        chunks.append(chunk_bytes)
    return b"".join(chunks)


async def _response_text(response: httpx.Response, deadline: float) -> str:
    try:
        return (await _response_bytes(response, deadline)).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("response is not valid UTF-8") from exc


async def _stream_text_async(url: str, **kwargs: Any) -> str:
    deadline = time.monotonic() + _INTERNET_STREAM_TIMEOUT
    try:
        async with asyncio.timeout(_INTERNET_STREAM_TIMEOUT):
            async with httpx.AsyncClient(timeout=_internet_timeout(), headers=_INTERNET_HEADERS) as client:
                async with client.stream("GET", url, **kwargs) as response:
                    response.raise_for_status()
                    return await _response_text(response, deadline)
    except TimeoutError as exc:
        raise RuntimeError("response stream timed out (total deadline exceeded)") from exc


def _stream_text(url: str, **kwargs: Any) -> str:
    return asyncio.run(_stream_text_async(url, **kwargs))


class _SearchResultParser(HTMLParser):
    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_results = max_results
        self._results: list[dict[str, str]] = []
        self._depth = 0
        self._result_depth: int | None = None
        self._result: dict[str, str] | None = None
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._capture_depth = 0
        self._capture_parts: list[str] = []
        self._capture_size = 0
        self._capture_href: str | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        values = dict(attrs)
        return set((values.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        values = dict(attrs)
        self._depth += 1
        if self._result_depth is None and tag == "div" and "result" in classes:
            self._result_depth = self._depth
            self._result = {"title": "", "link": "", "snippet": ""}
        if self._result is None or self._capture is not None or tag != "a":
            return
        if "result__a" in classes and not self._result["title"]:
            self._begin_capture("title", tag, values.get("href"))
        elif "result__snippet" in classes and not self._result["snippet"]:
            self._begin_capture("snippet", tag, None)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and tag == self._capture_tag and self._depth == self._capture_depth:
            self._finish_capture()
        if self._result_depth is not None and tag == "div" and self._depth == self._result_depth:
            if self._capture is not None:
                self._finish_capture()
            self._finish_result()
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture is None:
            return
        self._capture_size += len(data)
        if self._capture_size > _MAX_RESULT_FIELD_CHARS:
            raise RuntimeError("search result field exceeds bound")
        self._capture_parts.append(data)

    def _begin_capture(self, kind: str, tag: str, href: str | None) -> None:
        self._capture = kind
        self._capture_tag = tag
        self._capture_depth = self._depth
        self._capture_parts = []
        self._capture_size = 0
        self._capture_href = href

    def _finish_capture(self) -> None:
        if self._capture is None or self._result is None:
            return
        value = " ".join("".join(self._capture_parts).split())
        self._result[self._capture] = value
        if self._capture == "title" and self._capture_href is not None:
            self._result["link"] = self._capture_href.strip()
        self._capture = None
        self._capture_tag = None
        self._capture_parts = []
        self._capture_size = 0
        self._capture_href = None

    def _finish_result(self) -> None:
        if self._result is not None and len(self._results) < self._max_results:
            self._results.append(self._result)
        self._result = None
        self._result_depth = None

    def finish(self) -> list[dict[str, str]]:
        if self._capture is not None:
            self._finish_capture()
        if self._result is not None:
            self._finish_result()
        return self._results


def _normalise_result_link(link: str) -> str | None:
    link = link.strip()
    if not link:
        return None
    if link.startswith("//"):
        link = "https:" + link
    elif link.startswith("/"):
        link = urljoin(_SEARCH_ENDPOINT, link)
    try:
        parsed = urlsplit(link)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        _ = parsed.port
    except ValueError:
        return None
    if len(link.encode("utf-8")) > _MAX_URL_BYTES:
        return None
    if "uddg" in parse_qs(parsed.query):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return _normalise_result_link(unquote(target))
    return link


@mcp.tool
def search_web(query: str, limit: int = 5) -> list[dict[str, str]]:
    query = _validate_query(query)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_SEARCH_LIMIT:
        raise ValueError("limit must be between 1 and 10")
    document = _stream_text(_SEARCH_ENDPOINT, params={"q": query})
    parser = _SearchResultParser(limit)
    try:
        parser.feed(document)
        parser.close()
        parsed_results = parser.finish()
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError("search response could not be parsed") from exc
    results: list[dict[str, str]] = []
    for result in parsed_results:
        title = " ".join(result["title"].split())
        snippet = " ".join(result["snippet"].split())
        link = _normalise_result_link(result["link"])
        if not title or link is None:
            continue
        if len(title) > _MAX_RESULT_FIELD_CHARS or len(snippet) > _MAX_RESULT_FIELD_CHARS:
            raise RuntimeError("search result field exceeds bound")
        results.append({"title": title, "link": link, "snippet": snippet})
    return results


@mcp.tool
def fetch_url(url: str) -> dict[str, str]:
    _validate_url(url)
    text = _stream_text(_JINA_READER_ENDPOINT + url)
    return {"source_url": url, "text": text}


@mcp.tool
def backlog_list(status: str | None = None, limit: int = 50, after_id: str | None = None) -> Any:
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if after_id:
        params["after_id"] = after_id
    return _request("GET", "/backlog?" + urlencode(params))


@mcp.tool
def backlog_get(item_id: str) -> Any:
    return _request("GET", f"/backlog/{item_id}")


@mcp.tool
def backlog_create(
    request_id: str, kind: str, title: str, problem: str, outcome: str, parent_id: str | None = None
) -> Any:
    return _request("POST", "/backlog", locals())


@mcp.tool
def backlog_start_refinement(request_id: str, item_id: str, expected_version: int) -> Any:
    return _request("POST", f"/backlog/{item_id}/start-refinement", locals())


@mcp.tool
def backlog_refine(
    request_id: str,
    item_id: str,
    expected_version: int,
    title: str,
    problem: str,
    outcome: str,
    priority: str,
    specialty: str,
    acceptance_criteria: list[str],
    dependencies: list[str],
    review_gates: list[str],
    kind: str = "story",
) -> Any:
    return _request("PUT", f"/backlog/{item_id}", locals())


@mcp.tool
def backlog_mark_ready(request_id: str, item_id: str, expected_version: int) -> Any:
    return _request("POST", f"/backlog/{item_id}/ready", locals())


@mcp.tool
def reopen_work(request_id: str, item_id: str, expected_version: int, reason: str) -> Any:
    return _request("POST", f"/backlog/{item_id}/reopen", locals())


@mcp.tool
def cancel_work(request_id: str, item_id: str, expected_version: int, reason: str) -> Any:
    return _request("POST", f"/backlog/{item_id}/cancel", locals())


@mcp.tool
def request_consultation(request_id: str, item_id: str, expected_version: int, specialty: str, question: str) -> Any:
    return _request("POST", f"/backlog/{item_id}/consultations", locals())


@mcp.tool
def submit_consultation(request_id: str, consultation_id: int, expected_version: int, response: str) -> Any:
    return _request("POST", f"/consultations/{consultation_id}/submit", locals())


@mcp.tool
def propose_decision(
    request_id: str,
    title: str,
    question: str,
    options: list[DecisionOption],
    recommendation: str,
    item_id: str | None = None,
    expected_version: int | None = None,
) -> Any:
    return _request("POST", "/decisions", locals())


@mcp.tool
def post_message(request_id: str, to: str, body: str, reply_to: int | None = None, urgency: str = "normal") -> Any:
    return _request("POST", "/messages", locals())


@mcp.tool
def inbox(limit: int = 50) -> Any:
    return _request("GET", f"/inbox?limit={limit}")


@mcp.tool
def ack_inbox(request_id: str, message_ids: list[int]) -> Any:
    return _request("POST", "/inbox/ack", locals())


@mcp.tool
def conversation_history(address: str, before_id: int | None = None, limit: int = 50) -> Any:
    params: dict[str, Any] = {"address": address, "limit": limit}
    if before_id is not None:
        params["before_id"] = before_id
    return _request("GET", "/conversations/history?" + urlencode(params))


@mcp.tool
def get_assignment() -> Any:
    return _request("GET", "/assignment")


@mcp.tool
def report_progress(request_id: str, item_id: str, expected_version: int, summary: str) -> Any:
    return _request("POST", f"/backlog/{item_id}/progress", locals())


@mcp.tool
def block_work(request_id: str, item_id: str, expected_version: int, reason: str, requested_role: str) -> Any:
    return _request("POST", f"/backlog/{item_id}/block", locals())


@mcp.tool
def resolve_blocker(
    request_id: str,
    blocker_id: int,
    resolution: str,
    action: str = "resume",
    item_id: str | None = None,
    expected_version: int | None = None,
) -> Any:
    return _request("POST", f"/blockers/{blocker_id}/resolve", locals())


@mcp.tool
def submit_work(request_id: str, item_id: str, expected_version: int, commit_sha: str, summary: str) -> Any:
    return _request("POST", f"/backlog/{item_id}/submit", locals())


@mcp.tool
def submit_review(
    request_id: str, item_id: str, submission_id: int, expected_version: int, gate: str, verdict: str, body: str
) -> Any:
    return _request("POST", f"/reviews/{submission_id}/{gate}", locals())


def main() -> None:
    mcp.run()
