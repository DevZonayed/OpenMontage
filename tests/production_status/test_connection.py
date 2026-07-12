"""Hermes/Mochlet connection layer — MCP verify + guided connect (no live I/O)."""

from __future__ import annotations

import pytest

from lib.production_brain import connection as C
from lib.production_brain.mochlet import MochletMcpOrchestratorClient
from lib.production_brain.orchestrator import ORCHESTRATOR_TOKEN_ACCOUNT
from tests.production_status._fake_mcp import ALL_TOOLS, FakeMochletMcp

PID = "669a5386-f37b-4c6f-a712-b12e8221e54d"
TOKEN = "secret-token"


class _MemSecrets:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def getter(self, account):
        return self.store.get(account)

    def setter(self, account, value):
        self.store[account] = value

    def deleter(self, account):
        return self.store.pop(account, None) is not None


def _down_transport(*a, **k):
    raise ConnectionError("refused")


# --------------------------------------------------------------------------- #
# MCP verify handshake
# --------------------------------------------------------------------------- #
def test_verify_mcp_capable():
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL, transport=FakeMochletMcp(token=TOKEN).transport, token=TOKEN)
    assert v["reachable"] and v["authenticated"]
    assert v["server_name"] == "mochlet"
    assert v["has_required_tools"] is True
    assert v["projects"][0]["id"] == PID


def test_verify_mcp_needs_token():
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL, transport=FakeMochletMcp(token="right").transport, token="wrong")
    assert v["reachable"] and v["needs_token"] and not v["authenticated"]


def test_verify_mcp_tools_disabled():
    fake = FakeMochletMcp(token=TOKEN, tools=["health", "listProjects"])  # no sendChat/cancelJob
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL, transport=fake.transport, token=TOKEN)
    assert v["authenticated"] and v["has_required_tools"] is False


def test_verify_mcp_unreachable():
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL, transport=_down_transport, token=TOKEN)
    assert v["reachable"] is False


def test_verify_mcp_rejects_foreign_server():
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL,
                     transport=FakeMochletMcp(token=TOKEN, server_name="some-other-mcp").transport,
                     token=TOKEN)
    assert v["authenticated"] is True and v["is_mochlet"] is False


def test_verify_mcp_requires_control_and_discovery_tools():
    # sendChat+cancelJob present but runJob/continueSession/listProjects missing.
    fake = FakeMochletMcp(token=TOKEN, tools=["health", "sendChat", "cancelJob"])
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL, transport=fake.transport, token=TOKEN)
    assert v["has_required_tools"] is False


def test_verify_mcp_accepts_live_result_envelope():
    # REGRESSION: the LIVE Mochlet MCP returns listProjects structuredContent as
    # ``{"result": [<project>...]}`` (not ``{"projects": [...]}``). verify_mcp must
    # normalize it and surface the real project — else it wrongly reports needs_project.
    fake = FakeMochletMcp(token=TOKEN, projects_envelope_key="result")
    v = C.verify_mcp(C.DEFAULT_MOCHLET_URL, transport=fake.transport, token=TOKEN)
    assert v["projects_listed"] is True
    assert [p["id"] for p in v["projects"]] == [PID]


