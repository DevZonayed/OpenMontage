"""F1/B/C: saved provider preferences are actually consumed by the selectors.

Uses real mock provider tools to prove: a saved provider reaches execution,
primary -> ordered fallback -> auto is deterministic, an explicit per-call
override wins, the saved MODEL reaches the provider call (and an unsupported
model is surfaced honestly, not silently claimed).
"""

from __future__ import annotations

import pytest

from lib import provider_prefs as pp
from lib.provider_prefs import (
    MediaSelection,
    ProviderPreferences,
    PurposeSelection,
    effective_text_engines,
    resolve_media_selection,
)
from lib.engines import EngineStatus, AUTH_OAUTH_SUBSCRIPTION, TEXT_PURPOSES
from tools.base_tool import ToolResult, ToolStatus


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    monkeypatch.setattr(pp, "DEFAULT_PREFS_PATH", path)
    return path


def _save(path, **kw):
    prefs = ProviderPreferences.default()
    for k, v in kw.items():
        setattr(prefs, k, v)
    prefs.save(path)


class FakeTool:
    """Minimal provider tool the selectors can route to + execute."""

    def __init__(self, name, provider, capability, *, available=True, model_param=True):
        self.name = name
        self.provider = provider
        self.capability = capability
        self.best_for = []
        self.supports = {}
        self._available = available
        props = {"prompt": {}, "query": {}}
        if model_param:
            props["model"] = {}
        self.input_schema = {"properties": props}
        self.executed_with = None

    def get_status(self):
        return ToolStatus.AVAILABLE if self._available else ToolStatus.UNAVAILABLE

    def get_info(self):
        return {"agent_skills": [], "usage_location": "fake", "best_for": []}

    def estimate_cost(self, inputs):
        return 0.0

    def execute(self, inputs):
        self.executed_with = dict(inputs)
        return ToolResult(success=True, data={})


# ------------------------------- resolve hint -------------------------------

class TestResolveMediaSelection:
    def test_saved_preference_applied_when_auto(self, prefs_file):
        _save(prefs_file, image=MediaSelection(provider="flux_image", model="flux-pro", fallback=["recraft_image"]))
        r = resolve_media_selection("image_generation", {"preferred_provider": "auto"})
        assert r["preferred_provider"] == "flux_image"
        assert r["model"] == "flux-pro"
        assert r["source"] == "saved_preference"

    def test_explicit_request_overrides_saved(self, prefs_file):
        _save(prefs_file, image=MediaSelection(provider="flux_image"))
        r = resolve_media_selection("image_generation", {"preferred_provider": "gpt_image"})
        assert r["preferred_provider"] == "gpt_image"
        assert r["source"] == "explicit"


# --------------------- deterministic selection (C) + model (B) ---------------------

