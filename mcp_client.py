"""Minimal MCP Streamable-HTTP client for LetsFG.

Used by the Groq agent for the full tool-call path (initialize, list tools,
call tools). The Claude agent uses Anthropic's built-in MCP connector and
does not touch this file.

MCP transport reference: https://modelcontextprotocol.io/specification
Requests are JSON-RPC 2.0 posted to one URL; the server replies with either
application/json or a text/event-stream carrying `message` events. The server
may require a session id returned in the `mcp-session-id` response header on
initialize; we echo it back on every follow-up.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
from typing import Any, Iterable

import httpx

log = logging.getLogger("mcp")

PROTOCOL_VERSION = "2025-06-18"  # current stable MCP revision


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 180.0):
        self._url = url
        self._token = token
        self._timeout = timeout
        self._session_id: str | None = None
        self._initialized = False
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(headers=headers, timeout=timeout)

    # ---- transport ----

    def _post(self, payload: dict) -> dict | None:
        """Send one JSON-RPC request. Returns the response dict, or None for notifications."""
        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        try:
            resp = self._http.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise MCPError(f"MCP transport failed: {e}") from e

        new_sid = resp.headers.get("mcp-session-id")
        if new_sid:
            self._session_id = new_sid

        if resp.status_code == 202:  # accepted notification, no body
            return None
        if resp.status_code >= 400:
            raise MCPError(f"MCP HTTP {resp.status_code}: {resp.text[:400]}")

        is_notification = "id" not in payload
        if is_notification or not resp.content:
            return None

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return self._parse_sse(resp.text, payload.get("id"))
        try:
            return resp.json()
        except ValueError as e:
            raise MCPError(f"MCP non-JSON response: {resp.text[:200]}") from e

    @staticmethod
    def _parse_sse(body: str, want_id: Any) -> dict:
        """Pull out the JSON-RPC message with id == want_id from an SSE body."""
        current_event = "message"
        data_lines: list[str] = []
        last_match: dict | None = None
        for raw in body.splitlines() + [""]:
            if raw == "":
                if data_lines and current_event == "message":
                    try:
                        msg = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        msg = None
                    if isinstance(msg, dict) and msg.get("id") == want_id:
                        last_match = msg
                data_lines = []
                current_event = "message"
                continue
            if raw.startswith(":"):
                continue
            if ":" in raw:
                field, _, val = raw.partition(":")
                val = val[1:] if val.startswith(" ") else val
            else:
                field, val = raw, ""
            if field == "event":
                current_event = val
            elif field == "data":
                data_lines.append(val)
        if last_match is None:
            raise MCPError("MCP SSE stream ended without a response message")
        return last_match

    # ---- protocol ----

    def _request(self, method: str, params: dict | None = None) -> Any:
        rid = next(self._ids)
        payload: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload)
        if resp is None:
            raise MCPError(f"MCP {method} returned no body")
        if "error" in resp:
            err = resp["error"]
            raise MCPError(f"MCP {method} error {err.get('code')}: {err.get('message')}")
        return resp.get("result")

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)

    def initialize(self) -> dict:
        with self._lock:
            if self._initialized:
                return {}
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "traveldesk-bot", "version": "1.0"},
                },
            )
            self._notify("notifications/initialized")
            self._initialized = True
            return result

    def list_tools(self) -> list[dict]:
        self.initialize()
        result = self._request("tools/list")
        return list(result.get("tools", [])) if isinstance(result, dict) else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError(f"tools/call {name}: non-dict result")
        if result.get("isError"):
            text = _collect_text(result.get("content", []))
            raise MCPError(f"{name} reported error: {text[:400]}")
        return result

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass


def _collect_text(content: Iterable[dict]) -> str:
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(str(block.get("text", "")))
    return "\n".join(out)


def tool_result_text(result: dict) -> str:
    """Flatten a tool-result's `content` array into a single string."""
    return _collect_text(result.get("content") or [])


def tool_result_json(result: dict) -> Any:
    """Return `structuredContent` if the tool returned one, else parse the text."""
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    text = tool_result_text(result)
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