# --------------------------------------------------------------------------- #
# Connection status
# --------------------------------------------------------------------------- #
def test_status_needs_setup_when_mochlet_down(tmp_path):
    s = C.connection_status(env={}, base_dir=tmp_path, transport=_down_transport,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "needs_setup" and s["available"] is False


def test_status_detected_when_mochlet_up_unconfigured(tmp_path):
    s = C.connection_status(env={}, base_dir=tmp_path,
                            transport=FakeMochletMcp(token=TOKEN).transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "detected" and s["available"] is False
    assert s["suggested_endpoint"] == C.DEFAULT_MOCHLET_URL


def test_status_connected_only_after_capability_and_project(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path="/repo/x", base_dir=tmp_path)
    s = C.connection_status(env={}, base_dir=tmp_path,
                            transport=FakeMochletMcp(token=TOKEN).transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "connected" and s["available"] is True
    assert s["project"] == PID


def test_status_needs_project_when_capable_but_unselected(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=None,
                      project_path=None, base_dir=tmp_path)
    fake = FakeMochletMcp(token=TOKEN, projects=[
        {"id": PID, "name": "a", "path": "/a"}, {"id": "22222222-2222-4222-8222-222222222222", "name": "b", "path": "/b"}])
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "needs_project" and s["available"] is False
    assert len(s["projects"]) == 2


def test_status_connected_with_live_result_envelope(tmp_path):
    # REGRESSION: with the real ``{"result": [...]}`` listProjects envelope and the
    # OpenMontage project persisted, status must be "connected" (not "needs_project").
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path="/repo/x", base_dir=tmp_path)
    fake = FakeMochletMcp(token=TOKEN, projects_envelope_key="result")
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "connected" and s["available"] is True
    assert s["project"] == PID


def test_connect_persists_with_live_result_envelope(tmp_path):
    # REGRESSION: guided connect must resolve+persist the project from the live
    # ``{"result": [...]}`` envelope, enabling production.
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="tok-123", projects_envelope_key="result")
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="tok-123", project_id=PID,
                  base_dir=tmp_path, transport=fake.transport,
                  secret_setter=secrets.setter, secret_getter=secrets.getter, env={})
    assert s["status"] == "connected" and s["available"] is True
    assert C.stored_config(tmp_path)["mochlet_project_id"] == PID


def test_status_tools_disabled(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    fake = FakeMochletMcp(token=TOKEN, tools=["health", "listProjects"])
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "tools_disabled" and s["available"] is False


def test_status_wrong_server_not_connected(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    fake = FakeMochletMcp(token=TOKEN, server_name="totally-different-mcp")
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "wrong_server" and s["available"] is False


def test_status_fail_closed_when_projects_cannot_be_listed(tmp_path):
    # A stale persisted project must NOT pass when listProjects fails (unverifiable).
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    fake = FakeMochletMcp(token=TOKEN, projects_error=True)
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "needs_project" and s["available"] is False


def test_connect_rejects_foreign_server(tmp_path):
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="t", server_name="evil-mcp")
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="t", project_id=PID, base_dir=tmp_path,
                  transport=fake.transport, secret_setter=secrets.setter,
                  secret_getter=secrets.getter, env={})
    assert s["status"] == "wrong_server" and s["available"] is False
    assert C.stored_config(tmp_path) == {}


def test_status_degraded_when_health_reports_unhealthy(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    fake = FakeMochletMcp(token=TOKEN, health_ok=False)  # tools present but unhealthy
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: TOKEN}).getter)
    assert s["status"] == "degraded" and s["available"] is False


def test_status_needs_token_when_configured_but_unauthorized(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    fake = FakeMochletMcp(token="right")
    s = C.connection_status(env={}, base_dir=tmp_path, transport=fake.transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: "wrong"}).getter)
    assert s["status"] == "needs_token"


def test_status_never_returns_token(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    s = C.connection_status(env={}, base_dir=tmp_path, transport=FakeMochletMcp(token=TOKEN).transport,
                            secret_getter=_MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: "super-secret-value"}).getter)
    assert "super-secret-value" not in str(s)


# --------------------------------------------------------------------------- #
# Guided connect
# --------------------------------------------------------------------------- #
def test_connect_success_persists_project_and_enables(tmp_path):
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="tok-123")
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="tok-123", project_id=PID,
                  base_dir=tmp_path, transport=fake.transport,
                  secret_setter=secrets.setter, secret_getter=secrets.getter, env={})
    assert s["status"] == "connected" and s["available"] is True
    assert secrets.store[ORCHESTRATOR_TOKEN_ACCOUNT] == "tok-123"
    cfg = C.stored_config(tmp_path)
    assert cfg["mochlet_project_id"] == PID and cfg["endpoint_kind"] == "mcp"
    # the live client the adapter builds is the MCP orchestrator, and it's available
    client = C.build_live_client(env={}, base_dir=tmp_path, transport=fake.transport)
    assert isinstance(client, MochletMcpOrchestratorClient)
    assert client.available() is True


def test_connect_single_project_auto_selected(tmp_path):
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="t")  # one default project
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="t", base_dir=tmp_path,
                  transport=fake.transport, secret_setter=secrets.setter,
                  secret_getter=secrets.getter, env={})
    assert s["status"] == "connected"
    assert C.stored_config(tmp_path)["mochlet_project_id"] == PID


