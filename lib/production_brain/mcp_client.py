"""Minimal Streamable-HTTP MCP (JSON-RPC 2.0) client for the local Mochlet brain.

Mochlet exposes an AUTHENTICATED Model-Context-Protocol server over Streamable
HTTP on the loopback gateway (``POST http://127.0.0.1:9235/mcp``) — not a generic
REST API. Verified live against the installed app:

    GET  /health  -> 404
    POST /jobs    -> 404
    POST /mcp     -> 401 {"error":"unauthorized"}   (accepts Authorization +
                    x-maestro-mcp-token; exposes mcp-session-id)

So the health/verify handshake and job control MUST speak JSON-RPC MCP:
``initialize`` → ``notifications/initialized`` → ``tools/list`` → ``tools/call``.

Security posture (a bearer token rides every request):
  * endpoint validated fail-closed (loopback-HTTP allowed, else HTTPS) upstream;
  * redirects DISABLED and any 3xx rejected (never replay the token);
  * bounded timeout; content-type + JSON-RPC envelope + ``isError`` validated;
  * the token is read at call time from the OS keyring and is NEVER logged,
    returned, or written to a file/telemetry — only ``[redacted]`` ever appears.

This module is pure transport: it knows JSON-RPC + the MCP framing, not Mochlet's
tool semantics. The orchestrator (:mod:`lib.production_brain.mochlet`) layers the
``listProjects``/``sendChat``/``cancelJob`` contract on top.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from lib.production_brain.orchestrator import OrchestratorUnavailable, validate_endpoint

MCP_PROTOCOL_VERSION = "2025-06-18"
_HTTP_TIMEOUT_SECONDS = 20
_CLIENT_INFO = {"name": "openmontage-backlot", "version": "1.0"}


class McpError(RuntimeError):
    """A transport/protocol failure talking to the MCP server (sanitized)."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class McpAuthError(McpError):
    """The MCP server rejected the credential (401/403) — needs a valid token."""


class McpToolError(McpError):
    """A ``tools/call`` returned ``isError: true`` (sanitized message)."""


def _default_transport(url: str, *, json: dict, headers: dict, timeout: float):
    import requests  # a declared dependency

    return requests.post(url, json=json, headers=headers, timeout=timeout,
                         allow_redirects=False)


