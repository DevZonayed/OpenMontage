"""Secure orchestration port: production client fails closed; fake is test-only.

  * ConfiguredHermesOrchestratorClient is UNAVAILABLE when no endpoint is
    configured, and create_job/cancel_job raise OrchestratorUnavailable rather
    than fabricating ids. It never calls out in these tests (no url configured).
  * FakeOrchestratorClient is deterministic, idempotent on the key, records
    start/cancel, and never touches the network.
"""

from __future__ import annotations

import pytest

from lib.production_brain.orchestrator import (
    ConfiguredHermesOrchestratorClient,
    FakeOrchestratorClient,
    OrchestratorHandle,
    OrchestratorUnavailable,
    default_orchestrator_client,
)


class TestConfiguredClientFailsClosed:
    def test_unconfigured_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("OPENMONTAGE_HERMES_ORCHESTRATOR_URL", raising=False)
        c = ConfiguredHermesOrchestratorClient(url=None)
        assert c.available() is False

    def test_create_job_unconfigured_raises_actionable(self, monkeypatch):
        monkeypatch.delenv("OPENMONTAGE_HERMES_ORCHESTRATOR_URL", raising=False)
        c = ConfiguredHermesOrchestratorClient(url=None)
        with pytest.raises(OrchestratorUnavailable) as ei:
            c.create_job(project_id="p", run_id="r", requested_duration_seconds=60,
                         idempotency_key="p:r")
        assert "OPENMONTAGE_HERMES_ORCHESTRATOR_URL" in str(ei.value)

    def test_cancel_unconfigured_raises(self, monkeypatch):
        monkeypatch.delenv("OPENMONTAGE_HERMES_ORCHESTRATOR_URL", raising=False)
        c = ConfiguredHermesOrchestratorClient(url=None)
        with pytest.raises(OrchestratorUnavailable):
            c.cancel_job(job_id="job-1")

    def test_default_client_is_the_configured_one(self, monkeypatch):
        monkeypatch.delenv("OPENMONTAGE_HERMES_ORCHESTRATOR_URL", raising=False)
        c = default_orchestrator_client()
        assert isinstance(c, ConfiguredHermesOrchestratorClient)
        assert c.kind == "live"
        assert c.available() is False  # nothing configured here → fail-closed


class TestFakeClient:
    def test_returns_canonical_ids(self):
        c = FakeOrchestratorClient()
        h = c.create_job(project_id="proj", run_id="run_1",
                         requested_duration_seconds=60, idempotency_key="proj:run_1")
        assert isinstance(h, OrchestratorHandle) and h.is_valid()
        assert h.session_id == "fake-sess-run_1" and h.job_id == "fake-job-run_1"

    def test_idempotent_on_key(self):
        c = FakeOrchestratorClient()
        a = c.create_job(project_id="p", run_id="run_1", requested_duration_seconds=60, idempotency_key="k1")
        b = c.create_job(project_id="p", run_id="run_1", requested_duration_seconds=60, idempotency_key="k1")
        assert a is b and len(c.created) == 1

    def test_records_cancel(self):
        c = FakeOrchestratorClient()
        c.cancel_job(job_id="job-9")
        assert c.cancelled == ["job-9"]

    def test_kind_is_fake(self):
        assert FakeOrchestratorClient().kind == "fake"
