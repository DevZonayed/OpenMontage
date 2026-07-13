"""Secure orchestration port: canonical ids, the port shape, and the fake client.

The Mochlet/MCP endpoint+token client is gone. The orchestrator module now exposes
only the id/handle primitives, the ``HermesOrchestratorClient`` Protocol, the
deterministic ``FakeOrchestratorClient`` (test-only), and
``default_orchestrator_client()`` which builds the native Hermes Agent client
(fail-closed when not configured).

  * ``is_canonical_id`` / ``OrchestratorHandle.is_valid`` — the id allowlist.
  * ``FakeOrchestratorClient`` — deterministic, idempotent on the key, records
    start/cancel/control, never touches the network.
  * ``default_orchestrator_client`` — the native fail-closed client.
"""

from __future__ import annotations

import pytest

from lib.production_brain.orchestrator import (
    FakeOrchestratorClient,
    HermesOrchestratorClient,
    OrchestratorHandle,
    OrchestratorUnavailable,
    default_orchestrator_client,
    is_canonical_id,
)


class TestCanonicalIds:
    @pytest.mark.parametrize("val", ["job-1", "sess_ABC.9", "run:123", "a" * 128])
    def test_valid(self, val):
        assert is_canonical_id(val) is True

    @pytest.mark.parametrize("val", [
        "job/1",          # slash
        "job\\1",         # backslash
        "..",             # traversal
        "a/../b",         # traversal
        "job..1",         # dot-dot
        "job%2f1",        # encoded slash literal (percent not allowed)
        "job\n1",         # newline / control
        "job 1",          # whitespace
        "a" * 129,        # too long (bound is 128)
        "",               # empty
        123,              # non-string
        None,             # non-string
        {"x": 1},         # non-string object (never str()-coerced)
    ])
    def test_invalid(self, val):
        assert is_canonical_id(val) is False

    def test_handle_is_valid_checks_both(self):
        assert OrchestratorHandle("sess-1", "job-1").is_valid() is True
        assert OrchestratorHandle("sess/1", "job-1").is_valid() is False
        assert OrchestratorHandle("sess-1", "job/1").is_valid() is False


class TestHandle:
    def test_carries_engine_and_detail(self):
        h = OrchestratorHandle(session_id="s-1", job_id="j-1", engine="hermes-agent",
                               detail="native session")
        assert h.session_id == "s-1" and h.job_id == "j-1"
        assert h.engine == "hermes-agent" and h.detail == "native session"


class TestProtocolShape:
    def test_fake_client_satisfies_the_port(self):
        c = FakeOrchestratorClient()
        assert isinstance(c, HermesOrchestratorClient)
        # The port surface the adapter relies on.
        for method in ("available", "create_job", "cancel_job", "control_job"):
            assert callable(getattr(c, method))
        assert hasattr(c, "kind")


class TestFakeClient:
    def test_kind_is_fake(self):
        assert FakeOrchestratorClient().kind == "fake"

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

    def test_available_flag(self):
        assert FakeOrchestratorClient(available=True).available() is True
        assert FakeOrchestratorClient(available=False).available() is False

    def test_records_cancel(self):
        c = FakeOrchestratorClient()
        c.cancel_job(job_id="job-9")
        assert c.cancelled == ["job-9"]

    def test_records_control_actions(self):
        c = FakeOrchestratorClient()
        c.control_job(job_id="job-1", action="retry", idempotency_key="k1")
        c.control_job(job_id="job-1", action="resume", idempotency_key="k2")
        actions = [x["action"] for x in c.controls]
        assert actions == ["retry", "resume"]
        assert c.controls[0]["job_id"] == "job-1"

    def test_cancel_is_recorded_as_a_control(self):
        c = FakeOrchestratorClient()
        c.cancel_job(job_id="job-7")
        assert c.controls[-1]["action"] == "cancel"
        assert c.cancelled == ["job-7"]

    def test_fail_control_raises(self):
        c = FakeOrchestratorClient(fail_control=True)
        with pytest.raises(OrchestratorUnavailable):
            c.control_job(job_id="job-1", action="retry", idempotency_key="k")

    def test_custom_engine_stamped_on_handle(self):
        c = FakeOrchestratorClient(engine="hermes-fake")
        h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=1, idempotency_key="k")
        assert h.engine == "hermes-fake"


class TestDefaultClientIsNativeAndFailClosed:
    def test_default_client_is_native_fail_closed(self):
        # No Hermes Agent connected on this machine → the native client is built but
        # reports unavailable (fail closed). It never fabricates ids and never
        # calls a paid service.
        c = default_orchestrator_client()
        assert c.available() is False

    def test_default_client_create_job_fails_closed(self):
        c = default_orchestrator_client()
        with pytest.raises(OrchestratorUnavailable):
            c.create_job(project_id="p", run_id="r", requested_duration_seconds=60,
                         idempotency_key="p:r")
