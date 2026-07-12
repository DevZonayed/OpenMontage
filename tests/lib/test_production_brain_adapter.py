"""Hermes brain adapter contract + deterministic fake brain.

  * The real HermesBrainAdapter FAILS CLOSED when no orchestrator can create a
    durable job — it never fabricates session/job ids and never opens a run.
  * When an orchestrator returns canonical ids, they are recorded VERBATIM
    (never minted) and stamped onto telemetry; cancellation correlates with the
    external handle.
  * The FakeBrain drives the entire 11-stage machine offline (no paid services),
    producing visible, ordered stage/task changes with an approval gate.
"""

from __future__ import annotations

import pytest

from lib.production_brain.adapter import (
    BrainUnavailable,
    FakeBrain,
    HermesBrainAdapter,
)
from lib.production_brain.orchestrator import (
    FakeOrchestratorClient,
    OrchestratorHandle,
    OrchestratorUnavailable,
)
from lib.production_brain.store import ProductionBrainStore


def _clock():
    t = {"n": 0}

    def now():
        t["n"] += 1
        return f"2026-07-12T00:{t['n'] // 60:02d}:{t['n'] % 60:02d}+00:00"

    return now


def _store(tmp_path, rid="run_1"):
    d = tmp_path / "proj"
    d.mkdir()
    return ProductionBrainStore(d, now=_clock(), gen_id=lambda: rid)


class _UnavailableClient:
    kind = "live"
    engine = "hermes"

    def available(self):
        return False

    def create_job(self, **kw):
        raise OrchestratorUnavailable("no orchestrator configured")

    def cancel_job(self, **kw):
        pass


class _NoIdClient(_UnavailableClient):
    """Available, but returns an invalid (empty-id) handle → must fail closed."""

    def available(self):
        return True

    def create_job(self, **kw):
        return OrchestratorHandle(session_id="", job_id="")


class _CountingClient:
    """Thread-safe live client that counts create_job calls (idempotent on key)."""

    kind = "live"
    engine = "hermes"

    def __init__(self, on_create=None):
        import threading

        self._lock = threading.Lock()
        self._on_create = on_create
        self.create_calls = 0
        self._by_key = {}
        self.cancelled = []

    def available(self):
        return True

    def create_job(self, *, project_id, run_id, requested_duration_seconds, idempotency_key):
        if self._on_create:
            self._on_create()
        with self._lock:
            if idempotency_key in self._by_key:
                return self._by_key[idempotency_key]
            self.create_calls += 1
            h = OrchestratorHandle(session_id=f"sess-{run_id}", job_id=f"job-{run_id}",
                                   engine=self.engine)
            self._by_key[idempotency_key] = h
            return h

    def cancel_job(self, *, job_id):
        self.cancelled.append(job_id)


