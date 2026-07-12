"""End-to-end: Start via the Mochlet MCP bridge creates exactly ONE real job with
canonical handles; retry/resume record the successor handle (no fake, no keyring)."""

from __future__ import annotations

import backlot.brain_api as brain_api
from lib.production_brain.adapter import HermesBrainAdapter
from lib.production_brain.mochlet import (
    JobIdempotencyStore,
    MochletMcpOrchestratorClient,
    is_uuid,
)
from lib.production_brain.store import ProductionBrainStore
from tests.production_status._fake_mcp import FakeMochletMcp

PID = "669a5386-f37b-4c6f-a712-b12e8221e54d"


def _mochlet_client(fake, tmp_path):
    return MochletMcpOrchestratorClient(
        endpoint="http://127.0.0.1:9235/mcp", mochlet_project_id=PID,
        project_path="/repo/the-electricity-bulb", transport=fake.transport,
        token_getter=lambda: fake.token,
        idempotency_store=JobIdempotencyStore(tmp_path / "idem.json"))


def test_start_creates_one_real_external_job(tmp_path):
    proj = tmp_path / "the-electricity-bulb"
    proj.mkdir()
    fake = FakeMochletMcp()
    client = _mochlet_client(fake, tmp_path)
    adapter = HermesBrainAdapter(client=client)
    assert adapter.available() is True
    store = ProductionBrainStore(proj)
    state = adapter.start(store, requested_duration_seconds=150)
    brain = state["brain"]
    assert brain["orchestration"] == "external_job"  # LIVE, not fixture
    assert is_uuid(brain["job_id"]) and is_uuid(brain["session_id"])
    assert brain["engine"] == "mochlet"
    assert len(fake.sent_chats) == 1  # exactly one real sendChat

    # idempotent Start — a second start does NOT create a second Mochlet job
    state2 = adapter.start(store, requested_duration_seconds=150)
    assert state2["brain"]["job_id"] == brain["job_id"]
    assert len(fake.sent_chats) == 1


def test_retry_keeps_the_same_job_id(tmp_path):
    # runJob re-runs the same job → the live handle must NOT change; activity says retrying.
    proj = tmp_path / "eb"
    proj.mkdir()
    fake = FakeMochletMcp()
    client = _mochlet_client(fake, tmp_path)
    adapter = HermesBrainAdapter(client=client)
    store = ProductionBrainStore(proj)
    state = adapter.start(store, requested_duration_seconds=150)
    rid = state["run_id"]
    original_job = state["brain"]["job_id"]

    new_state = brain_api.retry_stage(
        proj, {"run_id": rid, "stage": "assets", "job_id": original_job},
        orchestrator=client)
    brain = new_state["brain"]
    assert brain["job_id"] == original_job          # SAME job (re-run), no successor
    assert "predecessor_job_id" not in brain         # no fabricated lineage
    assert fake.controls[-1]["action"] == "run"


def test_resume_records_successor_new_job_same_session(tmp_path):
    # resume → sendChat on the session → NEW job, same session; live handle swaps,
    # predecessor recorded, activity says "Resumed as successor".
    proj = tmp_path / "ebr"
    proj.mkdir()
    fake = FakeMochletMcp()
    client = _mochlet_client(fake, tmp_path)
    adapter = HermesBrainAdapter(client=client)
    store = ProductionBrainStore(proj)
    state = adapter.start(store, requested_duration_seconds=150)
    rid = state["run_id"]
    original_job = state["brain"]["job_id"]
    original_session = state["brain"]["session_id"]

    new_state = brain_api.resume_run(
        proj, {"run_id": rid, "job_id": original_job}, orchestrator=client)
    brain = new_state["brain"]
    assert brain["job_id"] != original_job              # successor job
    assert is_uuid(brain["job_id"])
    assert brain["predecessor_job_id"] == original_job  # lineage preserved
    assert brain["session_id"] == original_session      # SAME session
    assert "successor" in new_state["activity"].lower()
    # resume went through sendChat, never continueSession
    assert not any(cc["action"] == "continue" for cc in fake.controls)


def test_cancel_targets_the_exact_mochlet_job(tmp_path):
    proj = tmp_path / "eb2"
    proj.mkdir()
    fake = FakeMochletMcp()
    client = _mochlet_client(fake, tmp_path)
    adapter = HermesBrainAdapter(client=client)
    store = ProductionBrainStore(proj)
    state = adapter.start(store, requested_duration_seconds=90)
    rid = state["run_id"]
    job_id = state["brain"]["job_id"]
    out = brain_api.cancel_run(proj, {"run_id": rid}, orchestrator=client)
    assert fake.cancelled == [job_id]
    assert out["state"] == "cancelled"