def test_connect_ambiguous_projects_needs_choice(tmp_path):
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="t", projects=[
        {"id": PID, "name": "a", "path": "/a"},
        {"id": "22222222-2222-4222-8222-222222222222", "name": "b", "path": "/b"}])
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="t", base_dir=tmp_path,
                  transport=fake.transport, secret_setter=secrets.setter,
                  secret_getter=secrets.getter, env={})
    assert s["status"] == "needs_project" and s["available"] is False
    assert len(s["projects"]) == 2
    assert C.stored_config(tmp_path) == {}  # nothing persisted until a project is chosen


def test_connect_needs_token(tmp_path):
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="right")
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="wrong", base_dir=tmp_path,
                  transport=fake.transport, secret_setter=secrets.setter,
                  secret_getter=secrets.getter, env={})
    assert s["status"] == "needs_token" and s["available"] is False
    assert C.stored_config(tmp_path) == {}


def test_connect_tools_disabled(tmp_path):
    secrets = _MemSecrets()
    fake = FakeMochletMcp(token="t", tools=["health", "listProjects"])
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="t", project_id=PID, base_dir=tmp_path,
                  transport=fake.transport, secret_setter=secrets.setter,
                  secret_getter=secrets.getter, env={})
    assert s["status"] == "tools_disabled" and s["available"] is False


def test_connect_unreachable_does_not_persist(tmp_path):
    secrets = _MemSecrets()
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="t", base_dir=tmp_path,
                  transport=_down_transport, secret_setter=secrets.setter,
                  secret_getter=secrets.getter, env={})
    assert s["status"] == "unreachable" and s["available"] is False
    assert C.stored_config(tmp_path) == {}


def test_connect_rejects_bad_endpoint(tmp_path):
    with pytest.raises(C.ConnectionError):
        C.connect(url="http://evil.example.com/mcp", base_dir=tmp_path,
                  transport=FakeMochletMcp().transport, secret_setter=_MemSecrets().setter, env={})


def test_connect_empty_token_rejected(tmp_path):
    with pytest.raises(C.ConnectionError):
        C.connect(url=C.DEFAULT_MOCHLET_URL, token="   ", base_dir=tmp_path,
                  transport=FakeMochletMcp().transport, secret_setter=_MemSecrets().setter, env={})


# --------------------------------------------------------------------------- #
# Endpoint resolution + disconnect
# --------------------------------------------------------------------------- #
def test_env_var_takes_precedence(tmp_path):
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    ep = C.configured_endpoint(env={"OPENMONTAGE_HERMES_ORCHESTRATOR_URL": "https://hermes.example/mcp"},
                               base_dir=tmp_path)
    assert ep == "https://hermes.example/mcp"


def test_unconfigured_endpoint_is_none(tmp_path):
    assert C.configured_endpoint(env={}, base_dir=tmp_path) is None
    client = C.build_live_client(env={}, base_dir=tmp_path)
    assert client.available() is False  # fail-closed


def test_disconnect_forgets_config(tmp_path):
    secrets = _MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: "x"})
    C._persist_config(endpoint=C.DEFAULT_MOCHLET_URL, kind="mcp", project_id=PID,
                      project_path=None, base_dir=tmp_path)
    C.disconnect(base_dir=tmp_path, secret_deleter=secrets.deleter, wipe_token=True)
    assert C.stored_config(tmp_path) == {}
    assert secrets.getter(ORCHESTRATOR_TOKEN_ACCOUNT) is None


# --------------------------------------------------------------------------- #
# Legacy REST endpoint still supported when explicitly configured
# --------------------------------------------------------------------------- #
def test_rest_endpoint_uses_health_probe(tmp_path):
    C._persist_config(endpoint="http://127.0.0.1:9999", kind="rest", project_id=None,
                      project_path=None, base_dir=tmp_path)

    def rest_transport(url, *, timeout, headers=None):
        class R:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return {"service": "custom"}
        assert url.endswith("/health")
        return R()

    s = C.connection_status(env={}, base_dir=tmp_path, transport=rest_transport,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "connected" and s["endpoint_kind"] == "rest"