class TestHermesFailClosed:
    def test_unavailable_orchestrator_refuses_to_start(self, tmp_path):
        s = _store(tmp_path)
        h = HermesBrainAdapter(client=_UnavailableClient())
        assert h.available() is False
        with pytest.raises(BrainUnavailable):
            h.start(s, requested_duration_seconds=60)
        # No run was opened — the state is still not_started, log empty.
        assert s.read_state()["state"] == "not_started"
        assert s.read_events_raw() == []

    def test_client_returning_no_canonical_id_fails_closed(self, tmp_path):
        s = _store(tmp_path)
        h = HermesBrainAdapter(client=_NoIdClient())
        assert h.available() is True  # endpoint reachable...
        with pytest.raises(BrainUnavailable):  # ...but it returned no real ids
            h.start(s, requested_duration_seconds=60)
        assert s.read_events_raw() == []

    def test_client_exception_is_treated_as_unavailable(self, tmp_path):
        class _Boom:
            kind = "live"
            engine = "hermes"

            def available(self):
                raise RuntimeError("network down")

            def create_job(self, **kw):
                raise RuntimeError("network down")

            def cancel_job(self, **kw):
                pass

        h = HermesBrainAdapter(client=_Boom())
        assert h.available() is False

    def test_real_ids_are_recorded_verbatim(self, tmp_path):
        s = _store(tmp_path)
        client = FakeOrchestratorClient(engine="hermes-fake")
        h = HermesBrainAdapter(client=client)
        st = h.start(s, requested_duration_seconds=90)
        assert st["state"] == "running"
        # session/job ids are the ones the orchestrator returned, not minted here.
        handle = client.created[f"{s.project_dir.name}:{st['run_id']}"]
        assert st["brain"]["session_id"] == handle.session_id
        assert st["brain"]["job_id"] == handle.job_id
        # A fake client is visibly fake_driver — never a live external job.
        assert st["brain"]["orchestration"] == "fake_driver"
        # The run_started event carries the same external identity.
        started = [e for e in s.read_events_raw() if e["type"] == "run_started"][0]
        assert started["session_id"] == handle.session_id
        assert started["job_id"] == handle.job_id

    def test_idempotent_start_does_not_provision_second_job(self, tmp_path):
        s = ProductionBrainStore(tmp_path / "p", now=_clock(), gen_id=lambda: "run_1")
        (tmp_path / "p").mkdir(exist_ok=True)
        client = FakeOrchestratorClient()
        h = HermesBrainAdapter(client=client)
        first = h.start(s, requested_duration_seconds=60)
        second = h.start(s, requested_duration_seconds=60)
        assert second.get("already_active") is True
        assert first["run_id"] == second["run_id"]
        assert len(client.created) == 1  # only ONE external job was created

    def test_concurrent_starts_create_exactly_one_external_job(self, tmp_path):
        import threading

        d = tmp_path / "p"
        d.mkdir()
        # A provision that BLOCKS briefly so both threads overlap in the window —
        # only the lock winner may reach create_job.
        gate = threading.Event()
        client = _CountingClient(on_create=lambda: gate.wait(0.05))
        results = []

        def go(i):
            s = ProductionBrainStore(d, gen_id=(lambda i=i: f"run_{i}"))
            h = HermesBrainAdapter(client=client)
            results.append(h.start(s, requested_duration_seconds=60))

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join()

        store = ProductionBrainStore(d)
        started = [e for e in store.read_events_raw() if e["type"] == "run_started"]
        assert len(started) == 1
        # Exactly ONE external job was created — no orphans from the losers.
        assert client.create_calls == 1
        winners = [r for r in results if not r.get("already_active")]
        assert len(winners) == 1

    def test_orphan_compensated_when_local_persist_fails(self, tmp_path):
        from lib.production_brain.store import BrainStoreError

        d = tmp_path / "p"
        d.mkdir()
        s = ProductionBrainStore(d, gen_id=lambda: "run_1")
        client = FakeOrchestratorClient()
        h = HermesBrainAdapter(client=client)

        # Make the durable run_started write fail AFTER the external job is created.
        def boom(*a, **k):
            raise OSError("disk full")

        s._append_locked = boom  # type: ignore[assignment]
        with pytest.raises(BrainStoreError) as ei:
            h.start(s, requested_duration_seconds=60)
        # No local run opened...
        assert s.read_state()["state"] == "not_started"
        assert s.read_events_raw() == []
        # ...and the orphaned external job was cancelled EXACTLY once.
        assert len(client.created) == 1
        job = next(iter(client.created.values())).job_id
        assert client.cancelled == [job]
        assert "cancelled" in str(ei.value).lower()

    def test_compensation_failure_is_reported_no_run(self, tmp_path):
        from lib.production_brain.store import BrainStoreError

        d = tmp_path / "p"
        d.mkdir()
        s = ProductionBrainStore(d, gen_id=lambda: "run_1")
        client = FakeOrchestratorClient(fail_control=True)  # cancel will also fail
        h = HermesBrainAdapter(client=client)

        def boom(*a, **k):
            raise OSError("disk full")

        s._append_locked = boom  # type: ignore[assignment]
        with pytest.raises(BrainStoreError) as ei:
            h.start(s, requested_duration_seconds=60)
        assert s.read_events_raw() == []  # no run, truthfully reported
        assert "could not be cancelled" in str(ei.value).lower()

    def test_cancel_correlates_with_external_handle(self, tmp_path):
        s = _store(tmp_path)
        client = FakeOrchestratorClient()
        h = HermesBrainAdapter(client=client)
        st = h.start(s, requested_duration_seconds=60)
        job = st["brain"]["job_id"]
        assert h.cancel_external(job_id=job) is True
        assert client.cancelled == [job]

    def test_start_message_does_not_claim_online_orchestrator(self, tmp_path):
        s = _store(tmp_path)
        h = HermesBrainAdapter(client=FakeOrchestratorClient())
        st = h.start(s, requested_duration_seconds=60)
        assert "online" not in (st.get("activity") or "").lower()

    def test_identity_carries_no_secret(self, tmp_path):
        h = HermesBrainAdapter(client=FakeOrchestratorClient())
        block = h.identity().to_brain_block()
        assert set(block) == {"name", "adapter", "available", "agent_id",
                              "session_id", "engine", "orchestration"}


