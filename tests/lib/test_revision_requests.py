"""Layer AI-regeneration = an honest QUEUED agent revision request, never a fake
'generated'. A versioned request is appended (machine-readable for the agent) and
the target layer is marked queued; generation itself stays agent-driven.
"""

from __future__ import annotations

import json

import pytest

from lib import revision_requests as rr
from lib import timeline as _tl
from lib.revision_requests import RevisionError

_LAYER = {"id": "a", "type": "image", "track": 0, "start_frame": 0, "duration_frames": 90,
          "z": 0, "enabled": True, "locked": False, "opacity": 1.0}


def _mk(tmp_path, layers):
    d = tmp_path / "proj"; d.mkdir()
    (d / "intake.json").write_text(json.dumps({"project_id": "proj", "target_duration_seconds": 60}))
    tl = _tl.build_timeline({"target_duration_seconds": 60}); tl["layers"] = layers
    _tl.save_timeline(d, tl)
    return d


def test_queue_revision_timeline_conflict_is_409_and_leaves_no_orphan(tmp_path, monkeypatch):
    # A concurrent timeline edit (stale ETag) must surface as a clean 409 RevisionError
    # and must NOT leave an orphan request in the log (append happens only post-commit).
    d = _mk(tmp_path, [dict(_LAYER)])

    def _stale(*a, **k):
        raise _tl.TimelineError("stale etag", status=409)
    monkeypatch.setattr(_tl, "save_timeline", _stale)

    with pytest.raises(RevisionError) as ei:
        rr.queue_revision(d, "a", "warmer palette")
    assert getattr(ei.value, "status", None) == 409
    assert rr.list_revisions(d) == []  # nothing orphaned


def test_queue_revision_appends_and_marks_layer(tmp_path):
    d = _mk(tmp_path, [dict(_LAYER)])
    r = rr.queue_revision(d, "a", "make it a warm sunset", gen_id=lambda: "rev_1", now=lambda: "t0")
    assert r["id"] == "rev_1" and r["layer_id"] == "a" and r["status"] == "queued"
    assert "sunset" in r["prompt"]
    assert r["provenance"]["origin"] == "editor"
    # persisted request list
    reqs = rr.list_revisions(d)
    assert len(reqs) == 1 and reqs[0]["id"] == "rev_1"
    # the target layer is marked queued in the timeline
    tl, _ = _tl.read_timeline(d)
    L = [x for x in tl["layers"] if x["id"] == "a"][0]
    assert L["revision"]["status"] == "queued" and L["revision"]["request_id"] == "rev_1"


def test_status_is_never_completed_on_queue(tmp_path):
    d = _mk(tmp_path, [dict(_LAYER)])
    r = rr.queue_revision(d, "a", "x")
    assert r["status"] == "queued"  # never "completed"/"generated"


def test_unknown_layer_rejected(tmp_path):
    d = _mk(tmp_path, [])
    with pytest.raises(RevisionError):
        rr.queue_revision(d, "nope", "x")


def test_empty_and_oversize_prompt_rejected(tmp_path):
    d = _mk(tmp_path, [dict(_LAYER)])
    with pytest.raises(RevisionError):
        rr.queue_revision(d, "a", "   ")
    with pytest.raises(RevisionError):
        rr.queue_revision(d, "a", "x" * 4001)


def test_multiple_revisions_are_appended(tmp_path):
    d = _mk(tmp_path, [dict(_LAYER)])
    rr.queue_revision(d, "a", "one", gen_id=lambda: "r1", now=lambda: "t")
    rr.queue_revision(d, "a", "two", gen_id=lambda: "r2", now=lambda: "t")
    ids = [x["id"] for x in rr.list_revisions(d)]
    assert ids == ["r1", "r2"]


def test_request_is_machine_readable_json(tmp_path):
    d = _mk(tmp_path, [dict(_LAYER)])
    rr.queue_revision(d, "a", "x", gen_id=lambda: "r1", now=lambda: "t")
    data = json.loads((d / rr.REVISION_FILENAME).read_text())
    assert isinstance(data, list) and data[0]["layer_id"] == "a"
    assert data[0]["status"] == "queued" and "timeline_revision" in data[0]
