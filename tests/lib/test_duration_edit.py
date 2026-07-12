"""Duration edit-safety: changing target_duration_seconds after a timeline/approval
exists must NOT silently truncate/stretch/replan — it must surface the impact and
require an explicit trim / extend / replan strategy. A fresh project (no real
timeline, not approved) updates freely.
"""

from __future__ import annotations

import json

import pytest

from lib import duration_edit as de
from lib import timeline as _tl
from lib.duration_edit import DurationEditConflict


def _mk(tmp_path, *, secs=60, layers=None, approved=False, persist_timeline=None):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "intake.json").write_text(json.dumps(
        {"version": "1.1", "project_id": "proj", "title": "P", "brief": "b",
         "pipeline_type": "animation", "target_duration_seconds": secs}))
    if layers is not None or persist_timeline:
        tl = _tl.build_timeline({"target_duration_seconds": secs})
        tl["layers"] = layers or []
        _tl.save_timeline(d, tl)
    if approved:
        (d / "run.json").write_text(json.dumps(
            {"run_id": "run_1", "state": "waiting_for_approval", "plan_approved": True}))
    return d


class TestFreeUpdate:
    def test_fresh_project_updates_freely(self, tmp_path):
        d = _mk(tmp_path, secs=60)  # no timeline, not approved
        res = de.change_target_duration(d, 150)
        assert res["applied"] is True and res["strategy"] is None
        intake = json.loads((d / "intake.json").read_text())
        assert intake["target_duration_seconds"] == 150

    def test_empty_timeline_skeleton_updates_freely(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=[])  # timeline exists but no real content
        res = de.change_target_duration(d, 120)
        assert res["applied"] is True
        tl, _ = _tl.read_timeline(d)
        assert tl["total_frames"] == 120 * 30

    def test_accepts_mmss_and_validates_range(self, tmp_path):
        d = _mk(tmp_path)
        assert de.change_target_duration(d, "2:30")["impact"]["new_seconds"] == 150
        with pytest.raises(Exception):
            de.change_target_duration(d, 0)
        with pytest.raises(Exception):
            de.change_target_duration(d, 301)


class TestNeedsStrategy:
    def _layers(self):
        return [
            {"id": "a", "type": "text", "track": 0, "start_frame": 0, "duration_frames": 900,
             "z": 0, "enabled": True, "locked": False, "opacity": 1.0},
            {"id": "b", "type": "image", "track": 0, "start_frame": 1500, "duration_frames": 300,
             "z": 1, "enabled": True, "locked": False, "opacity": 1.0},
        ]

    def test_conflict_when_timeline_has_layers(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=self._layers())  # 1800 frames
        with pytest.raises(DurationEditConflict) as ei:
            de.change_target_duration(d, 30)  # shrink to 900 frames
        imp = ei.value.impact
        assert imp["old_frames"] == 1800 and imp["new_frames"] == 900
        assert imp["frame_delta"] == -900

    def test_conflict_when_run_approved(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=[], approved=True)
        with pytest.raises(DurationEditConflict):
            de.change_target_duration(d, 120)

    def test_trim_clamps_and_drops_layers(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=self._layers())
        res = de.change_target_duration(d, 30, strategy="trim")  # 900 frames
        assert res["strategy"] == "trim"
        tl, _ = _tl.read_timeline(d)
        assert tl["total_frames"] == 900
        by = {l["id"]: l for l in tl["layers"]}
        assert "b" not in by                       # started at 1500 > 900 → dropped
        assert by["a"]["duration_frames"] == 900   # clamped from 900 (0..900 fits exactly)

    def test_extend_keeps_layers(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=self._layers())
        res = de.change_target_duration(d, 120, strategy="extend")  # 3600 frames
        assert res["strategy"] == "extend"
        tl, _ = _tl.read_timeline(d)
        assert tl["total_frames"] == 3600
        assert len(tl["layers"]) == 2              # unchanged
        assert tl["layers"][1]["start_frame"] == 1500

    def test_replan_flags_and_preserves(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=self._layers())
        res = de.change_target_duration(d, 90, strategy="replan")
        assert res["strategy"] == "replan"
        tl, _ = _tl.read_timeline(d)
        assert tl.get("pending_replan") is True
        assert len(tl["layers"]) == 2              # preserved, not truncated

    def test_prior_timeline_versioned_on_change(self, tmp_path):
        d = _mk(tmp_path, secs=60, layers=self._layers())
        de.change_target_duration(d, 120, strategy="extend")
        assert (d / "history").exists() and any((d / "history").iterdir())
