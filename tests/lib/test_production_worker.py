"""The real (free) preflight/planning worker — hermetic, no subprocess/registry."""

from __future__ import annotations

import json

import pytest

from lib import production_run as pr
from lib import production_worker as pw


_FAKE_SUMMARY = {
    "composition_runtimes": {"ffmpeg": True, "remotion": True, "hyperframes": True},
    "capabilities": [
        {"capability": "tts", "configured": 0, "total": 6, "available_providers": []},
        {"capability": "video_generation", "configured": 0, "total": 18, "available_providers": []},
        {"capability": "image_generation", "configured": 0, "total": 11, "available_providers": []},
        {"capability": "music_search", "configured": 1, "total": 2, "available_providers": ["pixabay_music"]},
        {"capability": "video_post", "configured": 9, "total": 9, "available_providers": ["ffmpeg"]},
        {"capability": "subtitle", "configured": 2, "total": 2, "available_providers": ["openmontage"]},
    ],
}


def _project(tmp_path, secs=150, pipeline="animation"):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "intake.json").write_text(json.dumps(
        {"project_id": "proj", "pipeline_type": pipeline, "target_duration_seconds": secs}))
    # seed the run.json as the controller would (state=starting, pid set)
    pr._write_run(d, {"run_id": "run_1", "state": "starting", "worker_pid": 1234,
                      "project_id": "proj", "created_at": "t", "updated_at": "t"})
    return d


def test_worker_reaches_waiting_for_approval(tmp_path):
    d = _project(tmp_path, 150)
    state = pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY,
                          heartbeat=False)
    assert state == "waiting_for_approval"
    run = pr.read_run(d)
    assert run["state"] == "waiting_for_approval"
    assert run["worker_pid"] == 1234           # controller-written pid preserved
    assert "no paid generation" in run["activity"].lower()
    assert run["next_boundary"] == "provider_and_proposal_approval"


def test_worker_builds_frame_accurate_timeline_and_plan(tmp_path):
    d = _project(tmp_path, 150)
    pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY, heartbeat=False)
    tl = json.loads((d / "timeline.json").read_text())
    assert tl["total_frames"] == 4500          # 150 * 30
    plan = json.loads((d / "run_plan.json").read_text())
    assert plan["total_frames"] == 4500
    assert plan["target_formatted"] == "2:30"
    assert plan["word_budget"] == 375
    assert plan["provider_readiness"]["capabilities_configured"] == 3  # music_search+video_post+subtitle
    assert plan["provider_readiness"]["capabilities_total"] == 6
    # per-capability media breakdown so the UI can show what the models can do
    media = {m["capability"]: m for m in plan["provider_readiness"]["media_capabilities"]}
    assert media["video_generation"]["configured"] == 0 and media["video_generation"]["total"] == 18
    assert media["music_search"]["available_providers"] == ["pixabay_music"]


def test_worker_writes_activity_log(tmp_path):
    d = _project(tmp_path, 60)
    pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY, heartbeat=False)
    run = pr.read_run(d)
    log = run.get("log") or []
    assert len(log) >= 4
    joined = " ".join(e["message"] for e in log).lower()
    assert "preflight started" in joined
    assert "provider_menu_summary" in joined
    assert "waiting for your approval" in joined
    assert all("ts" in e and "message" in e for e in log)


@pytest.mark.parametrize("secs,frames", [(60, 1800), (300, 9000)])
def test_worker_duration_examples(tmp_path, secs, frames):
    d = _project(tmp_path, secs)
    pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY, heartbeat=False)
    assert json.loads((d / "run_plan.json").read_text())["total_frames"] == frames


def test_worker_fails_on_unknown_pipeline(tmp_path):
    d = _project(tmp_path, 150, pipeline="not-a-pipeline")
    state = pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY,
                          heartbeat=False)
    assert state == "failed"
    assert pr.read_run(d)["state"] == "failed"


def test_worker_stops_when_cancel_requested(tmp_path):
    d = _project(tmp_path, 150)
    # simulate the controller flipping to cancelling before the worker runs
    run = pr.read_run(d); run["state"] = "cancelling"; pr._write_run(d, run)
    state = pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY,
                          heartbeat=False)
    assert state == "cancelled"


def test_worker_heartbeat_exits_on_cancel(tmp_path):
    d = _project(tmp_path, 60)
    ticks = {"n": 0}
    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:  # flip to cancelling mid-heartbeat
            run = pr.read_run(d); run["state"] = "cancelling"; pr._write_run(d, run)
    state = pw.run_worker("proj", project_dir=d, provider_summary=lambda: _FAKE_SUMMARY,
                          heartbeat=True, max_lifetime=100, sleep=fake_sleep)
    assert state == "cancelled"
    assert ticks["n"] < 10  # exited promptly, did not spin to lifetime