class MochletMcpClient:
    """A single-connection JSON-RPC MCP client bound to one ``/mcp`` endpoint.

    ``transport(url, *, json, headers, timeout)`` is injectable so the test suite
    never touches the network or the keyring. ``token_getter()`` returns the bearer
    token (or None) at call time; its value is never stored on the instance.
    """

    def __init__(self, endpoint: str, *, transport: Optional[Callable[..., Any]] = None,
                 token_getter: Optional[Callable[[], Optional[str]]] = None,
                 timeout: float = _HTTP_TIMEOUT_SECONDS) -> None:
        self._endpoint = endpoint
        self._transport = transport or _default_transport
        self._token_getter = token_getter or (lambda: None)
        self._timeout = timeout
        self._session_id: Optional[str] = None
        self._next_id = 0
        self._initialized = False
        self.server_info: dict = {}
        self.capabilities: dict = {}

    # -- headers / transport -------------------------------------------------
    def _headers(self, *, notification: bool = False) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        token = self._token_getter()
        if token:
            # Mochlet accepts either header; send both. Never logged.
            headers["Authorization"] = f"Bearer {token}"
            headers["x-maestro-mcp-token"] = token
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _post(self, body: dict, *, notification: bool = False) -> Optional[dict]:
        try:
            resp = self._transport(self._endpoint, json=body,
                                   headers=self._headers(notification=notification),
                                   timeout=self._timeout)
        except Exception as exc:  # network / library error — sanitized, no token
            raise McpError(f"MCP request failed ({exc.__class__.__name__}).") from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        if 300 <= status < 400:
            raise McpError(
                f"MCP endpoint returned a redirect (HTTP {status}); refusing to follow "
                "(a bearer token must never be replayed to a redirect target).",
                status=status)
        if status in (401, 403):
            raise McpAuthError("MCP server rejected the credential (unauthorized).",
                              status=status)
        if status >= 400:
            raise McpError(f"MCP server returned HTTP {status}.", status=status)
        # Capture the session id the server assigns on initialize.
        sid = _header(resp, "mcp-session-id")
        if sid:
            self._session_id = sid
        if notification:
            return None
        return self._parse_message(resp, body.get("id"))

    def _parse_message(self, resp: Any, expected_id: Any) -> dict:
        ctype = (_header(resp, "content-type") or "").lower()
        if "text/event-stream" in ctype:
            messages = _parse_sse_messages(_text(resp))
        elif "application/json" in ctype or not ctype:
            try:
                messages = [resp.json()]
            except Exception as exc:
                raise McpError("MCP server returned a non-JSON response.") from exc
        else:
            raise McpError(f"MCP server returned an unexpected content-type ({ctype}).")
        # Correlate by JSON-RPC id — a Streamable-HTTP body may also carry server
        # notifications/requests; we must answer THIS request, not the last frame.
        msg = None
        for m in messages:
            if isinstance(m, dict) and m.get("jsonrpc") == "2.0" and m.get("id") == expected_id:
                msg = m
                break
        if msg is None:
            # Some servers omit the id echo; accept a lone response frame.
            responses = [m for m in messages if isinstance(m, dict) and m.get("jsonrpc") == "2.0"
                         and ("result" in m or "error" in m) and "id" in m and m.get("id") is None]
            if len(responses) == 1:
                msg = responses[0]
        if msg is None:
            raise McpError("MCP server returned no JSON-RPC response matching the request id.")
        if "error" in msg and msg["error"] is not None:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            code = err.get("code")
            # Do NOT echo server data blobs (may contain sensitive detail).
            raise McpError(f"MCP error {code}: {str(err.get('message'))[:200]}")
        if "result" not in msg:
            raise McpError("MCP response had neither result nor error.")
        return msg["result"]

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        return self._post(body)

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._post(body, notification=True)

    # -- MCP lifecycle -------------------------------------------------------
    def initialize(self) -> dict:
        """Perform the MCP handshake; record serverInfo + capabilities + session."""
        result = self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        if not isinstance(result, dict):
            raise McpError("MCP initialize returned an unexpected result.")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        # Best-effort initialized notification (some servers 202 with no body).
        try:
            self._notify("notifications/initialized")
        except McpError:
            pass
        self._initialized = True
        return result

    def ping(self) -> dict:
        return self._rpc("ping", {})

    def list_tools(self) -> list[str]:
        result = self._rpc("tools/list", {})
        tools = (result or {}).get("tools")
        names: list[str] = []
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    names.append(t["name"])
        return names

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        """Call a tool; return its structured payload. Raises on ``isError``.

        Returns a dict: ``structuredContent`` when present, else the parsed JSON of
        the first text content block, else ``{"text": <text>}``.
        """
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise McpError(f"tool {name} returned an unexpected result.")
        if result.get("isError"):
            raise McpToolError(f"tool {name} failed: {_first_text(result)[:200]}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        text = _first_text(result)
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                pass
            return {"text": text}
        return {}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _header(resp: Any, name: str) -> Optional[str]:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        # requests uses a case-insensitive dict; a plain dict needs help.
        if hasattr(headers, "get"):
            val = headers.get(name)
            if val is None:
                for k, v in dict(headers).items():
                    if k.lower() == name.lower():
                        return v
            return val
    except Exception:
        return None
    return None


def _text(resp: Any) -> str:
    txt = getattr(resp, "text", None)
    if callable(txt):
        return txt()
    return txt or ""


def _parse_sse_messages(body: str) -> list:
    """Parse EVERY JSON ``data:`` event in an SSE stream (the caller id-matches)."""
    messages: list = []
    data_lines: list[str] = []

    def _flush():
        if not data_lines:
            return
        blob = "\n".join(data_lines)
        try:
            messages.append(json.loads(blob))
        except ValueError:
            pass  # skip non-JSON events (comments/keepalives)

    for raw in body.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "":
            _flush()
            data_lines = []
    _flush()
    if not messages:
        raise McpError("MCP event-stream carried no JSON data.")
    return messages


def _first_text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                return block["text"]
    return ""


def build_mcp_client(endpoint: str, *, transport: Optional[Callable[..., Any]] = None,
                     token_getter: Optional[Callable[[], Optional[str]]] = None) -> MochletMcpClient:
    """Validate the endpoint fail-closed and return a client bound to it."""
    validate_endpoint(endpoint)  # raises OrchestratorUnavailable on a bad endpoint
    return MochletMcpClient(endpoint, transport=transport, token_getter=token_getter)
