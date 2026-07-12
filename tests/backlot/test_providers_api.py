"""Server + data-layer tests for the Backlot providers/settings API.

Discovery is monkeypatched to be hermetic (no real CLIs / registry), so these
assert the wiring, the catalog-based + runtime-availability validation (F4/F5),
the no-silent-swap runtime presentation, and that a preferences write can never
persist a secret, an unknown key, an off-catalog id, or an unavailable runtime.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backlot import providers_api
from backlot import server as server_mod
from lib.engines import EngineStatus, AUTH_OAUTH_SUBSCRIPTION, TEXT_PURPOSES


def _csrf_post(client, url, json=None):
    """POST with the server's process-scoped CSRF token (same-origin)."""
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=json, headers={"X-OpenMontage-CSRF": token})


def _engine(engine_id, name):
    return EngineStatus(
        id=engine_id, name=name, binary=engine_id, installed=True,
        auth_method=AUTH_OAUTH_SUBSCRIPTION, logged_in=True, subscription_backed=True,
        subscription_type="max" if engine_id == "claude" else "chatgpt",
        supported_purposes=list(TEXT_PURPOSES), image_capable=False,
        image_blocker="no image endpoint",
    )


@pytest.fixture
def fake_discovery(monkeypatch):
    """Deterministic engines + composition/media + catalog, no subprocess/registry."""
    fake_engines = [_engine("claude", "Claude Code"), _engine("codex", "Codex")]

    def fake_discover(**kwargs):
        return list(fake_engines)

    def fake_comp():
        return {
            "composition_runtimes": {"ffmpeg": True, "remotion": False, "hyperframes": True},
            "runtime_warnings": ["hyperframes: npm package `hyperframes` resolved 0.7.47"],
            "media_capabilities": [
                {"capability": "image_generation", "label": "Image Generation",
                 "configured": 1, "total": 11, "available_providers": ["flux_image"],
                 "unavailable_providers": ["openai"]},
                {"capability": "video_generation", "label": "Video Generation",
                 "configured": 1, "total": 18, "available_providers": ["pexels_video"],
                 "unavailable_providers": []},
            ],
            "degraded": False,
        }

    def fake_registry_providers(capability):
        return {
            "image_generation": ["flux_image", "pexels_image", "recraft_image"],
            "video_generation": ["kling_video", "pexels_video", "seedance"],
        }.get(capability, [])

    monkeypatch.setattr(providers_api, "discover_engines", fake_discover)
    monkeypatch.setattr(providers_api, "_composition_and_media", fake_comp)
    monkeypatch.setattr(providers_api, "_registry_providers", fake_registry_providers)


@pytest.fixture
def client(tmp_path, monkeypatch, fake_discovery):
    monkeypatch.setattr(server_mod, "PREFS_PATH", tmp_path / "providers.yaml")

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


class TestProvidersGet:
    def test_payload_shape(self, client):
        r = client.get("/api/providers?probe=0")
        assert r.status_code == 200
        body = r.json()
        assert [e["id"] for e in body["engines"]] == ["claude", "codex"]
        assert body["subscription_ready"] == ["claude", "codex"]
        assert body["image_via_subscription_supported"] is False
        # F4: catalog present for validated UI choices.
        assert set(body["catalog"]["engines"]) >= {"claude", "codex"}
        assert "flux_image" in body["catalog"]["image_providers"]
        assert "kling_video" in body["catalog"]["video_providers"]
        # F1: effective routing surfaced.
        assert set(body["effective_text_engines"]) == set(TEXT_PURPOSES)
        assert body["effective_text_engines"]["master"]["engine"] == "claude"
        # Preferences default present with all purposes + renamed field.
        assert set(body["preferences"]["purposes"]) == set(TEXT_PURPOSES)
        assert "preferred_render_runtime" in body["preferences"]

    def test_runtime_options_present_all_and_disable_unavailable(self, client):
        body = client.get("/api/providers?probe=0").json()
        opts = {o["id"]: o for o in body["render_runtime_options"]}
        assert set(opts) == {"hyperframes", "remotion", "ffmpeg"}
        assert opts["hyperframes"]["available"] is True
        assert opts["remotion"]["available"] is False
        assert opts["remotion"]["reason"]


