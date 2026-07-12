"""Tests for lib/provider_prefs.py — persistence, safety, and resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lib.engines import EngineStatus, AUTH_OAUTH_SUBSCRIPTION, AUTH_NONE
from lib.provider_prefs import (
    MediaSelection,
    ProviderPreferences,
    PurposeSelection,
    SecretInPreferencesError,
    TEXT_PURPOSES,
    resolve_purpose_engine,
)


def _engine(engine_id, *, logged_in, subscription_backed):
    return EngineStatus(
        id=engine_id, name=engine_id, binary=engine_id, installed=True,
        auth_method=AUTH_OAUTH_SUBSCRIPTION if subscription_backed else AUTH_NONE,
        logged_in=logged_in, subscription_backed=subscription_backed,
        supported_purposes=list(TEXT_PURPOSES),
    )


class TestModelValidation:
    def test_default_has_all_purposes(self):
        prefs = ProviderPreferences.default()
        assert set(prefs.purposes) == set(TEXT_PURPOSES)
        assert prefs.subscription_first is True

    def test_unknown_top_level_key_rejected(self):
        with pytest.raises(Exception):
            ProviderPreferences.model_validate({"subscription_first": True, "evil": 1})

    def test_unknown_purpose_rejected(self):
        with pytest.raises(Exception):
            ProviderPreferences.model_validate({"purposes": {"not_a_purpose": {}}})

    def test_invalid_render_runtime_rejected(self):
        with pytest.raises(Exception):
            ProviderPreferences.model_validate({"preferred_render_runtime": "davinci"})

    def test_old_render_runtime_key_rejected(self):
        # Renamed to preferred_render_runtime; the old key is now unknown.
        with pytest.raises(Exception):
            ProviderPreferences.model_validate({"render_runtime": "hyperframes"})

    def test_valid_render_runtime_accepted(self):
        prefs = ProviderPreferences.model_validate({"preferred_render_runtime": "hyperframes"})
        assert prefs.preferred_render_runtime == "hyperframes"

    def test_invalid_authoring_mode_rejected(self):
        with pytest.raises(Exception):
            ProviderPreferences.model_validate({"authoring_mode": "freestyle"})

    def test_purpose_extra_key_rejected(self):
        with pytest.raises(Exception):
            PurposeSelection.model_validate({"engine": "claude", "secret": "x"})


class TestSecretGuard:
    # pydantic wraps the SecretInPreferencesError (a ValueError) into a
    # ValidationError; the server pattern-matches the "secret" message to return
    # a tailored 400. We assert both the wrapping and the message.
    def test_openai_style_key_rejected(self):
        with pytest.raises(ValidationError, match="secret"):
            PurposeSelection(model="sk-proj-abcdefghijklmnop0123456789")

    def test_anthropic_key_rejected_in_engine(self):
        with pytest.raises(ValidationError, match="secret"):
            PurposeSelection(engine="sk-ant-abcdefghijklmnopqrstuvwx")

    def test_long_opaque_token_rejected_in_fallback(self):
        with pytest.raises(ValidationError, match="secret"):
            PurposeSelection(fallback=["claude", "a" * 48])

    def test_google_key_rejected_in_media(self):
        with pytest.raises(ValidationError, match="secret"):
            MediaSelection(provider="AIzaSyA1234567890abcdefghijklmnopqrstuv")

    def test_secret_error_is_valueerror_subclass(self):
        # The underlying guard raises a typed error usable outside pydantic.
        from lib.provider_prefs import _reject_secrets
        with pytest.raises(SecretInPreferencesError):
            _reject_secrets("sk-ant-abcdefghijklmnopqrstuvwx", "engine")

    def test_normal_names_allowed(self):
        sel = PurposeSelection(engine="claude", model="opus", fallback=["codex", "gemini"])
        assert sel.engine == "claude"


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "providers.yaml"
        prefs = ProviderPreferences.default()
        prefs.subscription_first = False
        prefs.purposes["code"] = PurposeSelection(engine="codex", fallback=["claude"])
        prefs.preferred_render_runtime = "hyperframes"
        prefs.authoring_mode = "atelier"
        prefs.save(path)
        assert path.exists()

        loaded = ProviderPreferences.load(path)
        assert loaded.subscription_first is False
        assert loaded.purposes["code"].engine == "codex"
        assert loaded.purposes["code"].fallback == ["claude"]
        assert loaded.preferred_render_runtime == "hyperframes"
        assert loaded.authoring_mode == "atelier"
        # Missing file -> defaults
        assert ProviderPreferences.load(tmp_path / "absent.yaml").subscription_first is True

    def test_saved_file_contains_no_secret(self, tmp_path):
        path = tmp_path / "providers.yaml"
        ProviderPreferences.default().save(path)
        text = path.read_text()
        assert "sk-" not in text
        assert "API_KEY" not in text


class TestResolution:
    def test_explicit_selection_used_when_available(self):
        prefs = ProviderPreferences.default()
        prefs.purposes["code"] = PurposeSelection(engine="codex")
        engines = [_engine("claude", logged_in=True, subscription_backed=True),
                   _engine("codex", logged_in=True, subscription_backed=True)]
        r = resolve_purpose_engine(prefs, engines, "code")
        assert r["engine"] == "codex"
        assert "explicit" in r["reason"]

    def test_fallback_when_primary_unavailable(self):
        prefs = ProviderPreferences.default()
        prefs.purposes["code"] = PurposeSelection(engine="gemini", fallback=["claude"])
        engines = [_engine("claude", logged_in=True, subscription_backed=True),
                   _engine("gemini", logged_in=False, subscription_backed=False)]
        r = resolve_purpose_engine(prefs, engines, "code")
        assert r["engine"] == "claude"
        assert "fell back" in r["reason"]

    def test_subscription_first_picks_ready_engine(self):
        prefs = ProviderPreferences.default()  # no explicit selection
        engines = [_engine("claude", logged_in=True, subscription_backed=True),
                   _engine("codex", logged_in=True, subscription_backed=True)]
        r = resolve_purpose_engine(prefs, engines, "master")
        assert r["engine"] == "claude"  # deterministic order

    def test_subscription_first_off_yields_none_without_selection(self):
        prefs = ProviderPreferences.default()
        prefs.subscription_first = False
        engines = [_engine("claude", logged_in=True, subscription_backed=True)]
        r = resolve_purpose_engine(prefs, engines, "master")
        assert r["engine"] is None
        assert "no configured engine" in r["reason"]

    def test_no_engine_available_reports_honestly(self):
        prefs = ProviderPreferences.default()
        prefs.purposes["script"] = PurposeSelection(engine="claude")
        engines = [_engine("claude", logged_in=False, subscription_backed=False)]
        r = resolve_purpose_engine(prefs, engines, "script")
        assert r["engine"] is None
