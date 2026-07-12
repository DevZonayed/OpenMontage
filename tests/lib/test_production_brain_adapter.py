"""Hermes brain adapter contract + deterministic fake brain.

  * The real HermesBrainAdapter FAILS CLOSED when no subscription engine is
    signed in — it never fabricates an LLM and never opens a run.
  * Identity is stable + non-secret and is stamped onto telemetry.
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


class TestHermesFailClosed:
    def test_unavailable_brain_refuses_to_start(self, tmp_path):
        s = _store(tmp_path)
        h = HermesBrainAdapter(probe=lambda: {"available": False, "engine": None, "detail": "not signed in"})
        assert h.available() is False
        with pytest.raises(BrainUnavailable):
            h.start(s, requested_duration_seconds=60)
        # No run was opened — the state is still not_started.
        assert s.read_state()["state"] == "not_started"
        assert s.read_events_raw() == []

    def test_probe_exception_is_treated_as_unavailable(self, tmp_path):
        def boom():
            raise RuntimeError("cli missing")

        h = HermesBrainAdapter(probe=boom)
        assert h.available() is False

    def test_available_brain_stamps_identity(self, tmp_path):
        s = _store(tmp_path)
        h = HermesBrainAdapter(
            probe=lambda: {"available": True, "engine": "claude", "detail": "ok"},
            session_id="sess-abc")
        st = h.start(s, requested_duration_seconds=90)
        assert st["state"] == "running"
        assert st["brain"]["engine"] == "claude"
        assert st["brain"]["agent_id"] == "hermes:claude"
        assert st["brain"]["session_id"] == "sess-abc"

    def test_identity_carries_no_secret(self, tmp_path):
        h = HermesBrainAdapter(probe=lambda: {"available": True, "engine": "claude"})
        block = h.identity().to_brain_block()
        assert set(block) == {"name", "adapter", "available", "agent_id", "session_id", "engine"}


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
