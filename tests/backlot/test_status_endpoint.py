"""Canonical /status endpoint — the read-only project OVERVIEW.

OpenMontage is manual-first: there is NO autonomous production worker, NO agent
connection, and NO production-run automation surface. This pins:

  * GET /api/project/{id}/status returns the read-only overview (headline,
    target, single ``open_studio`` action, NO ``connection`` block).
  * The removed agent/brain automation routes now 404.
  * The learned-Style preferences routes still work (they moved to
    ``backlot.preferences_api``).

Uses the session-global in-memory keyring (root conftest) — never the OS keychain.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod


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
# /status shape — read-only overview
# --------------------------------------------------------------------------- #
def test_status_returns_project_overview(client):
    r = client.get("/api/project/the-electricity-bulb/status")
    assert r.status_code == 200
    v = r.json()
    assert v["kind"] == "project_overview"
    assert v["version"] == "2.0"
    assert v["project_id"] == "the-electricity-bulb"
    assert v["title"] == "The Electricity Bulb"
    assert v["owner"] == "you"
    assert v["primary_action"]["id"] == "open_studio"
    assert isinstance(v["headline"], str) and v["headline"]


def test_status_has_no_connection_or_automation_surface(client):
    v = client.get("/api/project/the-electricity-bulb/status").json()
    for forbidden in ("connection", "overall_state", "identity", "is_live",
                      "stop_available"):
        assert forbidden not in v
    blob = json.dumps(v).lower()
    assert "mochlet" not in blob
    assert "hermes" not in blob


def test_status_target_uses_intake_requested_duration(client):
    # intake target_duration_seconds=150, no timeline yet → target 2:30 / 4500.
    v = client.get("/api/project/the-electricity-bulb/status").json()
    t = v["target"]
    assert t["available"] is True
    assert t["is_target"] is True
    assert t["formatted"] == "2:30"
    assert t["frames"] == 4500


def test_status_render_not_renderable_without_layers(client):
    v = client.get("/api/project/the-electricity-bulb/status").json()
    assert v["render"]["renderable"] is False
    assert v["render"]["reason"]
    assert v["render"]["active"] is False


def test_status_demo_flag_only_when_requested(client):
    plain = client.get("/api/project/the-electricity-bulb/status").json()
    assert plain["is_demo"] is False
    demo = client.get("/api/project/the-electricity-bulb/status?demo=1").json()
    assert demo["is_demo"] is True


def test_status_stale_flag_adds_diagnostic(client):
    stale = client.get("/api/project/the-electricity-bulb/status?stale=1").json()
    assert stale["stale"] is True
    assert any(d["kind"] == "stale" for d in stale["diagnostics"])


# --------------------------------------------------------------------------- #
# The removed agent / brain automation routes are GONE (404)
# --------------------------------------------------------------------------- #
def test_legacy_agent_routes_are_gone(client):
    assert client.get("/api/agent/connection").status_code == 404
    assert _post(client, "/api/agent/connect", {}).status_code == 404
    assert _post(client, "/api/agent/disconnect", {}).status_code == 404


def test_legacy_brain_routes_are_gone(client):
    assert client.get("/api/project/the-electricity-bulb/brain").status_code == 404
    assert _post(client, "/api/project/the-electricity-bulb/brain/start", {}).status_code == 404
    assert _post(client, "/api/project/the-electricity-bulb/brain/cancel", {}).status_code == 404


# --------------------------------------------------------------------------- #
# Learned-Style preferences routes still work (backlot.preferences_api)
# --------------------------------------------------------------------------- #
def test_project_preferences_get(client):
    r = client.get("/api/project/the-electricity-bulb/preferences")
    assert r.status_code == 200
    body = r.json()
    assert "categories" in body
    assert body["project"]["opted_out"] is False
    assert body["project"]["preferences"] == []


def test_global_preferences_get(client):
    r = client.get("/api/preferences")
    assert r.status_code == 200
    body = r.json()
    assert "categories" in body
    assert "global" in body


def test_project_preferences_opt_out_round_trip(client):
    r = _post(client, "/api/project/the-electricity-bulb/preferences",
              {"action": "opt_out", "scope": "project", "opted_out": True})
    assert r.status_code == 200
    after = client.get("/api/project/the-electricity-bulb/preferences").json()
    assert after["project"]["opted_out"] is True


def test_preferences_post_requires_csrf(client):
    # Cross-site POST without the CSRF header is rejected.
    r = client.post("/api/project/the-electricity-bulb/preferences",
                    json={"action": "opt_out", "scope": "project"})
    assert r.status_code in (400, 403)
