"""Operational media-parity: every TimelineFrame CLI render call site must feed the
composition a trusted meta {projectId, assetBaseUrl} so a project-local `source`
resolves to the same /media URL the Player uses (not a placeholder). These tests
capture the REAL props file passed to the pinned CLI argv and prove the active
server port is wired into the render endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from lib import frame_render as fr
from lib import timeline_render as tr
from lib.render_meta import RenderBaseError, build_render_meta, resolve_render_base_url

_TIMELINE = {
    "version": "1.0",
    "fps": 30,
    "target_duration_seconds": 5,
    "total_frames": 150,
    "layers": [
        {"id": "bg", "type": "image", "track": 0, "start_frame": 0,
         "duration_frames": 150, "z": 0, "enabled": True, "opacity": 1,
         "source": "assets/images/hero.png"},
    ],
}


# ── the trusted-base resolver ──
def test_resolve_base_precedence_and_loopback():
    assert resolve_render_base_url(port=4750) == "http://127.0.0.1:4750"
    assert resolve_render_base_url(env={"BACKLOT_PORT": "4761"}) == "http://127.0.0.1:4761"
    assert resolve_render_base_url(base_url="https://media.example.com") == "https://media.example.com"


@pytest.mark.parametrize("bad", [
    "http://evil.com",             # non-loopback http
    "http://10.0.0.5:4750",        # non-loopback http
    "ftp://x",                     # bad scheme
    "http://127.0.0.1/media/x",    # path not allowed
    "http://127.0.0.1:4750?q=1",   # query not allowed
])
def test_resolve_base_rejects_untrusted(bad):
    with pytest.raises(RenderBaseError):
        resolve_render_base_url(base_url=bad)


def test_build_render_meta_has_projectid_and_base(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    meta = build_render_meta(proj, port=4753)
    assert meta["projectId"] == "myproj"
    assert meta["assetBaseUrl"] == "http://127.0.0.1:4753"


# ── capture the REAL props file handed to the pinned CLI argv ──
def _capturing_runner(store):
    def runner(argv):
        props_path = next(a.split("=", 1)[1] for a in argv if a.startswith("--props="))
        store["props"] = json.loads(Path(props_path).read_text())
        store["argv"] = list(argv)
        Path(argv[4]).write_bytes(b"\x89PNG\r\n\x1a\n")  # non-empty output → ok
        class _P:  # noqa: N801
            returncode = 0
        return _P()
    return runner


def test_frame_render_props_carry_parity_meta(tmp_path):
    proj = tmp_path / "frameproj"
    proj.mkdir()
    store: dict = {}
    res = fr.render_still(
        proj, 3, timeline=_TIMELINE, runner=_capturing_runner(store),
        browser="/usr/bin/true", base_url="http://127.0.0.1:4753",
        doctor=lambda: {"available": True},
    )
    assert res["ok"] is True
    meta = store["props"]["meta"]
    assert meta["projectId"] == "frameproj"
    assert meta["assetBaseUrl"] == "http://127.0.0.1:4753"
    # the project-local source is preserved verbatim in the props the CLI renders
    assert store["props"]["timeline"]["layers"][0]["source"] == "assets/images/hero.png"


def test_timeline_render_props_carry_parity_meta(tmp_path):
    proj = tmp_path / "tlproj"
    proj.mkdir()
    store: dict = {}
    res = tr.render_timeline_preview(
        proj, timeline=_TIMELINE, runner=_capturing_runner(store),
        browser="/usr/bin/true", base_url="http://127.0.0.1:4753",
        doctor=lambda: {"available": True},
    )
    assert res["ok"] is True
    meta = store["props"]["meta"]
    assert meta["projectId"] == "tlproj"
    assert meta["assetBaseUrl"] == "http://127.0.0.1:4753"


# ── the ACTIVE server port is wired into the real render endpoints ──
def _mk_project(tmp_path):
    proj = tmp_path / "rproj"
    proj.mkdir()
    (proj / "intake.json").write_text(json.dumps(
        {"project_id": "rproj", "pipeline_type": "animation", "target_duration_seconds": 5}))
    return proj


def test_frame_route_passes_active_port(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_PORT", "4753")  # the operator-bound port
    _mk_project(tmp_path)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", tmp_path)

    async def no_watch():
        return None
    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)

    captured: dict = {}

    def fake_still(project_dir, frame, *, base_url=None, **kw):
        captured["base_url"] = base_url
        return {"ok": True, "frame": frame, "url": "/media/rproj/x.png", "size_bytes": 3}
    monkeypatch.setattr(fr, "render_still", fake_still)

    with TestClient(server_mod.create_app()) as c:
        token = c.get("/api/csrf").json()["csrf"]
        r = c.post("/api/project/rproj/frame", json={"frame": 0},
                   headers={"X-OpenMontage-CSRF": token})
    assert r.status_code == 200
    assert captured["base_url"] == "http://127.0.0.1:4753"


def test_timeline_route_passes_active_port(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_PORT", "4753")
    _mk_project(tmp_path)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", tmp_path)

    async def no_watch():
        return None
    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)

    captured: dict = {}

    def fake_preview(project_dir, *, base_url=None, **kw):
        captured["base_url"] = base_url
        return {"ok": True, "url": "/media/rproj/renders/timeline_preview.mp4"}
    monkeypatch.setattr(tr, "render_timeline_preview", fake_preview)

    with TestClient(server_mod.create_app()) as c:
        token = c.get("/api/csrf").json()["csrf"]
        r = c.post("/api/project/rproj/timeline/render", json={},
                   headers={"X-OpenMontage-CSRF": token})
    assert r.status_code == 200
    assert captured["base_url"] == "http://127.0.0.1:4753"
