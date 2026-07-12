"""F6/F7: the bespoke HyperFrames ATELIER render path (hand-authored index.html).

This replaces the old ad-hoc demo script (which fabricated a pipeline and faked
human approval — a Rule Zero violation). The real production is performed by the
agent through the pipeline; this fixture only validates the render MECHANICS the
tools grew: a prebuilt (hand-authored) composition renders WITHOUT scaffolding
from cuts, and governance (no silent runtime swap) holds on the atelier path.

The actual render is expensive (npx hyperframes + headless browser); it's gated
behind OPENMONTAGE_RUN_HYPERFRAMES_RENDER=1 so `make test` stays fast.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.video.hyperframes_compose import HyperFramesCompose
from tools.video.video_compose import VideoCompose

# A minimal but contract-valid hand-authored composition.
_ATELIER_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>t</title>
<style>body{margin:0;background:#0B0D12;color:#EDEBE3;font-family:Arial}
[data-composition-id="root"]{position:relative;width:1920px;height:1080px;overflow:hidden}
.clip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
#a h1{font-size:110px;margin:0;color:#F2A900}</style>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script></head>
<body><div data-composition-id="root" data-start="0" data-duration="2" data-width="1920" data-height="1080">
<div id="a" class="clip" data-start="0" data-duration="2" data-track-index="1"><h1>atelier</h1></div>
<script>const tl=gsap.timeline({paused:true});
tl.from("#a h1",{y:40,opacity:0,duration:0.6,ease:"power4.out"},0.1);
window.__timelines["root"]=tl;</script></div></body></html>"""


class TestPrebuiltMechanics:
    def test_render_prebuilt_requires_index_html(self, tmp_path):
        ws = tmp_path / "hyperframes"
        ws.mkdir()
        r = HyperFramesCompose().execute({
            "operation": "render", "prebuilt": True,
            "workspace_path": str(ws), "output_path": str(tmp_path / "out.mp4"),
        })
        assert r.success is False
        assert "index.html" in (r.error or "")


class TestAtelierGovernance:
    def test_atelier_blocks_when_hyperframes_unavailable(self, monkeypatch, tmp_path):
        # composition_mode=atelier + render_runtime=hyperframes must route to the
        # atelier method and return a STRUCTURED BLOCKER (no silent swap) when the
        # runtime is unavailable.
        monkeypatch.setattr(VideoCompose, "_hyperframes_available", lambda self: False, raising=True)
        r = VideoCompose().execute({
            "operation": "render",
            "edit_decisions": {"version": "1.0", "render_runtime": "hyperframes",
                               "composition_mode": "atelier", "renderer_family": "animation-first", "cuts": []},
            "output_path": str(tmp_path / "final.mp4"),
            "workspace_path": str(tmp_path / "hyperframes"),
        })
        assert r.success is False
        assert "BLOCKER" in (r.error or "")
        assert "approval" in (r.error or "").lower()  # must surface + get approval, not swap

    def test_atelier_route_used_for_composition_mode(self, monkeypatch, tmp_path):
        # Prove the router dispatches to the atelier method (not the cut-based path)
        # when composition_mode=atelier, even with empty cuts.
        called = {}

        def fake_atelier(self, inputs, edit_decisions):
            called["hit"] = True
            from tools.base_tool import ToolResult
            return ToolResult(success=True, data={"routed": "atelier"})

        monkeypatch.setattr(VideoCompose, "_render_via_hyperframes_atelier", fake_atelier, raising=True)
        r = VideoCompose().execute({
            "operation": "render",
            "edit_decisions": {"version": "1.0", "render_runtime": "hyperframes",
                               "composition_mode": "atelier", "renderer_family": "animation-first", "cuts": []},
            "output_path": str(tmp_path / "final.mp4"),
        })
        assert called.get("hit") is True
        assert r.data.get("routed") == "atelier"


# The tracked, shippable example composition (F, review 3).
_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "hyperframes-atelier"


class TestTrackedExample:
    """Validate the SHIPPED example source (examples/hyperframes-atelier/) against
    the HyperFrames contract — deterministically, no render needed."""

    def _html(self) -> str:
        p = _EXAMPLE_DIR / "index.html"
        assert p.is_file(), f"tracked example missing: {p}"
        return p.read_text(encoding="utf-8")

    def test_example_files_present(self):
        assert (_EXAMPLE_DIR / "index.html").is_file()
        assert (_EXAMPLE_DIR / "art-direction.md").is_file()
        assert (_EXAMPLE_DIR / "README.md").is_file()

    def test_root_composition_contract(self):
        html = self._html()
        assert 'data-composition-id="root"' in html
        assert 'data-width="1920"' in html and 'data-height="1080"' in html
        assert 'window.__timelines["root"]' in html
        assert "gsap.timeline({ paused: true })" in html

    def test_clips_are_well_formed_and_multi_scene(self):
        import re
        html = self._html()
        clips = re.findall(r'class="clip[^"]*"[^>]*', html)
        # 5 scenes + 1 persistent HUD = >= 6 timed clips
        assert len(clips) >= 6, f"expected >=6 clips, found {len(clips)}"
        for c in clips:
            assert "data-start=" in c and "data-duration=" in c and "data-track-index=" in c

    def test_has_persistent_anchor_full_duration(self):
        # G: a persistent HUD clip spans the whole 18s on its own track so no
        # transition frame is bare.
        html = self._html()
        assert 'data-track-index="0"' in html
        assert 'data-duration="18"' in html
        assert "hud-fill" in html  # the progress anchor

    def test_no_stock_scene_types(self):
        # Atelier: must not assemble stock cut.type scene registry components.
        html = self._html().lower()
        for stock in ("data-composition-src", 'class="stat-card"', "renderer_family_map"):
            assert stock not in html


@pytest.mark.skipif(
    os.environ.get("OPENMONTAGE_RUN_HYPERFRAMES_RENDER") != "1",
    reason="real HyperFrames render is slow; set OPENMONTAGE_RUN_HYPERFRAMES_RENDER=1 to run",
)
class TestTrackedExampleRenders:
    def test_tracked_example_renders(self, tmp_path):
        out = tmp_path / "renders" / "final.mp4"
        r = VideoCompose().execute({
            "operation": "render",
            "edit_decisions": {"version": "1.0", "render_runtime": "hyperframes",
                               "composition_mode": "atelier", "renderer_family": "animation-first", "cuts": []},
            "output_path": str(out),
            "workspace_path": str(_EXAMPLE_DIR),
        })
        assert r.success, r.error
        assert out.exists() and out.stat().st_size > 100_000


@pytest.mark.skipif(
    os.environ.get("OPENMONTAGE_RUN_HYPERFRAMES_RENDER") != "1",
    reason="real HyperFrames render is slow; set OPENMONTAGE_RUN_HYPERFRAMES_RENDER=1 to run",
)
class TestAtelierRealRender:
    def test_hand_authored_composition_renders_to_mp4(self, tmp_path):
        ws = tmp_path / "hyperframes"
        ws.mkdir()
        (ws / "index.html").write_text(_ATELIER_HTML, encoding="utf-8")
        # Relative output_path must still land correctly (the tool resolves it to
        # absolute; the CLI runs with cwd=workspace).
        out = tmp_path / "renders" / "final.mp4"
        r = VideoCompose().execute({
            "operation": "render",
            "edit_decisions": {"version": "1.0", "render_runtime": "hyperframes",
                               "composition_mode": "atelier", "renderer_family": "animation-first", "cuts": []},
            "output_path": str(out),
            "workspace_path": str(ws),
        })
        assert r.success, r.error
        assert out.exists() and out.stat().st_size > 1000