class TestProvidersPost:
    def test_valid_save_round_trips(self, client, tmp_path):
        payload = {
            "subscription_first": False,
            "purposes": {"code": {"engine": "codex", "model": "gpt-5", "fallback": ["claude"]}},
            "image": {"provider": "flux_image", "model": "flux-pro", "fallback": ["recraft_image"]},
            "video": {"provider": None, "fallback": []},
            "preferred_render_runtime": "hyperframes",
            "authoring_mode": "atelier",
        }
        r = _csrf_post(client, "/api/providers", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["preferences"]["subscription_first"] is False
        assert body["preferences"]["preferred_render_runtime"] == "hyperframes"
        assert body["preferences"]["purposes"]["code"]["engine"] == "codex"
        assert body["preferences"]["image"]["model"] == "flux-pro"  # F4 model field persisted
        assert (tmp_path / "providers.yaml").exists()

    def test_unavailable_preferred_runtime_rejected_400(self, client, tmp_path):
        # F5: direct POST cannot save an unavailable runtime (remotion is down here).
        r = _csrf_post(client, "/api/providers", json={"preferred_render_runtime": "remotion"})
        assert r.status_code == 400
        assert "not available" in r.json()["detail"].lower()
        assert not (tmp_path / "providers.yaml").exists()

    def test_off_catalog_engine_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers", json={"purposes": {"code": {"engine": "hackerbot"}}})
        assert r.status_code == 400
        assert "not a known engine" in r.json()["detail"].lower()

    def test_off_catalog_image_provider_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers", json={"image": {"provider": "totally_unknown"}})
        assert r.status_code == 400
        assert "not a known provider" in r.json()["detail"].lower()

    def test_duplicate_fallback_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers",
                        json={"image": {"provider": "flux_image", "fallback": ["recraft_image", "recraft_image"]}})
        assert r.status_code == 400
        assert "more than once" in r.json()["detail"].lower()

    def test_primary_in_own_fallback_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers",
                        json={"video": {"provider": "kling_video", "fallback": ["kling_video"]}})
        assert r.status_code == 400
        assert "must not also be a fallback" in r.json()["detail"].lower()

    def test_secret_value_rejected_422(self, client, tmp_path):
        payload = {"purposes": {"code": {"model": "sk-ant-abcdefghijklmnopqrstuvwxyz012345"}}}
        r = _csrf_post(client, "/api/providers", json=payload)
        assert r.status_code == 422
        assert "secret" in r.json()["detail"].lower()
        assert not (tmp_path / "providers.yaml").exists()

    def test_unknown_key_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers", json={"subscription_first": True, "backdoor": 1})
        assert r.status_code == 400

    def test_invalid_render_runtime_value_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers", json={"preferred_render_runtime": "davinci"})
        assert r.status_code == 400

    def test_non_object_body_rejected(self, client):
        r = _csrf_post(client, "/api/providers", json=[1, 2, 3])
        assert r.status_code == 400


