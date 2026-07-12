"""Free preview-animatic render wrapper — hermetic (injected runner/doctor)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib import preview_render as pv

_PROPS = {"title": "x", "pipeline": "animation", "targetFormatted": "1:00",
         "totalFrames": 1800, "wordBudget": 150, "sections": [],
         "providersConfigured": 0, "providersTotal": 0, "runtimes": []}


def test_build_props():
    intake = {"title": "Edu", "project_id": "edu", "pipeline_type": "animated-explainer",
              "target_duration_seconds": 300}
    plan = {"provider_readiness": {"capabilities_configured": 11, "capabilities_total": 20,
            "composition_runtimes": {"remotion": True, "ffmpeg": True, "hyperframes": True}}}
    tl = {"layers": [{"type": "text"}, {"type": "image"}]}
    p = pv.build_props(intake, plan, tl)
    assert p["title"] == "Edu" and p["targetFormatted"] == "5:00" and p["totalFrames"] == 9000
    assert p["wordBudget"] == 750 and p["providersConfigured"] == 11
    assert set(p["runtimes"]) == {"remotion", "ffmpeg", "hyperframes"}
    assert p["sections"] == ["text", "image"]


def test_build_props_defaults_when_missing():
    p = pv.build_props({}, {}, {})
    assert p["title"] == "Untitled project" and p["targetFormatted"] == "1:00"  # default 60s
    assert p["totalFrames"] == 1800


def test_render_preview_success(tmp_path):
    d = tmp_path / "proj"; (d / "renders").mkdir(parents=True)
    def fake_runner(argv):
        out = [a for a in argv if a.endswith("preview.mp4")][0]
        Path(out).write_bytes(b"\x00" * 1000)  # simulate remotion writing the file
        class R: returncode = 0
        return R()
    r = pv.render_preview(d, props=_PROPS, runner=fake_runner, browser="/x/chrome",
                          doctor=lambda: {"available": True})
    assert r["ok"] is True and r["size_bytes"] == 1000
    assert r["preview_url"] == "/media/proj/renders/preview.mp4"


def test_render_preview_not_render_ready(tmp_path):
    d = tmp_path / "proj"; (d / "renders").mkdir(parents=True)
    r = pv.render_preview(d, props=_PROPS, runner=lambda a: None, browser="",
                          doctor=lambda: {"available": False, "reason": "No usable browser found."})
    assert r["ok"] is False and "browser" in r["reason"].lower()


def test_render_preview_rc_failure(tmp_path):
    d = tmp_path / "proj"; (d / "renders").mkdir(parents=True)
    class R: returncode = 1
    r = pv.render_preview(d, props=_PROPS, runner=lambda a: R(), browser="/x",
                          doctor=lambda: {"available": True})
    assert r["ok"] is False and "did not complete" in r["reason"].lower()


def test_render_preview_argv_uses_pinned_cli_not_npx(tmp_path):
    d = tmp_path / "proj"; (d / "renders").mkdir(parents=True)
    seen = {}
    def fake_runner(argv):
        seen["argv"] = argv
        out = [a for a in argv if a.endswith("preview.mp4")][0]
        Path(out).write_bytes(b"x" * 10)
        class R: returncode = 0
        return R()
    pv.render_preview(d, props=_PROPS, runner=fake_runner, browser="/ok/chrome",
                      doctor=lambda: {"available": True})
    argv = seen["argv"]
    assert "npx" not in argv
    assert argv[0].endswith("node_modules/.bin/remotion")
    assert "PreviewAnimatic" in argv
    assert "--browser-executable=/ok/chrome" in argv
