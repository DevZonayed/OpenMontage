"""Backlot timeline payload helper — build / import / save with ETag."""

from __future__ import annotations

import json

import pytest

from backlot import timeline_api
from lib import timeline as tl


def _mkproject(tmp_path, secs=150):
    d = tmp_path / "proj"
    (d / "renders").mkdir(parents=True)
    (d / "intake.json").write_text(json.dumps(
        {"project_id": "proj", "target_duration_seconds": secs}))
    return d


def test_payload_builds_from_intake(tmp_path):
    p = timeline_api.build_timeline_payload(_mkproject(tmp_path, 150))
    assert p["total_frames"] == 4500
    assert p["target_formatted"] == "2:30"
    assert p["measured_output_seconds"] is None      # no render yet
    assert p["persisted"] is False
    assert "remotion_render_ready" in p and isinstance(p["remotion_render_ready"], bool)


def test_save_then_reload_persists(tmp_path):
    d = _mkproject(tmp_path, 60)
    p = timeline_api.build_timeline_payload(d)
    res = timeline_api.save_timeline_payload(d, {"timeline": p["timeline"]})
    assert res["ok"] and res["etag"]
    assert timeline_api.build_timeline_payload(d)["persisted"] is True


def test_save_conflict_rejected(tmp_path):
    d = _mkproject(tmp_path, 60)
    p = timeline_api.build_timeline_payload(d)
    timeline_api.save_timeline_payload(d, {"timeline": p["timeline"]})
    with pytest.raises(tl.TimelineError):
        timeline_api.save_timeline_payload(d, {"timeline": p["timeline"], "if_match": "wrong"})


def test_import_from_edit_checkpoint(tmp_path):
    d = _mkproject(tmp_path, 60)
    (d / "checkpoint_edit.json").write_text(json.dumps(
        {"artifacts": {"edit_decisions": {"cuts": [
            {"type": "text_card", "in_seconds": 0, "out_seconds": 2}]}}}))
    p = timeline_api.build_timeline_payload(d)
    assert len(p["timeline"]["layers"]) == 1
    assert p["timeline"]["layers"][0]["duration_frames"] == 60  # 2s * 30


def test_bad_timeline_body_rejected(tmp_path):
    d = _mkproject(tmp_path, 60)
    with pytest.raises(tl.TimelineError):
        timeline_api.save_timeline_payload(d, {"timeline": "not-an-object"})
