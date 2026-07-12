"""POST /api/providers/runtime — guarded, allowlisted runtime maintenance."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from lib import runtime_actions

_OK_DOCTOR = {"available": True, "installed": True, "reason": "", "checks": {}}


@pytest.fixture
def client(monkeypatch):
    async def no_watch():
        return None
    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


def _post(client, body):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post("/api/providers/runtime", json=body,
                       headers={"X-OpenMontage-CSRF": token})


def test_verify_returns_doctor(client, monkeypatch):
    monkeypatch.setattr(runtime_actions, "_doctor", lambda **k: dict(_OK_DOCTOR))
    r = _post(client, {"runtime": "remotion", "action": "verify"})
    assert r.status_code == 200
    assert r.json()["doctor"]["available"] is True
    assert r.json()["action"] == "verify"


def test_unknown_runtime_rejected_400(client):
    r = _post(client, {"runtime": "ffmpeg", "action": "verify"})
    assert r.status_code == 400


def test_unknown_action_rejected_400(client):
    r = _post(client, {"runtime": "remotion", "action": "nuke"})
    assert r.status_code == 400


def test_direct_post_without_csrf_403(client):
    r = client.post("/api/providers/runtime", json={"runtime": "remotion", "action": "verify"})
    assert r.status_code == 403


def test_runtime_endpoint_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(runtime_actions, "_doctor", lambda **k: dict(_OK_DOCTOR))
    limit, _ = server_mod._RATE_LIMITS["runtime"]
    for _ in range(limit):
        assert _post(client, {"runtime": "remotion", "action": "verify"}).status_code == 200
    r = _post(client, {"runtime": "remotion", "action": "verify"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