class TestEngineActionEndpoint:
    """POST /api/providers/action — safe paths only (connect is manual; a
    logout without confirm errors BEFORE any command runs, so no real logout)."""

    def test_connect_is_manual_not_success(self, client):
        r = _csrf_post(client, "/api/providers/action", json={"engine": "codex", "action": "connect"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "manual" and body["command"] == "codex login"
        assert body["ok"] is False and body["started"] is False  # D: not a fake success

    def test_unknown_engine_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers/action", json={"engine": "evil; rm -rf", "action": "status"})
        assert r.status_code == 400

    def test_unknown_action_rejected_400(self, client):
        r = _csrf_post(client, "/api/providers/action", json={"engine": "claude", "action": "sudo"})
        assert r.status_code == 400

    def test_logout_without_confirm_rejected_400(self, client):
        # Must NOT execute a real logout — the confirm guard fires first.
        r = _csrf_post(client, "/api/providers/action", json={"engine": "codex", "action": "logout"})
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"].lower()

    def test_gemini_logout_unsupported(self, client):
        r = _csrf_post(client, "/api/providers/action", json={"engine": "gemini", "action": "logout"})
        assert r.status_code == 200
        assert r.json()["supported"] is False


class TestSettingsPageServed:
    def test_settings_html_served(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Providers" in r.text


class TestRuntimeConsistencyNoSilentSwap:
    """The config surface must never offer a render_runtime the renderer or the
    edit_decisions schema would reject — binds UI ↔ schema ↔ prefs to one set."""

    def _schema_runtimes(self):
        import json
        from lib.paths import REPO_ROOT
        schema = json.loads((REPO_ROOT / "schemas/artifacts/edit_decisions.schema.json").read_text())
        return set(schema["properties"]["render_runtime"]["enum"])

    def test_ui_options_equal_schema_enum_and_prefs(self):
        from lib.provider_prefs import RENDER_RUNTIMES
        opts = providers_api._render_runtime_options(
            {"ffmpeg": True, "remotion": True, "hyperframes": True}, []
        )
        ui_ids = {o["id"] for o in opts}
        assert ui_ids == set(RENDER_RUNTIMES)
        assert ui_ids == self._schema_runtimes()

    def test_every_offered_runtime_is_accepted_by_edit_decisions_schema(self):
        from schemas.artifacts import validate_artifact
        from lib.provider_prefs import RENDER_RUNTIMES
        for rt in RENDER_RUNTIMES:
            ed = {"version": "1.0", "render_runtime": rt,
                  "cuts": [{"id": "c1", "source": "a1", "in_seconds": 0, "out_seconds": 2}]}
            validate_artifact("edit_decisions", ed)

    def test_unavailable_runtime_never_marked_available(self):
        opts = {o["id"]: o for o in providers_api._render_runtime_options(
            {"ffmpeg": True, "remotion": False, "hyperframes": False}, ["hyperframes: node too old"]
        )}
        assert opts["remotion"]["available"] is False and opts["remotion"]["reason"]
        assert opts["hyperframes"]["available"] is False
        assert "node too old" in opts["hyperframes"]["reason"]
        assert opts["ffmpeg"]["available"] is True


class TestSecurityGuards:
    """CSRF + same-origin + content-type + size protection on mutations (a direct
    cross-site or malformed POST must be rejected without side effects)."""

    def test_direct_post_without_csrf_rejected_403(self, client):
        r = client.post("/api/providers", json={"subscription_first": True})
        assert r.status_code == 403

    def test_wrong_csrf_token_rejected_403(self, client):
        r = client.post("/api/providers", json={"subscription_first": True},
                        headers={"X-OpenMontage-CSRF": "not-the-token"})
        assert r.status_code == 403

    def test_cross_origin_rejected_403(self, client):
        token = client.get("/api/csrf").json()["csrf"]
        r = client.post("/api/providers", json={"subscription_first": True},
                        headers={"X-OpenMontage-CSRF": token,
                                 "Origin": "http://evil.example", "Host": "testserver"})
        assert r.status_code == 403

    def test_wrong_content_type_rejected_415(self, client):
        token = client.get("/api/csrf").json()["csrf"]
        r = client.post("/api/providers", content="subscription_first=true",
                        headers={"X-OpenMontage-CSRF": token, "Content-Type": "text/plain"})
        assert r.status_code == 415

    def test_oversize_body_rejected_413(self, client):
        token = client.get("/api/csrf").json()["csrf"]
        big = {"blob": "x" * (17 * 1024)}
        r = client.post("/api/providers", json=big,
                        headers={"X-OpenMontage-CSRF": token})
        assert r.status_code == 413

    def test_action_endpoint_also_csrf_guarded(self, client):
        r = client.post("/api/providers/action", json={"engine": "codex", "action": "connect"})
        assert r.status_code == 403  # no token


class TestZaiCredentialEndpoint:
    """Z.AI key lifecycle over the CSRF-guarded endpoint. Fake keychain — no real
    Keychain writes, no real key. The key must never appear in any response."""

    @pytest.fixture(autouse=True)
    def zai_env(self, monkeypatch, tmp_path):
        from tests.lib.test_secret_store import FakeKeyring
        from lib import secret_store, zai_credentials as zc
        fk = FakeKeyring("keyring.backends.macOS")  # ONE shared in-memory keychain
        monkeypatch.setattr(secret_store, "_keyring", lambda: fk)
        monkeypatch.setattr(zc, "_META_PATH", tmp_path / "zai_meta.json")

    def test_store_then_status_no_leak(self, client):
        key = "z" * 44
        r = _csrf_post(client, "/api/providers/credential",
                       json={"engine": "zai", "action": "store", "api_key": key, "plan_type": "coding"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] is True and body["status"] == "stored_unverified"
        assert key not in r.text                     # key never echoed
        # status via GET payload reflects it
        payload = client.get("/api/providers?probe=0").json()
        assert payload["zai_credential"]["configured"] is True

    def test_store_rejects_bad_key(self, client):
        r = _csrf_post(client, "/api/providers/credential",
                       json={"engine": "zai", "action": "store", "api_key": "short", "plan_type": "coding"})
        assert r.status_code == 400

    def test_remove_makes_unavailable(self, client):
        _csrf_post(client, "/api/providers/credential",
                   json={"engine": "zai", "action": "store", "api_key": "z" * 44, "plan_type": "general"})
        r = _csrf_post(client, "/api/providers/credential", json={"engine": "zai", "action": "remove"})
        assert r.status_code == 200 and r.json()["configured"] is False

    def test_unknown_action_and_engine_rejected(self, client):
        assert _csrf_post(client, "/api/providers/credential",
                          json={"engine": "zai", "action": "sudo"}).status_code == 400
        assert _csrf_post(client, "/api/providers/credential",
                          json={"engine": "claude", "action": "store", "api_key": "z" * 44}).status_code == 400

    def test_launch_uses_injected_spawn_no_terminal(self):
        # Unit-level: the launch path must not open a real Terminal in tests.
        from backlot.providers_api import handle_credential
        from lib import secret_store, zai_credentials as zc
        import types
        # store a key in the fake keychain used by this test module
        # (reuse the autouse fake via a direct call path)
        calls = {"n": 0}
        # ensure a key exists
        secret_store.set_secret(zc.ACCOUNT, "z" * 44)
        r = handle_credential({"engine": "zai", "action": "launch"},
                              launcher=lambda: calls.__setitem__("n", calls["n"] + 1) or True)
        assert r["launched"] is True and calls["n"] == 1
