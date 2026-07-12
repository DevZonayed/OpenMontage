"""target_duration_seconds is persisted on create and inferred on read."""

from __future__ import annotations

import json

import pytest

from lib import duration
from lib.project_intake import ProjectIntakeError, create_project, read_intake


def _mk(tmp_path, **kw):
    return create_project("My Film", "brief", "animation", base=tmp_path, **kw)


class TestPersist:
    def test_default_when_omitted(self, tmp_path):
        r = _mk(tmp_path, project_id="a")
        data = json.loads((tmp_path / "a" / "intake.json").read_text())
        assert data["target_duration_seconds"] == duration.DEFAULT_TARGET_SECONDS
        assert data["version"] == "1.1"

    @pytest.mark.parametrize("val,secs", [(60, 60), (150, 150), (300, 300), ("2:30", 150),
                                          ({"minutes": 5, "seconds": 0}, 300)])
    def test_accepts_forms(self, tmp_path, val, secs):
        pid = "p" + str(secs)
        _mk(tmp_path, project_id=pid, target_duration_seconds=val)
        data = json.loads((tmp_path / pid / "intake.json").read_text())
        assert data["target_duration_seconds"] == secs

    @pytest.mark.parametrize("bad", [0, 301, -5, 60.5, "abc", True, float("inf")])
    def test_rejects_invalid_with_actionable_error(self, tmp_path, bad):
        with pytest.raises(ProjectIntakeError):
            _mk(tmp_path, project_id="bad", target_duration_seconds=bad)
        # rejected create must NOT leave a workspace behind
        assert not (tmp_path / "bad").exists()


class TestReadInference:
    def test_legacy_intake_without_duration_infers_default(self, tmp_path):
        _mk(tmp_path, project_id="legacy")
        p = tmp_path / "legacy" / "intake.json"
        d = json.loads(p.read_text())
        d.pop("target_duration_seconds")  # simulate a v1.0 project
        p.write_text(json.dumps(d))
        out = read_intake(tmp_path / "legacy")
        assert out["target_duration_seconds"] == duration.DEFAULT_TARGET_SECONDS
        assert out["target_duration_inferred"] is True
        # file on disk is NOT rewritten (non-destructive)
        assert "target_duration_seconds" not in json.loads(p.read_text())

    def test_stored_value_is_not_marked_inferred(self, tmp_path):
        _mk(tmp_path, project_id="stored", target_duration_seconds=150)
        out = read_intake(tmp_path / "stored")
        assert out["target_duration_seconds"] == 150
        assert out["target_duration_inferred"] is False

    def test_corrupt_stored_value_falls_back(self, tmp_path):
        _mk(tmp_path, project_id="corrupt")
        p = tmp_path / "corrupt" / "intake.json"
        d = json.loads(p.read_text())
        d["target_duration_seconds"] = "oops"
        p.write_text(json.dumps(d))
        out = read_intake(tmp_path / "corrupt")
        assert out["target_duration_seconds"] == duration.DEFAULT_TARGET_SECONDS
        assert out["target_duration_inferred"] is True
