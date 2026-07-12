"""Consolidated 'what's queued for the agent' view — honest, read-only (Rule Zero)."""

from __future__ import annotations

import pytest

from lib import agent_inbox as inbox
from lib import production_run as pr
from lib import revision_requests as rev
from lib import timeline as _tl


def _seed_timeline(proj, secs=60, layer_id="a", ltype="image"):
    proj.mkdir(parents=True, exist_ok=True)
    tl = _tl.build_timeline({"target_duration_seconds": secs})
    tl["layers"] = [{"id": layer_id, "type": ltype, "track": 0, "start_frame": 0,
                     "duration_frames": 90, "z": 0, "enabled": True, "locked": False, "opacity": 1.0}]
    _tl.save_timeline(proj, tl)
    return tl


def test_empty_project_has_nothing_queued(tmp_path):
    proj = tmp_path / "p"; proj.mkdir()
    out = inbox.pending_agent_work(proj)
    assert out["count"] == 0
    assert out["revisions"] == [] and out["replan"] is False and out["approval"] is None
    assert "nothing" in out["summary"].lower()


def test_queued_revision_appears(tmp_path):
    proj = tmp_path / "p"
    _seed_timeline(proj)
    rev.queue_revision(proj, "a", "warmer palette")
    out = inbox.pending_agent_work(proj)
    assert out["count"] == 1
    assert len(out["revisions"]) == 1
    r = out["revisions"][0]
    assert r["layer_id"] == "a" and r["prompt"] == "warmer palette" and r["id"]


def test_completed_revision_excluded(tmp_path):
    proj = tmp_path / "p"
    _seed_timeline(proj)
    rev.queue_revision(proj, "a", "x")
    # mark the on-disk request completed → no longer pending for the agent
    import json
    f = proj / rev.REVISION_FILENAME
    data = json.loads(f.read_text())
    data[0]["status"] = "completed"
    f.write_text(json.dumps(data))
    out = inbox.pending_agent_work(proj)
    assert out["count"] == 0 and out["revisions"] == []


def _set_replan(proj):
    tl, tag = _tl.read_timeline(proj)
    tl["pending_replan"] = True
    _tl.save_timeline(proj, tl, if_match=tag)


def test_pending_replan_flag_counts(tmp_path):
    proj = tmp_path / "p"
    _seed_timeline(proj)
    _set_replan(proj)
    out = inbox.pending_agent_work(proj)
    assert out["replan"] is True and out["count"] == 1
    assert "re-plan" in out["summary"].lower()


def test_waiting_for_approval_needs_user(tmp_path):
    proj = tmp_path / "p"; proj.mkdir()
    pr._write_run(proj, {"run_id": "run_x", "state": "waiting_for_approval"})
    out = inbox.pending_agent_work(proj)
    assert out["approval"]["needs"] == "user" and out["count"] == 1


def test_approved_plan_needs_agent(tmp_path):
    proj = tmp_path / "p"; proj.mkdir()
    pr._write_run(proj, {"run_id": "run_x", "state": "waiting_for_approval", "plan_approved": True})
    out = inbox.pending_agent_work(proj)
    assert out["approval"]["needs"] == "agent" and out["count"] == 1


def test_all_sources_sum(tmp_path):
    proj = tmp_path / "p"
    _seed_timeline(proj)
    rev.queue_revision(proj, "a", "one")
    _set_replan(proj)
    pr._write_run(proj, {"run_id": "run_x", "state": "waiting_for_approval", "plan_approved": True})
    out = inbox.pending_agent_work(proj)
    assert out["count"] == 3  # 1 revision + replan + approved plan
