"""Deterministic integration smoke for the orchestration port.

Proves end-to-end, with NO paid services:
  * a fake orchestrator client returns canonical ids and RECEIVES start (create)
    + cancel through the Backlot API;
  * production mode (unconfigured Configured client) REFUSES to open a run —
    fail-closed with an actionable 409 — instead of fabricating ids.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import backlot.brain_api as brain_api
from backlot import server as server_mod
from lib.production_brain.adapter import HermesBrainAdapter
from lib.production_brain.orchestrator import (
    ConfiguredHermesOrchestratorClient,
    FakeOrchestratorClient,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "intake.json").write_text(json.dumps({
        "version": "1.0", "project_id": "demo", "title": "Demo",
        "pipeline_type": "animation", "target_duration_seconds": 120}))
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", tmp_path)

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    return proj


def _post(client, url, body=None):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=body or {}, headers={"X-OpenMontage-CSRF": token})


def test_fake_orchestrator_receives_start_and_cancel(project, monkeypatch):
    fake = FakeOrchestratorClient(engine="hermes-fake")
    # default_adapter returns an adapter bound to the SHARED fake client so we can
    # assert the create/cancel it receives.
    monkeypatch.setattr(brain_api, "default_adapter",
                        lambda: HermesBrainAdapter(client=fake))

    with TestClient(server_mod.create_app()) as c:
        # START → the fake orchestrator created exactly one canonical job.
        r = _post(c, "/api/project/demo/brain/start", {})
        assert r.status_code == 200
        st = r.json()
        rid = st["run_id"]
        assert st["state"] == "running"
        assert len(fake.created) == 1
        handle = fake.created[f"demo:{rid}"]
        assert st["brain"]["session_id"] == handle.session_id
        assert st["brain"]["job_id"] == handle.job_id
        assert st["brain"]["external"] is True

        # Idempotent: a second start does NOT create a second external job.
        r2 = _post(c, "/api/project/demo/brain/start", {})
        assert r2.json().get("already_active") is True
        assert len(fake.created) == 1

        # CANCEL through the API (inject the shared fake client) → the external
        # job is cancelled, correlating with the returned handle.
        state = brain_api.cancel_run(project, {"run_id": rid}, orchestrator=fake)
        assert state["state"] == "cancelled"
        assert fake.cancelled == [handle.job_id]


def test_production_mode_refuses_without_external_ids(project, monkeypatch):
    # Unconfigured production client → unavailable → the API fails closed (409),
    # never fabricating a run.
    monkeypatch.delenv("OPENMONTAGE_HERMES_ORCHESTRATOR_URL", raising=False)
    monkeypatch.setattr(
        brain_api, "default_adapter",
        lambda: HermesBrainAdapter(client=ConfiguredHermesOrchestratorClient(url=None)))
    with TestClient(server_mod.create_app()) as c:
        r = _post(c, "/api/project/demo/brain/start", {})
        assert r.status_code == 409
        assert "orchestrator" in r.json()["detail"].lower()
        assert c.get("/api/project/demo/brain").json()["state"] == "not_started"
