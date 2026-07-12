"""Canonical project-local timeline/composition artifact.

Drives lib/timeline.py: one versioned artifact consumed by BOTH the editor/Player
and the render argv. Frame-accurate (fps + total_frames derived from
target_duration_seconds), stable layer IDs, project-local media only (no traversal
/ external paths), optimistic-concurrency ETag + version history, and a
non-destructive import from legacy edit_decisions.
"""

from __future__ import annotations

import json

import pytest

from lib import timeline as tl
from lib.timeline import TimelineError


def _intake(secs=150):
    return {"project_id": "p", "title": "T", "target_duration_seconds": secs}


class TestBuild:
    def test_build_from_intake_frame_math(self):
        t = tl.build_timeline(_intake(150), fps=30)
        assert t["fps"] == 30
        assert t["target_duration_seconds"] == 150
        assert t["total_frames"] == 4500  # 150 * 30
        assert t["version"] == tl.SCHEMA_VERSION
        assert isinstance(t["layers"], list)

    @pytest.mark.parametrize("secs,frames", [(60, 1800), (150, 4500), (300, 9000)])
    def test_total_frames_examples(self, secs, frames):
        assert tl.build_timeline(_intake(secs), fps=30)["total_frames"] == frames

    def test_build_defaults_duration_when_absent(self):
        t = tl.build_timeline({"project_id": "p"}, fps=30)
        assert t["total_frames"] == 60 * 30  # documented default 60s


class TestLayerValidation:
    def _layer(self, **kw):
        base = {"id": "l1", "type": "text", "track": 0, "start_frame": 0,
                "duration_frames": 90, "z": 0, "enabled": True, "locked": False,
                "opacity": 1.0}
        base.update(kw)
        return base

    def test_valid_layer_passes(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer()]
        tl.validate_timeline(t, project_dir=tmp_path)  # no raise

    def test_duplicate_ids_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(id="x"), self._layer(id="x")]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)

    def test_negative_timing_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(start_frame=-1)]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)

    def test_non_integer_timing_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(duration_frames=1.5)]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)

    def test_non_finite_number_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(opacity=float("inf"))]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)

    def test_unknown_type_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(type="malware")]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)

    def test_project_local_source_ok(self, tmp_path):
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "a.png").write_text("x")
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(type="image", source="assets/a.png")]
        tl.validate_timeline(t, project_dir=tmp_path)

    def test_traversal_source_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(type="image", source="../../etc/passwd")]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)

    def test_absolute_external_source_rejected(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        t["layers"] = [self._layer(type="image", source="/etc/hosts")]
        with pytest.raises(TimelineError):
            tl.validate_timeline(t, project_dir=tmp_path)


class TestPersistenceAndETag:
    def test_etag_stable_and_changes(self):
        t = tl.build_timeline(_intake(60), fps=30)
        e1 = tl.etag(t)
        assert e1 == tl.etag(tl.build_timeline(_intake(60), fps=30))
        t2 = tl.build_timeline(_intake(120), fps=30)
        assert tl.etag(t2) != e1

    def test_save_read_roundtrip(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        res = tl.save_timeline(tmp_path, t)
        assert res["etag"]
        back, etag = tl.read_timeline(tmp_path)
        assert back["total_frames"] == 1800 and etag == res["etag"]

    def test_optimistic_conflict(self, tmp_path):
        t = tl.build_timeline(_intake(60), fps=30)
        first = tl.save_timeline(tmp_path, t)
        # a stale writer with the wrong if_match must be rejected
        with pytest.raises(TimelineError):
            tl.save_timeline(tmp_path, tl.build_timeline(_intake(120), fps=30),
                             if_match="stale-etag")
        # correct if_match succeeds and versions the prior
        ok = tl.save_timeline(tmp_path, tl.build_timeline(_intake(120), fps=30),
                              if_match=first["etag"])
        assert ok["etag"] != first["etag"]
        assert (tmp_path / "history").exists()


class TestImportLegacy:
    def test_import_edit_decisions_non_destructive(self):
        ed = {"version": "1.0", "render_runtime": "remotion",
              "cuts": [{"type": "text_card", "in_seconds": 0, "out_seconds": 3},
                       {"type": "image", "in_seconds": 3, "out_seconds": 8}]}
        original = json.loads(json.dumps(ed))
        t = tl.import_from_edit_decisions(ed, _intake(60), fps=30)
        assert len(t["layers"]) == 2
        assert t["layers"][0]["start_frame"] == 0
        assert t["layers"][0]["duration_frames"] == 90    # 3s * 30
        assert t["layers"][1]["start_frame"] == 90        # 3s * 30
        assert all(L["provenance"]["origin"] == "edit_decisions" for L in t["layers"])
        assert ed == original  # source untouched
