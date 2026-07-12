"""Canonical production-brain store: append-only event history + materialized view.

Contract under test (lib/production_brain/store.py + schema.py):
  * the event log is authoritative; state.json is a rebuildable cache (crash
    recovery), so a deleted/torn/stale cache reconstructs faithfully;
  * monotonic sequence numbers + cursor reads give strict event ordering;
  * ``start`` is idempotent (one active run); ``cancel`` validates the EXACT
    run id; terminal states are sticky and truthful;
  * invalid coarse-state transitions are rejected (strict) / clamped (lenient);
  * secrets NEVER reach the persisted telemetry;
  * the 1..300s requested-duration contract is preserved and kept distinct from
    the actual rendered duration (300s ⇒ 9000 frames @30fps).

Everything time/id related is injected so the tests are hermetic.
"""

from __future__ import annotations

import json

import pytest

from lib import duration as dur
from lib.production_brain import schema as S
from lib.production_brain.store import BrainStoreError, ProductionBrainStore


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


def _started(tmp_path, rid="run_1", secs=120):
    s = _store(tmp_path, rid)
    s.start(brain={"name": "hermes", "adapter": "fake", "available": True,
                   "agent_id": "a1", "session_id": "sess1"},
            requested_duration_seconds=secs)
    return s


class TestStartIdempotency:
    def test_start_records_running_with_identity(self, tmp_path):
        s = _started(tmp_path, secs=90)
        st = s.read_state()
        assert st["state"] == "running"
        assert st["run_id"] == "run_1"
        assert st["brain"]["agent_id"] == "a1"
        assert st["requested_duration_seconds"] == 90

    def test_duplicate_start_is_idempotent(self, tmp_path):
        s = _started(tmp_path)
        again = s.start(run_id="run_2")  # must NOT open a second run
        assert again.get("already_active") is True
        assert again["run_id"] == "run_1"
        assert sum(1 for e in s.read_events_raw() if e["type"] == "run_started") == 1

    def test_start_after_terminal_starts_new_run(self, tmp_path):
        s = _store(tmp_path, rid="run_1")
        s.start(requested_duration_seconds=60)
        s.cancel("run_1")
        s2 = ProductionBrainStore(s.project_dir, now=_clock(), gen_id=lambda: "run_2")
        st = s2.start(requested_duration_seconds=60)
        assert st["run_id"] == "run_2" and st["state"] == "running"

    def test_invalid_requested_duration_rejected(self, tmp_path):
        s = _store(tmp_path)
        with pytest.raises(BrainStoreError):
            s.start(requested_duration_seconds=901)
        with pytest.raises(BrainStoreError):
            s.start(requested_duration_seconds=0)


