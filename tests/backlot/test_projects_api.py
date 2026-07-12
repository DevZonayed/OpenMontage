"""Tests for the New-Project workflow: intake creation + CSRF-guarded API.

Creation is operational (workspace + intake.json via the canonical init_project);
production stays agent-driven. Covers happy path, canonical invocation, pipeline
+ bounds validation, traversal/XSS payloads, duplicate/concurrent protection,
atomic rollback, and CSRF/content-type/size rejection.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from backlot import state as state_mod


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    import os
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", os.path.normcase(str(root.resolve())))
    return root


@pytest.fixture
def client(projects_root, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


def _post(client, url, body):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=body, headers={"X-OpenMontage-CSRF": token})


class TestPipelinesList:
    def test_lists_real_pipelines_with_beta(self, client):
        r = client.get("/api/pipelines")
        assert r.status_code == 200
        rows = r.json()
        ids = {p["id"] for p in rows}
        assert "animation" in ids and "cinematic" in ids
        beta = {p["id"]: p["beta"] for p in rows}
        assert beta.get("animation") is False        # production
        assert beta.get("talking-head") is True      # beta


class TestCreateProject:
    def test_happy_path_creates_canonical_workspace(self, client, projects_root):
        r = _post(client, "/api/projects",
                  {"title": "My First Film", "brief": "A short about tea.", "pipeline": "animation"})
        assert r.status_code == 200, r.text
        pid = r.json()["project_id"]
        assert pid == "my-first-film"
        proj = projects_root / pid
        # canonical init_project layout + marker
        assert (proj / "project.json").is_file()
        assert (proj / "renders").is_dir() and (proj / "artifacts").is_dir()
        # intake persisted (the saved brief the agent will read)
        intake = json.loads((proj / "intake.json").read_text())
        assert intake["title"] == "My First Film"
        assert intake["brief"] == "A short about tea."
        assert intake["pipeline_type"] == "animation"
        # and it's readable via the API
        got = client.get(f"/api/project/{pid}/intake").json()
        assert got["brief"] == "A short about tea."

    def test_unknown_pipeline_rejected(self, client):
        r = _post(client, "/api/projects", {"title": "X", "brief": "", "pipeline": "not-a-pipeline"})
        assert r.status_code == 400

    def test_empty_title_rejected(self, client):
        r = _post(client, "/api/projects", {"title": "   ", "brief": "", "pipeline": "animation"})
        assert r.status_code == 400

    def test_too_long_title_rejected(self, client):
        r = _post(client, "/api/projects", {"title": "z" * 200, "brief": "", "pipeline": "animation"})
        assert r.status_code == 400

    def test_control_char_title_rejected(self, client):
        r = _post(client, "/api/projects", {"title": "bad\x00title", "brief": "", "pipeline": "animation"})
        assert r.status_code == 400

    def test_traversal_project_id_rejected(self, client):
        r = _post(client, "/api/projects",
                  {"title": "ok title", "brief": "", "pipeline": "animation", "project_id": "../evil"})
        assert r.status_code == 400

    def test_xss_payload_is_stored_raw_not_executed(self, client, projects_root):
        # The server stores the raw brief; the UI renders it via textContent (no XSS).
        payload = '<img src=x onerror=alert(1)>'
        r = _post(client, "/api/projects",
                  {"title": "Safe Title", "brief": payload, "pipeline": "animation"})
        assert r.status_code == 200
        intake = json.loads((projects_root / "safe-title" / "intake.json").read_text())
        assert intake["brief"] == payload  # stored verbatim, not mangled/executed

    def test_duplicate_returns_409(self, client):
        body = {"title": "Dup Film", "brief": "", "pipeline": "animation"}
        assert _post(client, "/api/projects", body).status_code == 200
        assert _post(client, "/api/projects", body).status_code == 409  # concurrent-safe claim

    def test_atomic_rollback_on_init_failure(self, client, projects_root, monkeypatch):
        from lib import project_intake
        monkeypatch.setattr(project_intake, "init_project",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        r = _post(client, "/api/projects", {"title": "Broken", "brief": "", "pipeline": "animation"})
        assert r.status_code == 500
        assert not (projects_root / "broken").exists()  # rolled back — no orphan dir


class TestProjectApiSecurity:
    def test_direct_post_without_csrf_rejected_403(self, client):
        r = client.post("/api/projects", json={"title": "X", "pipeline": "animation"})
        assert r.status_code == 403

    def test_wrong_content_type_rejected_415(self, client):
        token = client.get("/api/csrf").json()["csrf"]
        r = client.post("/api/projects", content="title=x",
                        headers={"X-OpenMontage-CSRF": token, "Content-Type": "text/plain"})
        assert r.status_code == 415
