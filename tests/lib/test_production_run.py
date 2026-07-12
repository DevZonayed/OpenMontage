"""Durable per-project production-run controller.

Drives lib/production_run.py: a persisted run.json with a real lifecycle, atomic
writes, reconciliation after a Backlot restart (an orphaned worker → failed),
one active run per project, idempotent/race-safe start, and cancel by EXACT
run-id that signals ONLY the worker pid. Everything is injectable (spawn / now /
pid_alive / terminate / gen_id) so tests are hermetic — no real subprocess.
"""

from __future__ import annotations

import pytest

from lib import production_run as pr
from lib.production_run import RunError


def _project(tmp_path, secs=150):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "intake.json").write_text(
        '{"project_id":"proj","pipeline_type":"animation","target_duration_seconds":%d}' % secs)
    return d


class _Spawner:
    def __init__(self, pid=4242):
        self.pid = pid
        self.calls = []

    def __call__(self, project_id, project_dir):
        self.calls.append((project_id, str(project_dir)))
        return self.pid


def _clock():
    t = {"n": 1000}
    def now():
        t["n"] += 1
        return f"2026-07-10T00:00:{t['n']:02d}+00:00"
    return now


class TestInitialState:
    def test_no_run_is_not_started(self, tmp_path):
        d = _project(tmp_path)
        st = pr.get_run(d)
        assert st["state"] == "not_started"
        assert "no generation" in st["activity"].lower() or st["activity"]


class TestStart:
    def test_start_spawns_and_persists_starting(self, tmp_path):
        d = _project(tmp_path)
        sp = _Spawner()
        st = pr.start_run(d, "proj", spawn=sp, now=_clock(), gen_id=lambda: "run_abc")
        assert st["run_id"] == "run_abc"
        assert st["state"] in ("starting", "running")
        assert st["worker_pid"] == 4242
        assert sp.calls == [("proj", str(d))]
        # persisted
        again = pr.get_run(d, pid_alive=lambda p: True)
        assert again["run_id"] == "run_abc"

    def test_duplicate_start_returns_same_active_run(self, tmp_path):
        d = _project(tmp_path)
        sp = _Spawner()
        first = pr.start_run(d, "proj", spawn=sp, now=_clock(), gen_id=lambda: "run_1",
                             pid_alive=lambda p: True)
        second = pr.start_run(d, "proj", spawn=sp, now=_clock(), gen_id=lambda: "run_2",
                              pid_alive=lambda p: True)
        assert second["run_id"] == "run_1"       # same active run
        assert second.get("already_active") is True
        assert len(sp.calls) == 1                 # did NOT spawn a second worker

    def test_start_after_terminal_state_begins_new_run(self, tmp_path):
        d = _project(tmp_path)
        sp = _Spawner()
        pr.start_run(d, "proj", spawn=sp, now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        # mark completed
        run = pr.read_run(d); run["state"] = "completed"; pr._write_run(d, run)
        second = pr.start_run(d, "proj", spawn=sp, now=_clock(), gen_id=lambda: "run_2",
                              pid_alive=lambda p: True)
        assert second["run_id"] == "run_2"
        assert len(sp.calls) == 2


class TestReconcile:
    def test_orphan_running_worker_becomes_failed(self, tmp_path):
        d = _project(tmp_path)
        pr.start_run(d, "proj", spawn=_Spawner(), now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        # after a restart the worker pid is gone
        st = pr.get_run(d, pid_alive=lambda p: False)
        assert st["state"] == "failed"
        assert "reconcil" in (st.get("activity", "") + st.get("error", "")).lower()

    def test_waiting_for_approval_survives_dead_worker(self, tmp_path):
        d = _project(tmp_path)
        pr.start_run(d, "proj", spawn=_Spawner(), now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        run = pr.read_run(d); run["state"] = "waiting_for_approval"; pr._write_run(d, run)
        # waiting_for_approval is durable — a dead worker must NOT flip it to failed
        st = pr.get_run(d, pid_alive=lambda p: False)
        assert st["state"] == "waiting_for_approval"


class TestCancel:
    def test_cancel_exact_run_terminates_only_worker(self, tmp_path):
        d = _project(tmp_path)
        killed = []
        pr.start_run(d, "proj", spawn=_Spawner(pid=999), now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        st = pr.cancel_run(d, "run_1", terminate=lambda pid, **k: killed.append(pid),
                           now=_clock(), pid_alive=lambda p: False)
        assert st["state"] == "cancelled"
        assert killed == [999]                    # ONLY the worker pid
        assert st["ended_at"]

    def test_cancel_wrong_run_id_rejected(self, tmp_path):
        d = _project(tmp_path)
        pr.start_run(d, "proj", spawn=_Spawner(), now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        with pytest.raises(RunError):
            pr.cancel_run(d, "run_WRONG", terminate=lambda pid, **k: None, now=_clock())

    def test_cancel_no_active_run_rejected(self, tmp_path):
        d = _project(tmp_path)
        with pytest.raises(RunError):
            pr.cancel_run(d, "run_x", terminate=lambda pid, **k: None, now=_clock())

    def test_cancel_never_signals_a_missing_pid(self, tmp_path):
        d = _project(tmp_path)
        killed = []
        pr.start_run(d, "proj", spawn=_Spawner(pid=0), now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        # a bogus/zero pid must never be signalled
        pr.cancel_run(d, "run_1", terminate=lambda pid, **k: killed.append(pid),
                      now=_clock(), pid_alive=lambda p: False)
        assert 0 not in killed


class TestApprove:
    def _to_waiting(self, tmp_path):
        d = _project(tmp_path)
        pr.start_run(d, "proj", spawn=_Spawner(), now=_clock(), gen_id=lambda: "run_1",
                     pid_alive=lambda p: True)
        run = pr.read_run(d); run["state"] = "waiting_for_approval"; pr._write_run(d, run)
        return d

    def test_approve_records_go_ahead(self, tmp_path):
        d = self._to_waiting(tmp_path)
        st = pr.approve_plan(d, "run_1", now=_clock())
        assert st["plan_approved"] is True and st["approved_at"]
        assert "agent" in st["activity"].lower()
        # still cancellable + durable state unchanged
        assert st["state"] == "waiting_for_approval"

    def test_approve_wrong_id_rejected(self, tmp_path):
        d = self._to_waiting(tmp_path)
        with pytest.raises(RunError):
            pr.approve_plan(d, "run_WRONG", now=_clock())

    def test_approve_when_not_waiting_rejected(self, tmp_path):
        d = _project(tmp_path)  # not_started
        with pytest.raises(RunError):
            pr.approve_plan(d, "run_1", now=_clock())


class TestSanitization:
    def test_run_payload_has_no_absolute_paths(self, tmp_path):
        d = _project(tmp_path)
        st = pr.start_run(d, "proj", spawn=_Spawner(), now=_clock(), gen_id=lambda: "run_1",
                          pid_alive=lambda p: True)
        import json
        assert str(tmp_path) not in json.dumps(st)