class TestEventOrderingAndCursors:
    def test_seq_is_monotonic_and_dense(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        s.stage_progress("research", 0.4)
        s.complete_stage("research")
        seqs = [e["seq"] for e in s.read_events_raw()]
        assert seqs == list(range(1, len(seqs) + 1))

    def test_cursor_read_returns_only_newer_events(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        s.complete_stage("research")
        state = s.read_state()
        cur = state["cursor"]
        s.enter_stage("proposal")
        tail = s.read_events(after=cur)
        assert tail and all(e["seq"] > cur for e in tail)
        assert [e["type"] for e in tail] == ["stage_entered"]

    def test_cursor_read_respects_limit(self, tmp_path):
        s = _started(tmp_path)
        for _ in range(5):
            s.heartbeat()
        page = s.read_events(after=0, limit=3)
        assert len(page) == 3
        assert page[0]["seq"] == 1


class TestStateMachine:
    def test_full_happy_path_reaches_completed(self, tmp_path):
        s = _started(tmp_path, secs=120)
        for stage in ("research", "proposal", "script"):
            s.enter_stage(stage)
            s.complete_stage(stage)
        st = s.complete_run("run_1", actual_duration_seconds=118.4)
        assert st["state"] == "completed" and st["terminal"] is True
        assert st["requested_duration_seconds"] == 120
        assert st["actual_duration_seconds"] == 118.4

    def test_approval_gate_moves_to_awaiting_then_running(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("proposal")
        s.request_approval("proposal", prompt="ok?")
        assert s.read_state()["state"] == "awaiting_approval"
        st = s.grant_approval("run_1", stage="proposal", by="user")
        assert st["state"] == "running"
        assert st["approvals"][0]["status"] == "approved"

    def test_blocker_classification_and_clear(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("assets")
        s.raise_blocker("assets", kind="provider_access", message="No image key",
                        options=["Add key", "Skip"])
        st = s.read_state()
        assert st["state"] == "blocked"
        assert st["blockers"][0]["kind"] == "provider_access"
        assert st["blockers"][0]["options"] == ["Add key", "Skip"]
        s.clear_blocker("assets")
        st = s.read_state()
        assert st["state"] == "running"
        assert st["blockers"][0]["resolved"] is True

    def test_unknown_blocker_kind_is_coerced_to_other(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("assets")
        s.raise_blocker("assets", kind="nonsense", message="huh")
        assert s.read_state()["blockers"][0]["kind"] == "other"

    def test_retry_reopens_failed_stage(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("render")
        s.fail_stage("render", error="encoder crash")
        assert s.read_state()["stages"][7]["status"] == "failed"
        st = s.retry_stage("render", run_id="run_1")
        rstage = next(x for x in st["stages"] if x["id"] == "render")
        assert rstage["status"] == "active" and rstage["error"] is None


class TestControlEventsCarryExternalHandle:
    def _started_with_handle(self, tmp_path):
        s = _store(tmp_path)
        s.start(brain={"name": "hermes", "adapter": "hermes", "session_id": "sess-9",
                       "job_id": "job-9", "external": True},
                requested_duration_seconds=120, session_id="sess-9", job_id="job-9")
        return s

    def test_stage_and_control_events_are_stamped_with_session_job(self, tmp_path):
        s = self._started_with_handle(tmp_path)
        s.enter_stage("research")
        s.complete_stage("research")
        s.retry_stage("research", run_id="run_1")
        s.resume()
        s.cancel("run_1")
        by_type = {}
        for e in s.read_events_raw():
            by_type.setdefault(e["type"], []).append(e)
        # Every post-start event structurally carries the persisted external handle.
        for etype in ("stage_entered", "stage_completed", "retry", "resume", "run_cancelled"):
            ev = by_type[etype][-1]
            assert ev.get("session_id") == "sess-9", (etype, ev)
            assert ev.get("job_id") == "job-9", (etype, ev)

    def test_run_started_carries_its_own_handle(self, tmp_path):
        s = self._started_with_handle(tmp_path)
        started = [e for e in s.read_events_raw() if e["type"] == "run_started"][0]
        assert started["session_id"] == "sess-9" and started["job_id"] == "job-9"


class TestCancel:
    def test_cancel_exact_id(self, tmp_path):
        s = _started(tmp_path)
        st = s.cancel("run_1")
        assert st["state"] == "cancelled" and st["terminal"] is True
        assert "preserved" in st["activity"].lower()

    def test_cancel_wrong_id_rejected(self, tmp_path):
        s = _started(tmp_path)
        with pytest.raises(BrainStoreError) as ei:
            s.cancel("run_WRONG")
        assert ei.value.status == 409

    def test_cancel_no_active_run_rejected(self, tmp_path):
        s = _store(tmp_path)
        with pytest.raises(BrainStoreError):
            s.cancel("run_1")

    def test_cancel_skips_in_flight_stage_but_keeps_completed(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        s.complete_stage("research")
        s.enter_stage("proposal")
        st = s.cancel("run_1")
        by_id = {x["id"]: x for x in st["stages"]}
        assert by_id["research"]["status"] == "done"
        assert by_id["proposal"]["status"] == "skipped"


class TestTerminalStickiness:
    def test_events_after_completion_are_rejected(self, tmp_path):
        s = _started(tmp_path)
        s.complete_run("run_1", actual_duration_seconds=60)
        n_before = len(s.read_events_raw())
        # A stray stage event on a terminal run is REJECTED (not silently
        # persisted) so the authoritative log can't contain impossible events.
        with pytest.raises(BrainStoreError) as ei:
            s.event("stage_entered", stage="assets", message="late")
        assert ei.value.status == 409
        st = s.read_state()
        assert st["state"] == "completed" and st["terminal"] is True
        assert len(s.read_events_raw()) == n_before  # nothing appended

    def test_strict_reduce_rejects_illegal_transition(self, tmp_path):
        st = S.materialize("p", [
            {"v": "1.0", "seq": 1, "ts": "2026-07-12T00:00:01+00:00",
             "type": "run_started", "run_id": "r", "project_id": "p", "data": {}},
        ])
        bad = {"v": "1.0", "seq": 2, "ts": "2026-07-12T00:00:02+00:00",
               "type": "run_cancelled", "run_id": "r", "project_id": "p"}
        # running → cancelled is allowed; but completed → running is not.
        st = S.reduce_event(st, bad)
        assert st["state"] == "cancelled"
        with pytest.raises(S.InvalidTransition):
            S.reduce_event(st, {"v": "1.0", "seq": 3, "ts": "t", "type": "stage_entered",
                                "stage": "assets"}, strict=True)


class TestNoImpossibleEvents:
    def test_event_before_start_is_rejected(self, tmp_path):
        s = _store(tmp_path)
        with pytest.raises(BrainStoreError) as ei:
            s.enter_stage("research")
        assert ei.value.status == 409
        assert s.read_events_raw() == []  # log stays empty

    def test_event_with_mismatched_run_id_rejected(self, tmp_path):
        s = _started(tmp_path)
        with pytest.raises(BrainStoreError) as ei:
            s.event("stage_entered", stage="research", run_id="run_WRONG")
        assert ei.value.status == 409

    def test_lifecycle_after_cancel_is_rejected(self, tmp_path):
        s = _started(tmp_path)
        s.cancel("run_1")
        n = len(s.read_events_raw())
        for call in (lambda: s.complete_run("run_1"),
                     lambda: s.fail_run("run_1", error="x"),
                     lambda: s.retry_stage("render", run_id="run_1"),
                     lambda: s.grant_approval("run_1")):
            with pytest.raises(BrainStoreError):
                call()
        assert len(s.read_events_raw()) == n  # nothing appended after terminal


class TestConcurrentStart:
    def test_parallel_starts_append_exactly_one_run_started(self, tmp_path):
        import threading

        d = tmp_path / "proj"
        d.mkdir()
        results = []

        def go(i):
            # Each thread uses its own store instance + unique run id; the shared
            # file lock must let exactly ONE win the run_started append.
            st = ProductionBrainStore(d, gen_id=(lambda i=i: f"run_{i}")).start(
                requested_duration_seconds=60)
            results.append(st)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        store = ProductionBrainStore(d)
        started = [e for e in store.read_events_raw() if e["type"] == "run_started"]
        assert len(started) == 1, f"expected exactly one run_started, got {len(started)}"
        winners = [r for r in results if not r.get("already_active")]
        assert len(winners) == 1
        assert sum(1 for r in results if r.get("already_active")) == 7


class TestCrashRecovery:
    def test_rebuild_from_log_when_cache_deleted(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        s.stage_progress("research", 0.7)
        before = s.read_state()
        s.state_path.unlink()  # simulate a crash that lost the cache
        after = s.read_state()
        assert after["cursor"] == before["cursor"]
        assert after["state"] == before["state"]
        assert after["stages"][0]["progress"] == 0.7

    def test_rebuild_from_log_when_cache_is_torn(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        s.state_path.write_text("{ this is not valid json", encoding="utf-8")
        after = s.read_state()
        assert after["state"] == "running"
        assert after["current_stage"] == "research"

    def test_torn_trailing_event_line_is_tolerated(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        with open(s.events_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 999, "type": "stage_ent')  # torn write, no newline
        st = s.read_state()  # must not raise; ignores the torn line
        assert st["state"] == "running"

    def test_resume_recomputes_and_marks(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        st = s.resume()
        assert st["state"] == "running"
        assert any(e["type"] == "resume" for e in s.read_events_raw())


class TestSecretRedaction:
    def test_secret_keys_and_values_never_persist(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("assets")
        s.event("provider_call", stage="assets", provider="openai",
                data={"api_key": "sk-abcdef0123456789ABCDEF",
                      "headers": {"Authorization": "Bearer zzzzzzzzzzzz1234"},
                      "note": "using key sk-DEADBEEFDEADBEEF00 now"})
        blob = s.events_path.read_text(encoding="utf-8")
        assert "sk-abcdef0123456789" not in blob
        assert "sk-DEADBEEFDEADBEEF00" not in blob
        assert "zzzzzzzzzzzz1234" not in blob
        assert "[redacted]" in blob
        last = [e for e in s.read_events_raw() if e["type"] == "provider_call"][-1]
        assert last["redacted"] is True

    def test_state_cache_has_no_secret(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("assets")
        s.event("tool_call", stage="assets", tool="x",
                data={"client_secret": "supersecretvalue"})
        assert "supersecretvalue" not in s.state_path.read_text(encoding="utf-8")


class TestDurationInvariants:
    @pytest.mark.parametrize("secs,frames", [(1, 30), (60, 1800), (300, 9000)])
    def test_requested_duration_maps_to_frame_budget(self, tmp_path, secs, frames):
        s = _started(tmp_path, secs=secs)
        st = s.read_state()
        assert st["requested_duration_seconds"] == secs
        assert dur.frames_for(secs) == frames

    def test_requested_and_actual_are_distinct(self, tmp_path):
        s = _started(tmp_path, secs=300)
        s.enter_stage("render")
        s.output("render", kind="video", path="renders/final.mp4",
                 actual_duration_seconds=297.966)
        st = s.complete_run("run_1")
        assert st["requested_duration_seconds"] == 300
        assert st["actual_duration_seconds"] == 297.966
        assert st["actual_duration_seconds"] != st["requested_duration_seconds"]


class TestSanitization:
    def test_payload_carries_live_elapsed_on_active_stage(self, tmp_path):
        s = _started(tmp_path)
        s.enter_stage("research")
        p = s.payload()
        active = next(x for x in p["stages"] if x["id"] == "research")
        assert active["elapsed_seconds"] is not None and active["elapsed_seconds"] >= 0

    def test_state_validates_against_schema(self, tmp_path):
        import jsonschema

        from lib.paths import REPO_ROOT

        schema = json.loads((REPO_ROOT / "schemas/artifacts/production_run_state.schema.json").read_text())
        s = _started(tmp_path)
        s.enter_stage("research")
        s.complete_stage("research")
        jsonschema.validate(s.read_state(), schema)
