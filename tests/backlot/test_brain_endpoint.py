"""Production-brain Backlot API: read (state/events/assets), control (approve/
reject/cancel/retry/resume), and learned preferences (read/update/reset).

Covers the security + robustness contract shared by every mutation:
  * CSRF token required; cross-origin rejected; malformed/oversize body handled;
  * exact-run-id validation (409 on mismatch), missing fields (400);
  * cursor pagination on the event history;
  * rate-limit bucket enforcement;
  * learning provenance (explicit source only), correction, opt-out, reset;
  * secret redaction end-to-end (a leaked provider key must not reach the wire).

Uses the session-global in-memory keyring (root conftest) — never the OS keychain.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from lib.production_brain import learning as learn_mod
from lib.production_brain.adapter import FakeBrain
from lib.production_brain.store import ProductionBrainStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "intake.json").write_text(json.dumps({
        "version": "1.0", "project_id": "demo", "title": "Demo",
        "pipeline_type": "animation", "target_duration_seconds": 120}))
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", tmp_path)
    # Never touch the real global style store.
    monkeypatch.setattr(learn_mod, "GLOBAL_STORE_PATH", tmp_path / "_global_style.json")

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        c._proj = proj  # type: ignore[attr-defined]
        yield c


def _post(client, url, body=None):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=body or {}, headers={"X-OpenMontage-CSRF": token})


def _seed(client, *, approver=None, run_id="run_demo", secs=120):
    """Drive a fake brain against the project store to populate telemetry."""
    store = ProductionBrainStore(client._proj)
    FakeBrain().drive(store, requested_duration_seconds=secs, run_id=run_id, approver=approver)
    return store


class TestReads:
    def test_state_reflects_seeded_run(self, client):
        _seed(client)  # stops at proposal approval gate
        st = client.get("/api/project/demo/brain").json()
        assert st["state"] == "awaiting_approval"
        assert st["requested_duration_seconds"] == 120
        assert st["brain"]["adapter"] == "fake"
        assert st["current_stage"] == "proposal"

    def test_events_cursor_pagination(self, client):
        _seed(client)
        page1 = client.get("/api/project/demo/brain/events?after=0&limit=4").json()
        assert page1["count"] == 4 and page1["has_more"] is True
        page2 = client.get(f"/api/project/demo/brain/events?after={page1['next_cursor']}&limit=100").json()
        assert page2["events"][0]["seq"] == page1["next_cursor"] + 1
        # No overlap between pages.
        assert page1["events"][-1]["seq"] < page2["events"][0]["seq"]

    def test_assets_lists_outputs(self, client):
        _seed(client)
        a = client.get("/api/project/demo/brain/assets").json()
        assert a["count"] >= 1
        assert all("path" in o for o in a["outputs"])

    def test_unknown_project_404(self, client):
        assert client.get("/api/project/ghost/brain").status_code == 404

    def test_not_started_project_is_honest(self, client):
        # project exists but no run seeded
        st = client.get("/api/project/demo/brain").json()
        assert st["state"] == "not_started"


class TestControlSecurity:
    def test_approve_requires_csrf(self, client):
        _seed(client)
        r = client.post("/api/project/demo/brain/approve", json={"run_id": "run_demo"})
        assert r.status_code == 403

    def test_cross_origin_rejected(self, client):
        _seed(client)
        token = client.get("/api/csrf").json()["csrf"]
        r = client.post("/api/project/demo/brain/approve",
                        json={"run_id": "run_demo"},
                        headers={"X-OpenMontage-CSRF": token, "Origin": "http://evil.example",
                                 "Host": "testserver"})
        assert r.status_code == 403

    def test_missing_run_id_400(self, client):
        _seed(client)
        r = _post(client, "/api/project/demo/brain/approve", {})
        assert r.status_code == 400

    def test_wrong_run_id_409(self, client):
        _seed(client)
        r = _post(client, "/api/project/demo/brain/approve", {"run_id": "run_bogus"})
        assert r.status_code == 409

    def test_malformed_json_400(self, client):
        token = client.get("/api/csrf").json()["csrf"]
        r = client.post("/api/project/demo/brain/approve", content=b"{not json",
                        headers={"X-OpenMontage-CSRF": token, "Content-Type": "application/json"})
        assert r.status_code == 400


class TestControlFlow:
    def test_approve_then_running(self, client):
        _seed(client)
        r = _post(client, "/api/project/demo/brain/approve", {"run_id": "run_demo", "stage": "proposal"})
        assert r.status_code == 200 and r.json()["state"] == "running"

    def test_reject_fails_the_gate_stage(self, client):
        _seed(client)
        r = _post(client, "/api/project/demo/brain/reject", {"run_id": "run_demo", "stage": "proposal"})
        assert r.status_code == 200
        stages = {s["id"]: s for s in r.json()["stages"]}
        assert stages["proposal"]["status"] == "failed"

    def test_cancel_exact_id(self, client):
        _seed(client)
        r = _post(client, "/api/project/demo/brain/cancel", {"run_id": "run_demo"})
        assert r.status_code == 200 and r.json()["state"] == "cancelled"

    def test_retry_stage(self, client):
        store = _seed(client, approver="auto", run_id="run_done")
        # completed run -> retry has no active run -> 409
        r = _post(client, "/api/project/demo/brain/retry", {"stage": "render"})
        assert r.status_code == 409

    def test_retry_on_active_run(self, client):
        store = ProductionBrainStore(client._proj, gen_id=lambda: "run_live")
        FakeBrain().drive(store, requested_duration_seconds=60, run_id="run_live", stop_after="assets")
        store.fail_stage("render", error="boom")
        r = _post(client, "/api/project/demo/brain/retry", {"stage": "render", "run_id": "run_live"})
        assert r.status_code == 200
        stages = {s["id"]: s for s in r.json()["stages"]}
        assert stages["render"]["status"] == "active"

    def test_resume_recomputes(self, client):
        store = ProductionBrainStore(client._proj, gen_id=lambda: "run_live")
        FakeBrain().drive(store, requested_duration_seconds=60, run_id="run_live", stop_after="assets")
        r = _post(client, "/api/project/demo/brain/resume", {})
        assert r.status_code == 200 and r.json()["state"] == "running"


class TestStartFailClosed:
    def test_start_fails_closed_when_brain_unavailable(self, client, monkeypatch):
        # The default adapter probes engines; force it unavailable.
        import backlot.brain_api as brain_api
        from lib.production_brain.adapter import HermesBrainAdapter

        monkeypatch.setattr(
            brain_api, "default_adapter",
            lambda: HermesBrainAdapter(probe=lambda: {"available": False, "engine": None}))
        r = _post(client, "/api/project/demo/brain/start", {})
        assert r.status_code == 409
        # No run opened.
        assert client.get("/api/project/demo/brain").json()["state"] == "not_started"

    def test_start_opens_run_when_available(self, client, monkeypatch):
        import backlot.brain_api as brain_api
        from lib.production_brain.adapter import HermesBrainAdapter

        monkeypatch.setattr(
            brain_api, "default_adapter",
            lambda: HermesBrainAdapter(probe=lambda: {"available": True, "engine": "claude"}))
        r = _post(client, "/api/project/demo/brain/start", {})
        assert r.status_code == 200 and r.json()["state"] == "running"
        assert r.json()["requested_duration_seconds"] == 120


class TestRateLimit:
    def test_brain_bucket_enforced(self, client, monkeypatch):
        _seed(client)
        state = {"t": 1000.0}
        monkeypatch.setattr(server_mod, "_rate_now", lambda: state["t"])
        limit, _window = server_mod._RATE_LIMITS["brain"]
        token = client.get("/api/csrf").json()["csrf"]
        hdr = {"X-OpenMontage-CSRF": token}
        # Cancel is idempotent-ish here; use a harmless wrong-id approve to burn budget.
        last = None
        for _ in range(limit + 2):
            last = client.post("/api/project/demo/brain/approve", json={"run_id": "x"}, headers=hdr)
        assert last.status_code == 429
        assert "retry-after" in {k.lower() for k in last.headers}


class TestPreferencesApi:
    def test_learn_and_read_project_scope(self, client):
        r = _post(client, "/api/project/demo/preferences", {
            "action": "learn", "scope": "project", "category": "pacing",
            "key": "cuts_per_min", "value": 18, "source": "approval", "confidence": 0.8})
        assert r.status_code == 200
        prefs = client.get("/api/project/demo/preferences?scope=all").json()
        assert len(prefs["project"]["preferences"]) == 1
        assert prefs["project"]["preferences"][0]["provenance"]["source"] == "approval"

    def test_opaque_source_rejected(self, client):
        r = _post(client, "/api/project/demo/preferences", {
            "action": "learn", "scope": "global", "category": "pacing",
            "key": "x", "value": 1, "source": "profiling"})
        assert r.status_code == 400

    def test_correct_and_reject_and_reset(self, client):
        _post(client, "/api/preferences", {
            "action": "learn", "scope": "global", "category": "typography",
            "key": "font", "value": "Inter", "source": "approval"})
        g = client.get("/api/preferences").json()
        pid = g["global"]["preferences"][0]["pref_id"]
        r = _post(client, "/api/preferences", {"action": "correct", "scope": "global",
                                               "pref_id": pid, "value": "Fraunces"})
        assert r.status_code == 200
        applied = [p for p in client.get("/api/preferences").json()["global"]["preferences"]
                   if p["status"] == "applied"]
        assert applied and applied[0]["value"] == "Fraunces"
        # reset wipes
        _post(client, "/api/preferences", {"action": "opt_out", "scope": "global", "opted_out": True, "wipe": True})
        g = client.get("/api/preferences").json()
        assert g["global"]["opted_out"] is True and g["global"]["preferences"] == []

    def test_reset_endpoint(self, client):
        _post(client, "/api/project/demo/preferences", {
            "action": "learn", "scope": "project", "category": "music",
            "key": "genre", "value": "ambient", "source": "approval"})
        r = _post(client, "/api/project/demo/preferences/reset", {"scope": "project"})
        assert r.status_code == 200
        prefs = client.get("/api/project/demo/preferences?scope=project").json()
        assert prefs["project"]["preferences"] == []


class TestRedactionOverWire:
    def test_leaked_secret_never_reaches_events_endpoint(self, client):
        store = ProductionBrainStore(client._proj, gen_id=lambda: "run_r")
        store.start(brain={"name": "hermes", "adapter": "fake", "available": True},
                    requested_duration_seconds=60)
        store.enter_stage("assets")
        store.event("provider_call", stage="assets", provider="openai",
                    data={"api_key": "sk-SHOULDNEVERAPPEAR0123456789"})
        body = client.get("/api/project/demo/brain/events").text
        assert "sk-SHOULDNEVERAPPEAR" not in body
        assert "[redacted]" in body