class TestFakeBrainDrive:
    def test_drives_all_stages_to_completion(self, tmp_path):
        s = _store(tmp_path, rid="run_x")
        st = FakeBrain().drive(s, requested_duration_seconds=180, run_id="run_x")
        assert st["state"] == "completed" and st["terminal"] is True
        assert st["requested_duration_seconds"] == 180
        assert st["actual_duration_seconds"] == 180.0
        assert all(x["status"] in ("done", "skipped") for x in st["stages"])
        assert st["counts"]["outputs"] >= 8
        # Every event is stamped with the fake brain identity.
        for e in s.read_events_raw():
            if e["type"] in ("stage_entered", "tool_call"):
                assert e.get("agent_id") == "fake-hermes-agent"
                assert e.get("session_id") == "fake-session-0001"

    def test_stages_are_entered_in_canonical_order(self, tmp_path):
        s = _store(tmp_path, rid="run_x")
        FakeBrain().drive(s, requested_duration_seconds=60, run_id="run_x")
        entered = [e["stage"] for e in s.read_events_raw() if e["type"] == "stage_entered"]
        from lib.production_brain.schema import STAGES

        assert entered == list(STAGES)

    def test_pending_gate_when_approver_none(self, tmp_path):
        s = _store(tmp_path, rid="run_x")
        st = FakeBrain().drive(s, requested_duration_seconds=60, run_id="run_x", approver=None)
        assert st["state"] == "awaiting_approval"
        assert st["current_stage"] == "proposal"

    def test_rejection_fails_the_run(self, tmp_path):
        s = _store(tmp_path, rid="run_x")
        st = FakeBrain().drive(s, requested_duration_seconds=60, run_id="run_x",
                               approver=lambda state: False)
        assert st["state"] == "failed"

    def test_stop_after_partial_drive(self, tmp_path):
        s = _store(tmp_path, rid="run_x")
        st = FakeBrain().drive(s, requested_duration_seconds=60, run_id="run_x",
                               stop_after="script")
        assert st["state"] == "running"
        by_id = {x["id"]: x for x in st["stages"]}
        assert by_id["script"]["status"] == "done"
        assert by_id["assets"]["status"] == "pending"

    def test_fake_brain_never_leaks_secret_even_if_asked(self, tmp_path):
        # Defensive: identity + telemetry must stay secret-free by construction.
        s = _store(tmp_path, rid="run_x")
        FakeBrain().drive(s, requested_duration_seconds=30, run_id="run_x")
        blob = s.events_path.read_text(encoding="utf-8")
        for needle in ("api_key", "sk-", "Bearer "):
            assert needle not in blob
