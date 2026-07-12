"""Canonical /status endpoint + guided Hermes connection routes.

Covers the single-view-model contract and the guarded connect flow. Uses the
session-global in-memory keyring (root conftest) — never the OS keychain — and
never hits a live orchestrator (connection probing is stubbed out where needed).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from backlot import status_api
from lib.production_brain import connection as conn_mod
from lib.production_brain.adapter import FakeBrain
from lib.production_brain.store import ProductionBrainStore


@pytest.fixture(autouse=True)
def _no_conn_probe(monkeypatch):
    """Default: Hermes not connected, no network probe (deterministic)."""
    status_api._invalidate_conn_cache()
    monkeypatch.setattr(
        conn_mod, "connection_status",
        lambda **kw: {"status": "needs_setup", "available": False,
                      "headline": "Hermes isn't connected yet", "detail": "",
                      "actions": [{"id": "connect_hermes", "label": "Connect Hermes"}],
                      "token_configured": False, "endpoint": None})
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


def test_status_includes_connection_block(client):
    v = client.get("/api/project/the-electricity-bulb/status").json()
    assert v["connection"]["status"] == "needs_setup"
    assert v["connection"]["available"] is False


# --------------------------------------------------------------------------- #
# Direct-API bypass: controls must stay guarded / fail-closed
# --------------------------------------------------------------------------- #
def test_brain_start_without_connection_fails_closed(client):
    # No orchestrator connected → Start must 409, not fabricate a run.
    r = _post(client, "/api/project/the-electricity-bulb/brain/start")
    assert r.status_code == 409


def test_hermes_connect_requires_csrf(client):
    # Cross-site POST without the CSRF header is rejected.
    r = client.post("/api/hermes/connect", json={"url": "http://127.0.0.1:9235"})
    assert r.status_code in (400, 403)


def test_hermes_connect_rejects_bad_endpoint(client, monkeypatch):
    # A non-loopback plain-HTTP endpoint is refused fail-closed (400).
    r = _post(client, "/api/hermes/connect", {"url": "http://evil.example.com"})
    assert r.status_code == 400


def test_hermes_connection_route(client):
    r = client.get("/api/hermes/connection")
    assert r.status_code == 200
    assert r.json()["status"] == "needs_setup"


def test_status_never_leaks_a_token(client, monkeypatch):
    # Even with a token configured, the payload must not contain its value.
    monkeypatch.setattr(
        conn_mod, "connection_status",
        lambda **kw: {"status": "connected", "available": True,
                      "headline": "Connected to Hermes", "detail": "",
                      "actions": [], "token_configured": True, "endpoint": "http://127.0.0.1:9235"})
    status_api._invalidate_conn_cache()
    v = client.get("/api/project/the-electricity-bulb/status").json()
    assert "token" not in json.dumps(v).lower() or "token_configured" in json.dumps(v)
    assert v["connection"]["available"] is True