class TestImageSelectorDeterministic:
    def _selector(self, monkeypatch, tools):
        from tools.graphics.image_selector import ImageSelector
        sel = ImageSelector()
        monkeypatch.setattr(sel, "_providers", lambda: tools)
        # keep candidate filtering a no-op so the fakes survive
        monkeypatch.setattr(sel, "_filter_candidates", lambda inputs, cands: list(cands))
        return sel

    def _tools(self):
        cap = "image_generation"
        return [FakeTool("flux_image", "flux", cap), FakeTool("recraft_image", "recraft", cap),
                FakeTool("gpt_image", "openai", cap, model_param=False)]

    def test_saved_primary_selected(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="recraft"))
        sel = self._selector(monkeypatch, self._tools())
        r = sel.execute({"prompt": "a cat"})
        assert r.data["selected_provider"] == "recraft"
        assert r.data["preference_source"] == "saved_preference:primary"

    def test_first_fallback_when_primary_unavailable(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="not_present", fallback=["recraft", "openai"]))
        sel = self._selector(monkeypatch, self._tools())
        r = sel.execute({"prompt": "x"})
        assert r.data["selected_provider"] == "recraft"
        assert r.data["preference_source"] == "saved_preference:fallback:recraft"

    def test_second_fallback_used_in_order(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="not_present", fallback=["also_missing", "openai"]))
        sel = self._selector(monkeypatch, self._tools())
        r = sel.execute({"prompt": "x"})
        assert r.data["selected_provider"] == "openai"
        assert r.data["preference_source"] == "saved_preference:fallback:openai"

    def test_falls_to_auto_when_all_saved_unavailable(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="nope", fallback=["also_nope"]))
        sel = self._selector(monkeypatch, self._tools())
        # deterministic saved pick fails -> auto path; stub scoring to a known tool
        tools = self._tools()
        monkeypatch.setattr(sel, "_select_best_tool", lambda i, c, x: (tools[0], None))
        r = sel.execute({"prompt": "x"})
        assert r.data["preference_source"] == "auto"

    def test_explicit_override_wins(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="recraft"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        monkeypatch.setattr(sel, "_select_best_tool", lambda i, c, x: (tools[2], None))
        r = sel.execute({"prompt": "x", "preferred_provider": "openai"})
        assert r.data["preference_source"] == "explicit"
        assert r.data["selected_provider"] == "openai"

    def test_saved_model_reaches_execution(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="flux", model="flux-pro-1.1"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        r = sel.execute({"prompt": "x"})
        flux = next(t for t in tools if t.provider == "flux")
        assert flux.executed_with.get("model") == "flux-pro-1.1"       # reached the provider
        assert r.data["selected_model"] == "flux-pro-1.1"

    def test_explicit_model_wins_over_saved(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="flux", model="saved-model"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        sel.execute({"prompt": "x", "model": "caller-model"})
        flux = next(t for t in tools if t.provider == "flux")
        assert flux.executed_with.get("model") == "caller-model"

    def test_unsupported_model_surfaced_not_claimed(self, prefs_file, monkeypatch):
        # openai fake has no model param; a saved model must NOT be silently claimed.
        _save(prefs_file, image=MediaSelection(provider="openai", model="dall-e-3"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        r = sel.execute({"prompt": "x"})
        openai = next(t for t in tools if t.provider == "openai")
        assert "model" not in openai.executed_with
        assert r.data.get("model_preference_unsupported") is True
        assert "selected_model" not in r.data

    # ---- review-4 #1: saved model belongs to the saved PRIMARY only ----

    def test_saved_model_not_injected_into_explicit_other_provider(self, prefs_file, monkeypatch):
        # Saved primary=flux with a model; caller explicitly asks for recraft (no
        # explicit model). The saved model must NOT land on recraft.
        _save(prefs_file, image=MediaSelection(provider="flux", model="flux-pro"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        r = sel.execute({"prompt": "x", "preferred_provider": "recraft"})
        recraft = next(t for t in tools if t.provider == "recraft")
        assert r.data["selected_provider"] == "recraft"
        assert "model" not in recraft.executed_with          # NOT the flux model
        assert "selected_model" not in r.data
        assert r.data.get("saved_model_not_applied") is True

    def test_saved_model_not_injected_when_auto_after_unavailable(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="nope", fallback=["also_nope"], model="flux-pro"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        monkeypatch.setattr(sel, "_select_best_tool", lambda i, c, x: (tools[0], None))  # flux via auto
        r = sel.execute({"prompt": "x"})
        assert r.data["preference_source"] == "auto"
        assert "model" not in tools[0].executed_with         # auto pick doesn't get primary's model
        assert r.data.get("saved_model_not_applied") is True

    def test_saved_model_not_injected_into_fallback(self, prefs_file, monkeypatch):
        _save(prefs_file, image=MediaSelection(provider="not_present", fallback=["recraft"], model="flux-pro"))
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        r = sel.execute({"prompt": "x"})
        recraft = next(t for t in tools if t.provider == "recraft")
        assert r.data["preference_source"] == "saved_preference:fallback:recraft"
        assert "model" not in recraft.executed_with          # fallback doesn't get primary's model
        assert r.data.get("saved_model_not_applied") is True

    # ---- review-4 #2: explicit means explicit (strict, no scoring override) ----

    def test_explicit_is_strict_even_if_scoring_would_prefer_another(self, prefs_file, monkeypatch):
        # All three providers are AVAILABLE. We do NOT mock _select_best_tool — the
        # strict path must pick the requested provider outright, regardless of any
        # score the auto path would compute.
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        called = {"n": 0}
        orig = sel._select_best_tool
        def spy(i, c, x):
            called["n"] += 1
            return orig(i, c, x)
        monkeypatch.setattr(sel, "_select_best_tool", spy)
        r = sel.execute({"prompt": "x", "preferred_provider": "recraft"})
        assert r.data["selected_provider"] == "recraft"
        assert r.data["preference_source"] == "explicit"
        assert called["n"] == 0  # strict path never deferred to scoring

    def test_explicit_unavailable_reports_auto_not_explicit(self, prefs_file, monkeypatch):
        tools = self._tools()
        sel = self._selector(monkeypatch, tools)
        monkeypatch.setattr(sel, "_select_best_tool", lambda i, c, x: (tools[0], None))
        r = sel.execute({"prompt": "x", "preferred_provider": "does_not_exist"})
        assert r.data["preference_source"] == "auto"          # never "explicit" for a different provider
        assert r.data.get("requested_provider_unavailable") == "does_not_exist"


class TestVideoSelectorDeterministic:
    def test_saved_primary_and_model(self, prefs_file, monkeypatch):
        _save(prefs_file, video=MediaSelection(provider="kling", model="kling-2.1"))
        from tools.video.video_selector import VideoSelector
        cap = "video_generation"
        tools = [FakeTool("kling_video", "kling", cap), FakeTool("veo_video", "veo", cap)]
        sel = VideoSelector()
        monkeypatch.setattr(sel, "_providers", lambda: tools)
        monkeypatch.setattr(sel, "_filter_candidates", lambda inputs, cands: list(cands))
        r = sel.execute({"prompt": "a rocket", "operation": "text_to_video"})
        assert r.data["selected_provider"] == "kling"
        assert r.data["preference_source"] == "saved_preference:primary"
        kling = tools[0]
        assert kling.executed_with.get("model") == "kling-2.1"
        assert r.data["selected_model"] == "kling-2.1"

    def test_saved_model_not_injected_into_explicit_other(self, prefs_file, monkeypatch):
        _save(prefs_file, video=MediaSelection(provider="kling", model="kling-2.1"))
        from tools.video.video_selector import VideoSelector
        cap = "video_generation"
        tools = [FakeTool("kling_video", "kling", cap), FakeTool("veo_video", "veo", cap)]
        sel = VideoSelector()
        monkeypatch.setattr(sel, "_providers", lambda: tools)
        monkeypatch.setattr(sel, "_filter_candidates", lambda inputs, cands: list(cands))
        r = sel.execute({"prompt": "x", "operation": "text_to_video", "preferred_provider": "veo"})
        veo = tools[1]
        assert r.data["selected_provider"] == "veo" and r.data["preference_source"] == "explicit"
        assert "model" not in veo.executed_with                 # kling's model must not land on veo
        assert r.data.get("saved_model_not_applied") is True


class TestEffectiveTextEngines:
    def test_routes_each_purpose(self):
        prefs = ProviderPreferences.default()
        prefs.purposes["code"] = PurposeSelection(engine="codex")
        engines = [
            EngineStatus(id="claude", name="c", binary="claude", installed=True,
                         auth_method=AUTH_OAUTH_SUBSCRIPTION, logged_in=True,
                         subscription_backed=True, supported_purposes=list(TEXT_PURPOSES)),
            EngineStatus(id="codex", name="x", binary="codex", installed=True,
                         auth_method=AUTH_OAUTH_SUBSCRIPTION, logged_in=True,
                         subscription_backed=True, supported_purposes=list(TEXT_PURPOSES)),
        ]
        routing = effective_text_engines(prefs, engines)
        assert set(routing) == set(TEXT_PURPOSES)
        assert routing["code"]["engine"] == "codex"
        assert routing["master"]["engine"] == "claude"


class TestPreflightSurfacesPreferences:
    def test_provider_menu_summary_reflects_saved_prefs(self, prefs_file):
        _save(prefs_file, subscription_first=False, image=MediaSelection(provider="pexels_image"))
        from tools.tool_registry import registry
        registry.discover()
        s = registry.provider_menu_summary()
        prefs = s["provider_preferences"]
        assert prefs["subscription_first"] is False
        assert prefs["image"]["provider"] == "pexels_image"
