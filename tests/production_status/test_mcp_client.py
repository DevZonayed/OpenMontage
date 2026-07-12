"""Streamable-HTTP MCP JSON-RPC client — transport + protocol validation."""

from __future__ import annotations

import pytest

from lib.production_brain.mcp_client import (
    McpAuthError,
    McpError,
    McpToolError,
    MochletMcpClient,
    build_mcp_client,
)
from tests.production_status._fake_mcp import FakeMochletMcp

ENDPOINT = "http://127.0.0.1:9235/mcp"


def _client(fake: FakeMochletMcp, token="secret-token"):
    return MochletMcpClient(ENDPOINT, transport=fake.transport, token_getter=lambda: token)


def test_initialize_records_serverinfo_and_session():
    fake = FakeMochletMcp()
    c = _client(fake)
    result = c.initialize()
    assert result["serverInfo"]["name"] == "mochlet"
    assert c.server_info["name"] == "mochlet"
    assert c._session_id == fake.session_id
    assert fake.initialized is True  # notifications/initialized sent


def test_ping_and_tools_list():
    fake = FakeMochletMcp()
    c = _client(fake)
    c.initialize()
    assert c.ping() == {}
    tools = c.list_tools()
    assert "sendChat" in tools and "cancelJob" in tools and "health" in tools


def test_call_tool_health_structured():
    fake = FakeMochletMcp()
    c = _client(fake)
    c.initialize()
    out = c.call_tool("health", {})
    assert out["ok"] is True and out["version"] == fake.version


def test_unauthorized_raises_auth_error():
    fake = FakeMochletMcp(token="right")
    c = _client(fake, token="wrong")
    with pytest.raises(McpAuthError):
        c.initialize()


def test_missing_token_is_unauthorized():
    fake = FakeMochletMcp()
    c = MochletMcpClient(ENDPOINT, transport=fake.transport, token_getter=lambda: None)
    with pytest.raises(McpAuthError):
        c.initialize()


def test_redirect_is_refused():
    fake = FakeMochletMcp(redirect=True)
    c = _client(fake)
    with pytest.raises(McpError) as ei:
        c.initialize()
    assert "redirect" in str(ei.value).lower()


def test_iserror_tool_raises_tool_error():
    fake = FakeMochletMcp(tools=["health", "listProjects"])  # no sendChat
    c = _client(fake)
    c.initialize()
    with pytest.raises(McpToolError):
        c.call_tool("sendChat", {"projectId": "p", "text": "hi"})


def test_event_stream_response_is_parsed():
    fake = FakeMochletMcp(event_stream=True)
    c = _client(fake)
    c.initialize()
    out = c.call_tool("health", {})
    assert out["ok"] is True


def test_malformed_envelope_raises():
    def bad_transport(url, *, json, headers, timeout):
        class R:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def json(self):
                return {"not": "jsonrpc"}
        return R()

    c = MochletMcpClient(ENDPOINT, transport=bad_transport, token_getter=lambda: "t")
    with pytest.raises(McpError):
        c.initialize()


def test_token_is_forwarded_in_headers_both_forms():
    captured = {}

    def spy(url, *, json, headers, timeout):
        captured.update(headers)
        return FakeMochletMcp().transport(url, json=json, headers=headers, timeout=timeout)

    c = MochletMcpClient(ENDPOINT, transport=spy, token_getter=lambda: "secret-token")
    c.initialize()
    assert captured["Authorization"] == "Bearer secret-token"
    assert captured["x-maestro-mcp-token"] == "secret-token"
    assert "text/event-stream" in captured["Accept"]


def test_trailing_notification_does_not_shadow_the_response():
    # An SSE body with a trailing server notification must still resolve to the
    # id-matched result, not the last event (regression: last-event parsing).
    fake = FakeMochletMcp(event_stream=True, trailing_notification=True)
    c = _client(fake)
    c.initialize()
    out = c.call_tool("health", {})
    assert out["ok"] is True


def test_response_id_is_correlated():
    # A response with a MISMATCHED id must not be accepted as this call's answer.
    def wrong_id(url, *, json, headers, timeout):
        class R:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return {"jsonrpc": "2.0", "id": 999, "result": {"tools": []}}
        return R()

    c = MochletMcpClient(ENDPOINT, transport=wrong_id, token_getter=lambda: "t")
    with pytest.raises(McpError):
        c.initialize()


def test_build_mcp_client_rejects_bad_endpoint():
    from lib.production_brain.orchestrator import OrchestratorUnavailable
    with pytest.raises(OrchestratorUnavailable):
        build_mcp_client("http://example.com/mcp")  # non-loopback plain http
