"""Full-timeline Remotion render (capped preview MP4) of the canonical timeline."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from lib import timeline as _tl
from lib import timeline_render as tr


def _seed(proj, secs=8, layers=None, title="Demo"):
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "intake.json").write_text(json.dumps(
        {"project_id": proj.name, "pipeline_type": "animation",
         "target_duration_seconds": secs, "title": title}))
    tl = _tl.build_timeline({"target_duration_seconds": secs})
    tl["layers"] = layers if layers is not None else [
        {"id": "t", "type": "text", "track": 0, "start_frame": 0, "duration_frames": secs * 30,
         "z": 1, "enabled": True, "locked": False, "opacity": 1.0, "text": "Hello"}]
    _tl.save_timeline(proj, tl)
    return tl


def _ok_runner(argv):
    # honor the output path (render <entry> <comp> <output> ...) — index 4
    outp = Path(argv[4])
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 128)
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _doc_ok():
    return {"available": True}


def test_no_timeline_not_ok(tmp_path):
    proj = tmp_path / "p"; proj.mkdir()
    res = tr.render_timeline_preview(proj, runner=_ok_runner, browser="/x", doctor=_doc_ok)
    assert res["ok"] is False and "timeline" in res["reason"].lower()


def test_empty_timeline_refuses_render(tmp_path):
    # A skeleton timeline with positive frames but ZERO layers must not render a
    # misleading blank film — even via a direct API caller bypassing the UI.
    proj = tmp_path / "p"; _seed(proj, secs=150, layers=[])
    res = tr.render_timeline_preview(proj, runner=_ok_runner, browser="/x", doctor=_doc_ok)
    assert res["ok"] is False and "layer" in res["reason"].lower()


def test_doctor_unavailable(tmp_path):
    proj = tmp_path / "p"; _seed(proj)
    res = tr.render_timeline_preview(proj, runner=_ok_runner, browser="/x",
                                     doctor=lambda: {"available": False, "reason": "no browser"})
    assert res["ok"] is False and "browser" in res["reason"].lower()


def test_success_url_and_size(tmp_path):
    proj = tmp_path / "p"; _seed(proj, secs=8)
    res = tr.render_timeline_preview(proj, runner=_ok_runner, browser="/x", doctor=_doc_ok)
    assert res["ok"] is True
    assert res["url"] == f"/media/{proj.name}/renders/timeline_preview.mp4"
    assert res["size_bytes"] > 0
    assert res["truncated"] is False  # 8s @30 = 240 frames < cap


def test_long_timeline_is_capped_and_flagged(tmp_path):
    proj = tmp_path / "p"; _seed(proj, secs=300)  # 9000 frames >> cap
    captured = {}

    def run(argv):
        captured["argv"] = argv
        return _ok_runner(argv)

    res = tr.render_timeline_preview(proj, runner=run, browser="/x", doctor=_doc_ok,
                                     max_frames=600)
    assert res["ok"] is True
    assert res["truncated"] is True
    assert res["frames_rendered"] == 600
    # a --frames=0-599 range is passed (render only the capped window)
    assert any(a == "--frames=0-599" for a in captured["argv"])
    assert captured["argv"][1] == "render"


def test_meta_props_include_title(tmp_path):
    proj = tmp_path / "p"; _seed(proj, title="Night Trains")
    captured = {}

    def run(argv):
        captured["argv"] = argv
        # read the props file passed via --props=<path>
        for a in argv:
            if a.startswith("--props="):
                captured["props"] = json.loads(Path(a.split("=", 1)[1]).read_text())
        return _ok_runner(argv)

    tr.render_timeline_preview(proj, runner=run, browser="/x", doctor=_doc_ok)
    assert captured["props"]["meta"]["title"] == "Night Trains"
    assert "timeline" in captured["props"]


def test_runner_failure(tmp_path):
    proj = tmp_path / "p"; _seed(proj)
    res = tr.render_timeline_preview(
        proj, runner=lambda a: types.SimpleNamespace(returncode=1, stdout="", stderr="x"),
        browser="/x", doctor=_doc_ok)
    assert res["ok"] is False
