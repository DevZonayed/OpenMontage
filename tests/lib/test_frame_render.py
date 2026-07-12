"""Real single-frame still render of the canonical timeline (pinned Remotion CLI)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from lib import frame_render as fr
from lib import timeline as _tl


def _ok_runner(argv):
    """A fake CLI run that honors the output path in argv (still ... <output> at index 4)."""
    outp = Path(argv[4])
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _seed(proj, secs=10, layers=None):
    proj.mkdir(parents=True, exist_ok=True)
    tl = _tl.build_timeline({"target_duration_seconds": secs})
    tl["layers"] = layers if layers is not None else [
        {"id": "a", "type": "text", "track": 0, "start_frame": 0, "duration_frames": 90,
         "z": 0, "enabled": True, "locked": False, "opacity": 1.0, "text": "Hi"}]
    _tl.save_timeline(proj, tl)
    return tl


def _doctor_ok():
    return {"available": True}


def test_no_timeline_returns_not_ok(tmp_path):
    proj = tmp_path / "p"; proj.mkdir()
    res = fr.render_still(proj, 0, runner=lambda a: None, browser="/x", doctor=_doctor_ok)
    assert res["ok"] is False and "timeline" in res["reason"].lower()


def test_doctor_unavailable_blocks(tmp_path):
    proj = tmp_path / "p"
    _seed(proj)
    res = fr.render_still(proj, 0, runner=lambda a: None, browser="/x",
                          doctor=lambda: {"available": False, "reason": "no browser"})
    assert res["ok"] is False and "browser" in res["reason"].lower()


def test_success_returns_media_url(tmp_path):
    proj = tmp_path / "p"
    _seed(proj)
    res = fr.render_still(proj, 45, runner=_ok_runner, browser="/x", doctor=_doctor_ok)
    assert res["ok"] is True
    assert res["frame"] == 45
    assert res["url"] == f"/media/{proj.name}/renders/frames/frame_45.png"
    assert res["size_bytes"] > 0
    # atomic publish: the final PNG exists and no .tmp files are left behind
    assert (proj / "renders" / "frames" / "frame_45.png").is_file()
    assert not list((proj / "renders" / "frames").glob("*.tmp.png"))


def test_frame_clamped_to_timeline_bounds(tmp_path):
    proj = tmp_path / "p"
    _seed(proj, secs=10)  # 300 frames → last index 299
    captured = {}

    def run(argv):
        captured["argv"] = argv
        # find the resolved output path from argv (3rd positional after 'still' entry comp)
        outp = argv[4]
        from pathlib import Path
        Path(outp).parent.mkdir(parents=True, exist_ok=True)
        Path(outp).write_bytes(b"\x89PNG" + b"0" * 32)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    res = fr.render_still(proj, 99999, runner=run, browser="/x", doctor=_doctor_ok)
    assert res["ok"] is True
    assert res["frame"] == 299
    assert any(a == "--frame=299" for a in captured["argv"])
    assert captured["argv"][1] == "still"  # uses the still subcommand, not render


def test_negative_frame_rejected(tmp_path):
    proj = tmp_path / "p"
    _seed(proj)
    with pytest.raises(fr.FrameRenderError):
        fr.render_still(proj, -3, runner=lambda a: None, browser="/x", doctor=_doctor_ok)


def test_runner_failure_reports_not_ok(tmp_path):
    proj = tmp_path / "p"
    _seed(proj)

    def run(argv):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    res = fr.render_still(proj, 0, runner=run, browser="/x", doctor=_doctor_ok)
    assert res["ok"] is False
