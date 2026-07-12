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


# --------------------------------------------------------------------------- #
# Truthful cancellation + real retry/resume control (B4 / B5)
# --------------------------------------------------------------------------- #
from lib.production_brain.adapter import HermesBrainAdapter
from lib.production_brain.store import ProductionBrainStore


def _external_run(project_dir, client):
    store = ProductionBrainStore(project_dir, gen_id=lambda: "run_x")
    HermesBrainAdapter(client=client).start(store, requested_duration_seconds=60)
    return store


def _job(store):
    return (store.read_state().get("brain") or {}).get("job_id")


class TestTruthfulCancellation:
    def test_unconfirmed_cancel_is_nonterminal(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        fail = FakeOrchestratorClient(fail_control=True)
        _external_run(d, fail)
        st = brain_api.cancel_run(d, {"run_id": "run_x"}, orchestrator=fail)
        # Never terminally cancelled on an unconfirmed external cancel.
        assert st["state"] == "cancelling" and st["terminal"] is False
        assert any(b["kind"] == "control_unconfirmed" and not b["resolved"] for b in st["blockers"])

    def test_retry_cancel_after_ack_is_terminal(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        fail = FakeOrchestratorClient(fail_control=True)
        store = _external_run(d, fail)
        job = _job(store)
        brain_api.cancel_run(d, {"run_id": "run_x"}, orchestrator=fail)  # → cancelling
        good = FakeOrchestratorClient()
        st = brain_api.cancel_run(d, {"run_id": "run_x"}, orchestrator=good)
        assert st["state"] == "cancelled" and st["terminal"] is True
        assert good.cancelled == [job]

    def test_restart_recovery_keeps_cancelling(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        fail = FakeOrchestratorClient(fail_control=True)
        store = _external_run(d, fail)
        brain_api.cancel_run(d, {"run_id": "run_x"}, orchestrator=fail)
        store.state_path.unlink()  # simulate a crash losing the cache
        rebuilt = ProductionBrainStore(d).read_state()
        assert rebuilt["state"] == "cancelling" and rebuilt["terminal"] is False


class TestRealRetryResume:
    def test_retry_tells_orchestrator_then_advances_local(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        good = FakeOrchestratorClient()
        store = _external_run(d, good)
        store.fail_stage("render", error="boom")
        st = brain_api.retry_stage(d, {"stage": "render", "run_id": "run_x"}, orchestrator=good)
        assert any(c["action"] == "retry" for c in good.controls)
        assert next(s for s in st["stages"] if s["id"] == "render")["status"] == "active"

    def test_retry_failure_blocks_and_does_not_fake_local_retry(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        fail = FakeOrchestratorClient(fail_control=True)
        store = _external_run(d, fail)
        store.fail_stage("render", error="boom")
        st = brain_api.retry_stage(d, {"stage": "render", "run_id": "run_x"}, orchestrator=fail)
        assert st["state"] == "blocked"
        assert any(b["kind"] == "control_unconfirmed" for b in st["blockers"])
        assert next(s for s in st["stages"] if s["id"] == "render")["status"] != "active"

    def test_resume_tells_orchestrator(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        good = FakeOrchestratorClient()
        _external_run(d, good)
        st = brain_api.resume_run(d, {"run_id": "run_x"}, orchestrator=good)
        assert any(c["action"] == "resume" for c in good.controls)
        assert st["state"] == "running"

    def test_resume_failure_blocks(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        fail = FakeOrchestratorClient(fail_control=True)
        _external_run(d, fail)
        st = brain_api.resume_run(d, {"run_id": "run_x"}, orchestrator=fail)
        assert st["state"] == "blocked"
        assert any(b["kind"] == "control_unconfirmed" for b in st["blockers"])


class TestExternalControlRequiresExactHandles:
    def test_resume_without_run_id_rejected(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        good = FakeOrchestratorClient()
        _external_run(d, good)
        with pytest.raises(brain_api.BrainApiError) as ei:
            brain_api.resume_run(d, {}, orchestrator=good)  # no run_id → refused
        assert ei.value.status == 400
        assert good.controls == []  # orchestrator NEVER contacted

    def test_retry_wrong_run_id_rejected(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        good = FakeOrchestratorClient()
        store = _external_run(d, good)
        store.fail_stage("render", error="boom")
        with pytest.raises(brain_api.BrainApiError) as ei:
            brain_api.retry_stage(d, {"stage": "render", "run_id": "run_STALE"}, orchestrator=good)
        assert ei.value.status == 409
        assert good.controls == []

    def test_retry_wrong_job_id_rejected(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        good = FakeOrchestratorClient()
        store = _external_run(d, good)
        store.fail_stage("render", error="boom")
        with pytest.raises(brain_api.BrainApiError) as ei:
            brain_api.retry_stage(d, {"stage": "render", "run_id": "run_x", "job_id": "job-WRONG"},
                                 orchestrator=good)
        assert ei.value.status == 409
        assert good.controls == []
