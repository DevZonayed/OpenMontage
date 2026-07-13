"""Canonical /status endpoint + native Hermes Agent connection routes.

Covers the single-view-model contract and the guarded connect flow against the
NATIVE Hermes Agent surface (Mochlet is gone). The connection block is the
``agent_status`` shape — {status, available, headline, ...} with NO endpoint,
token, project, or job. Connection probing is stubbed (a fake ``agent_status`` /
``connect`` / ``disconnect``) so nothing spawns a real process or touches
``~/.hermes``. The legacy ``/api/hermes/*`` routes must be 404.

Uses the session-global in-memory keyring (root conftest) — never the OS keychain.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from backlot import status_api
from lib.production_brain import hermes_agent as HA
from lib.production_brain.adapter import FakeBrain
from lib.production_brain.store import ProductionBrainStore

# The native "not connected" connection block the board renders when the agent is
# not installed on this machine (no endpoint/token/project — ever).
_NOT_INSTALLED = {
    "kind": "hermes_agent", "status": "not_installed", "available": False,
    "server_name": "Hermes Agent", "headline": "Hermes Agent is not installed",
    "detail": "", "actions": [{"id": "retry_detect", "label": "Re-check for Hermes"}],
    "enabled": False, "installed": False, "ready": False, "version": None,
}


@pytest.fixture(autouse=True)
def _no_conn_probe(monkeypatch):
    """Default: Hermes Agent not installed, no subprocess probe (deterministic)."""
    status_api._invalidate_conn_cache()
    monkeypatch.setattr(HA, "agent_status", lambda **kw: dict(_NOT_INSTALLED))
    yield
    status_api._invalidate_conn_cache()


@pytest.fixture
def client(tmp_path, monkeypatch):
    proj = tmp_path / "the-electricity-bulb"
    proj.mkdir()
    (proj / "intake.json").write_text(json.dumps({
        "version": "1.0", "project_id": "the-electricity-bulb", "title": "The Electricity Bulb",
        "pipeline_type": "animation", "target_duration_seconds": 150}))
    (proj / "project.json").write_text(json.dumps({
        "project_id": "the-electricity-bulb", "title": "The Electricity Bulb",
        "pipeline_type": "animation"}))
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", tmp_path)

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        c._proj = proj  # type: ignore[attr-defined]
        yield c


def _post(client, url, body=None):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=body or {}, headers={"X-OpenMontage-CSRF": token})


# --------------------------------------------------------------------------- #
# /status shape + reconciliation
# --------------------------------------------------------------------------- #
def test_status_new_project_not_started(client):
    r = client.get("/api/project/the-electricity-bulb/status")
    assert r.status_code == 200
    v = r.json()
    assert v["kind"] == "production_status_view"
    assert v["stage_count"] == 11
    assert len(v["stages"]) == 11
    assert v["overall_state"] == "not_started"
    assert isinstance(v["primary_action"], dict) and v["primary_action"]["id"]
    assert v["stop_available"] is False


def test_status_reflects_live_brain_run(client):
    store = ProductionBrainStore(client._proj)
    FakeBrain().drive(store, requested_duration_seconds=150, run_id="run1",
                      approver=None)  # stop at first approval gate
    v = client.get("/api/project/the-electricity-bulb/status").json()
    # fake driver → fixture mode, never "live"
    assert v["is_live"] is False
    assert v["overall_state"] in ("awaiting_approval", "producing")
    assert v["run_id"] == "run1"


def test_status_render_not_renderable_without_layers(client):
    v = client.get("/api/project/the-electricity-bulb/status").json()
    assert v["render"]["renderable"] is False
    assert v["render"]["reason"]


def test_status_demo_flag_only_when_requested(client):
    store = ProductionBrainStore(client._proj)
    FakeBrain().drive(store, requested_duration_seconds=150, run_id="run1", approver=None)
    plain = client.get("/api/project/the-electricity-bulb/status").json()
    assert plain["is_demo"] is False
    demo = client.get("/api/project/the-electricity-bulb/status?demo=1").json()
    assert demo["is_demo"] is True


def test_status_includes_native_connection_block(client):
    v = client.get("/api/project/the-electricity-bulb/status").json()
    conn = v["connection"]
    assert conn["kind"] == "hermes_agent"
    assert conn["status"] == "not_installed"
    assert conn["available"] is False


def test_connection_block_never_contains_endpoint_token_or_project(client):
    v = client.get("/api/project/the-electricity-bulb/status").json()
    conn = v["connection"]
    for forbidden in ("endpoint", "token", "project", "projects", "job", "url"):
        assert forbidden not in conn
    blob = json.dumps(v).lower()
    assert "endpoint" not in blob and "mochlet" not in blob


# --------------------------------------------------------------------------- #
# Direct-API bypass: controls must stay guarded / fail-closed
# --------------------------------------------------------------------------- #
def test_brain_start_without_connection_fails_closed(client):
    # No Hermes Agent connected → Start must 409, not fabricate a run.
    r = _post(client, "/api/project/the-electricity-bulb/brain/start")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Native agent connection routes (auto-detected, no credentials)
# --------------------------------------------------------------------------- #
def test_agent_connection_route(client):
    r = client.get("/api/agent/connection")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "hermes_agent"
    assert body["status"] == "not_installed"
    assert body["available"] is False


def test_agent_connect_requires_csrf(client):
    # Cross-site POST without the CSRF header is rejected.
    r = client.post("/api/agent/connect", json={})
    assert r.status_code in (400, 403)


def test_agent_connect_fail_closed_when_not_ready(client, monkeypatch):
    # A not-ready verify does NOT enable — connect returns an unavailable view.
    monkeypatch.setattr(HA, "connect", lambda **kw: dict(_NOT_INSTALLED))
    r = _post(client, "/api/agent/connect", {})
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_agent_connect_and_disconnect_round_trip(client, monkeypatch):
    connected = {
        "kind": "hermes_agent", "status": "connected", "available": True,
        "server_name": "Hermes Agent", "headline": "Hermes Agent connected",
        "detail": "", "actions": [{"id": "disconnect_agent", "label": "Disconnect"}],
        "enabled": True, "installed": True, "ready": True, "version": "1.2.3",
    }
    monkeypatch.setattr(HA, "connect", lambda **kw: dict(connected))
    monkeypatch.setattr(HA, "disconnect", lambda **kw: dict(_NOT_INSTALLED))
    r = _post(client, "/api/agent/connect", {})
    assert r.status_code == 200 and r.json()["available"] is True
    # And the connected view still carries no endpoint/token/project.
    for forbidden in ("endpoint", "token", "project", "url"):
        assert forbidden not in r.json()
    r2 = _post(client, "/api/agent/disconnect", {})
    assert r2.status_code == 200 and r2.json()["available"] is False


# --------------------------------------------------------------------------- #
# The legacy Mochlet /api/hermes/* routes are GONE
# --------------------------------------------------------------------------- #
def test_legacy_hermes_routes_are_gone(client):
    assert client.get("/api/hermes/connection").status_code == 404
    assert _post(client, "/api/hermes/connect", {"url": "http://127.0.0.1:9235"}).status_code == 404
    assert _post(client, "/api/hermes/disconnect", {}).status_code == 404
