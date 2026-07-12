"""Production-run API: start (idempotent) / status (reconciled) / cancel (exact id)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from lib import production_run as pr


@pytest.fixture
def client(tmp_path, monkeypatch):
    # a project on disk
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "intake.json").write_text(json.dumps(
        {"project_id": "proj", "pipeline_type": "animation", "target_duration_seconds": 150}))
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", tmp_path)

    # no real subprocess / signals
    monkeypatch.setattr(pr, "_default_spawn", lambda pid_id, d: 4242)
    monkeypatch.setattr(pr, "_default_pid_alive", lambda pid: True)
    monkeypatch.setattr(pr, "_default_terminate", lambda pid, **k: None)

    async def no_watch():
        return None
    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


def _post(client, url, body=None):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=body or {}, headers={"X-OpenMontage-CSRF": token})


def test_status_not_started_before_start(client):
    r = client.get("/api/project/proj/run")
    assert r.status_code == 200
    assert r.json()["state"] == "not_started"


def test_start_then_status_running(client):
    r = _post(client, "/api/project/proj/run")
    assert r.status_code == 200
    run = r.json()
    assert run["state"] in ("starting", "running")
    assert run["worker_pid"] == 4242 and run["run_id"]
    st = client.get("/api/project/proj/run").json()
    assert st["run_id"] == run["run_id"]


def test_duplicate_start_is_idempotent(client):
    first = _post(client, "/api/project/proj/run").json()
    second = _post(client, "/api/project/proj/run").json()
    assert second["run_id"] == first["run_id"]
    assert second.get("already_active") is True


def test_cancel_wrong_run_id_409(client):
    _post(client, "/api/project/proj/run")
    r = _post(client, "/api/project/proj/run/cancel", {"run_id": "run_bogus"})
    assert r.status_code == 409


def test_cancel_exact_run_id_ok(client):
    run = _post(client, "/api/project/proj/run").json()
    r = _post(client, "/api/project/proj/run/cancel", {"run_id": run["run_id"]})
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"
    assert client.get("/api/project/proj/run").json()["state"] == "cancelled"


def test_cancel_missing_run_id_400(client):
    _post(client, "/api/project/proj/run")
    r = _post(client, "/api/project/proj/run/cancel", {})
    assert r.status_code == 400


def test_start_requires_csrf(client):
    r = client.post("/api/project/proj/run", json={})
    assert r.status_code == 403


def test_get_includes_plan_and_approve_flow(client, tmp_path):
    proj = tmp_path / "proj"
    _post(client, "/api/project/proj/run")
    # simulate the worker reaching waiting_for_approval + writing the plan
    run = pr.read_run(proj); run["state"] = "waiting_for_approval"; pr._write_run(proj, run)
    (proj / "run_plan.json").write_text(json.dumps(
        {"total_frames": 9000, "target_formatted": "5:00", "word_budget": 750,
         "provider_readiness": {"capabilities_configured": 11, "capabilities_total": 20,
                                "composition_runtimes": {"remotion": True, "hyperframes": True, "ffmpeg": True}}}))
    got = client.get("/api/project/proj/run").json()
    assert got["state"] == "waiting_for_approval"
    assert got["plan"]["total_frames"] == 9000
    # approve
    r = _post(client, "/api/project/proj/run/approve", {"run_id": run["run_id"]})
    assert r.status_code == 200 and r.json()["plan_approved"] is True
    assert client.get("/api/project/proj/run").json()["plan_approved"] is True


def test_approve_wrong_id_409(client, tmp_path):
    proj = tmp_path / "proj"
    _post(client, "/api/project/proj/run")
    run = pr.read_run(proj); run["state"] = "waiting_for_approval"; pr._write_run(proj, run)
    r = _post(client, "/api/project/proj/run/approve", {"run_id": "run_bogus"})
    assert r.status_code == 409


def test_preview_endpoint_ok(client, monkeypatch):
    from lib import preview_render
    monkeypatch.setattr(preview_render, "generate_and_record",
                        lambda d: {"ok": True, "preview_url": "/media/proj/renders/preview.mp4",
                                   "measured_seconds": 12.0})
    r = _post(client, "/api/project/proj/run/preview")
    assert r.status_code == 200 and r.json()["preview_url"].endswith("preview.mp4")


def test_preview_endpoint_failure_400(client, monkeypatch):
    from lib import preview_render
    monkeypatch.setattr(preview_render, "generate_and_record",
                        lambda d: {"ok": False, "reason": "Remotion is not render-ready."})
    r = _post(client, "/api/project/proj/run/preview")
    assert r.status_code == 400


def test_preview_endpoint_requires_csrf(client):
    r = client.post("/api/project/proj/run/preview", json={})
    assert r.status_code == 403


def _seed_timeline_with_layers(proj, secs=150):
    from lib import timeline as _tl
    tl = _tl.build_timeline({"target_duration_seconds": secs})
    tl["layers"] = [{"id": "a", "type": "text", "track": 0, "start_frame": 0,
                     "duration_frames": 900, "z": 0, "enabled": True, "locked": False, "opacity": 1.0}]
    _tl.save_timeline(proj, tl)


def test_duration_change_free_when_no_timeline(client):
    r = _post(client, "/api/project/proj/duration", {"duration": 200})
    assert r.status_code == 200 and r.json()["applied"] is True


def test_duration_change_conflict_returns_impact(client, tmp_path):
    _seed_timeline_with_layers(tmp_path / "proj")
    r = _post(client, "/api/project/proj/duration", {"duration": 30})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "strategy_required"
    assert detail["impact"]["new_frames"] == 900 and detail["impact"]["old_frames"] == 4500


def test_duration_change_with_strategy_ok(client, tmp_path):
    _seed_timeline_with_layers(tmp_path / "proj")
    r = _post(client, "/api/project/proj/duration", {"duration": 30, "strategy": "trim"})
    assert r.status_code == 200 and r.json()["strategy"] == "trim"


def test_duration_invalid_400(client):
    r = _post(client, "/api/project/proj/duration", {"duration": 0})
    assert r.status_code == 400


def test_duration_requires_csrf(client):
    r = client.post("/api/project/proj/duration", json={"duration": 120})
    assert r.status_code == 403


def test_layer_revision_queue_ok(client, tmp_path):
    from lib import timeline as _tl
    proj = tmp_path / "proj"
    tl = _tl.build_timeline({"target_duration_seconds": 60})
    tl["layers"] = [{"id": "a", "type": "image", "track": 0, "start_frame": 0,
                     "duration_frames": 90, "z": 0, "enabled": True, "locked": False, "opacity": 1.0}]
    _tl.save_timeline(proj, tl)
    r = _post(client, "/api/project/proj/timeline/revision", {"layer_id": "a", "prompt": "warmer palette"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["layer_id"] == "a"


def test_layer_revision_unknown_layer_404(client, tmp_path):
    from lib import timeline as _tl
    _tl.save_timeline(tmp_path / "proj", _tl.build_timeline({"target_duration_seconds": 60}))
    r = _post(client, "/api/project/proj/timeline/revision", {"layer_id": "nope", "prompt": "x"})
    assert r.status_code == 404


def test_layer_revision_requires_prompt_and_csrf(client, tmp_path):
    from lib import timeline as _tl
    tl = _tl.build_timeline({"target_duration_seconds": 60})
    tl["layers"] = [{"id": "a", "type": "text", "track": 0, "start_frame": 0,
                     "duration_frames": 90, "z": 0, "enabled": True, "locked": False, "opacity": 1.0}]
    _tl.save_timeline(tmp_path / "proj", tl)
    assert _post(client, "/api/project/proj/timeline/revision", {"layer_id": "a", "prompt": ""}).status_code == 400
    assert client.post("/api/project/proj/timeline/revision", json={"layer_id": "a", "prompt": "x"}).status_code == 403


def test_frame_still_ok(client, monkeypatch):
    from lib import frame_render
    monkeypatch.setattr(frame_render, "render_still",
                        lambda d, f, **k: {"ok": True, "frame": f,
                                           "url": f"/media/proj/renders/frames/frame_{f}.png",
                                           "size_bytes": 1234})
    r = _post(client, "/api/project/proj/frame", {"frame": 45})
    assert r.status_code == 200 and r.json()["url"].endswith("frame_45.png")


def test_frame_still_failure_400(client, monkeypatch):
    from lib import frame_render
    monkeypatch.setattr(frame_render, "render_still",
                        lambda d, f, **k: {"ok": False, "reason": "This project has no timeline yet."})
    r = _post(client, "/api/project/proj/frame", {"frame": 0})
    assert r.status_code == 400


def test_frame_still_requires_csrf(client):
    r = client.post("/api/project/proj/frame", json={"frame": 0})
    assert r.status_code == 403


def test_timeline_render_ok(client, monkeypatch):
    from lib import timeline_render
    monkeypatch.setattr(timeline_render, "render_timeline_preview",
                        lambda d, **k: {"ok": True, "url": "/media/proj/renders/timeline_preview.mp4",
                                        "size_bytes": 9999, "measured_seconds": 8.0,
                                        "frames_rendered": 240, "total_frames": 240, "truncated": False})
    r = _post(client, "/api/project/proj/timeline/render", {})
    assert r.status_code == 200 and r.json()["url"].endswith("timeline_preview.mp4")


def test_timeline_render_failure_400(client, monkeypatch):
    from lib import timeline_render
    monkeypatch.setattr(timeline_render, "render_timeline_preview",
                        lambda d, **k: {"ok": False, "reason": "This project has no timeline yet."})
    r = _post(client, "/api/project/proj/timeline/render", {})
    assert r.status_code == 400


def test_timeline_render_requires_csrf(client):
    r = client.post("/api/project/proj/timeline/render", json={})
    assert r.status_code == 403


def test_agent_inbox_empty(client):
    r = client.get("/api/project/proj/agent-inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0 and body["revisions"] == [] and body["approval"] is None


def test_agent_inbox_reflects_queued_revision(client, tmp_path):
    from lib import timeline as _tl
    proj = tmp_path / "proj"
    tl = _tl.build_timeline({"target_duration_seconds": 60})
    tl["layers"] = [{"id": "a", "type": "image", "track": 0, "start_frame": 0,
                     "duration_frames": 90, "z": 0, "enabled": True, "locked": False, "opacity": 1.0}]
    _tl.save_timeline(proj, tl)
    _post(client, "/api/project/proj/timeline/revision", {"layer_id": "a", "prompt": "warmer"})
    body = client.get("/api/project/proj/agent-inbox").json()
    assert body["count"] == 1 and body["revisions"][0]["layer_id"] == "a"


def test_reconcile_orphan_via_status(client, monkeypatch):
    _post(client, "/api/project/proj/run")
    # simulate the worker dying (restart): pid no longer alive
    monkeypatch.setattr(pr, "_default_pid_alive", lambda pid: False)
    st = client.get("/api/project/proj/run").json()
    assert st["state"] == "failed"
    assert "reconcil" in (st.get("activity", "") + st.get("error", "")).lower()
