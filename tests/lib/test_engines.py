"""Unit + contract tests for lib/engines.py (subscription-engine discovery).

These use injectable ``runner``/``which`` so no real CLI is required and every
install/auth combination is deterministic. The overriding contract under test:
detection is honest, and NO secret ever appears in the output.
"""

from __future__ import annotations

import json

import pytest

from lib import engines
from lib.engines import (
    AUTH_API_KEY,
    AUTH_NONE,
    AUTH_NOT_INSTALLED,
    AUTH_OAUTH_SUBSCRIPTION,
    AUTH_UNKNOWN,
    ProbeResult,
    discover_engines,
    engines_summary,
)


def _which_none(_name):
    return None


def _which_all(name):
    return f"/usr/bin/{name}"


def _runner_map(mapping):
    """Build a runner that returns a canned ProbeResult keyed by argv[0]."""
    def run(cmd, timeout):
        return mapping.get(cmd[0], ProbeResult(False, -1, "", "no stub"))
    return run


class TestClaudeDetection:
    def test_not_installed(self):
        [claude] = discover_engines(only=["claude"], which=_which_none, runner=_runner_map({}))
        assert claude.installed is False
        assert claude.auth_method == AUTH_NOT_INSTALLED
        assert claude.logged_in is False
        assert claude.subscription_backed is False
        assert claude.blockers  # explains what to install

    def test_logged_in_max_plan_is_subscription_backed(self):
        payload = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
            # Identity fields the CLI emits — MUST be dropped:
            "email": "secret@example.com",
            "orgId": "org-should-not-leak",
        }
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.installed is True
        assert claude.auth_method == AUTH_OAUTH_SUBSCRIPTION
        assert claude.logged_in is True
        assert claude.subscription_backed is True
        assert claude.subscription_type == "max"
        # Identity fields never surface.
        blob = json.dumps(claude.to_dict())
        assert "secret@example.com" not in blob
        assert "org-should-not-leak" not in blob

    def test_api_key_auth_is_not_subscription_backed(self):
        payload = {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "console"}
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.auth_method == AUTH_API_KEY
        assert claude.subscription_backed is False

    def test_installed_but_logged_out(self):
        payload = {"loggedIn": False}
        runner = _runner_map({"claude": ProbeResult(True, 1, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.installed is True
        assert claude.logged_in is False
        assert claude.subscription_backed is False
        assert any("not logged in" in b for b in claude.blockers)

    def test_unknown_auth_method_fails_closed(self):
        # F3: logged in via an unrecognized authMethod must NOT be subscription-backed.
        payload = {"loggedIn": True, "authMethod": "sso-enterprise-mystery"}
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.logged_in is True
        assert claude.auth_method == AUTH_UNKNOWN
        assert claude.subscription_backed is False

    def test_returncode0_garbage_fails_closed(self):
        # A (review 3): exit 0 with non-JSON garbage must NOT be subscription.
        runner = _runner_map({"claude": ProbeResult(True, 0, "welcome to claude!\n", "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.subscription_backed is False
        assert claude.logged_in is False           # garbage exit-0 is not proof of a session
        assert claude.auth_method == AUTH_UNKNOWN

    def test_malformed_json_fails_closed(self):
        runner = _runner_map({"claude": ProbeResult(True, 0, '{"loggedIn": true, "authMethod":', "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.subscription_backed is False
        assert claude.auth_method == AUTH_UNKNOWN

    def test_json_missing_auth_method_fails_closed(self):
        payload = {"loggedIn": True}  # no authMethod / apiProvider
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.logged_in is True
        assert claude.auth_method == AUTH_UNKNOWN
        assert claude.subscription_backed is False

    def test_explicit_claudeai_is_subscription(self):
        payload = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"}
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.auth_method == AUTH_OAUTH_SUBSCRIPTION and claude.subscription_backed is True

    def test_apikey_firstparty_is_not_subscription(self):
        # review-4 #3: apiKey authMethod with apiProvider=firstParty must be an API
        # key, NOT OAuth (api-key detection runs before claude.ai; firstParty alone
        # is not consumer-OAuth evidence).
        payload = {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"}
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.auth_method == AUTH_API_KEY
        assert claude.subscription_backed is False

    def test_firstparty_alone_is_not_subscription(self):
        # apiProvider=firstParty with an unknown authMethod is NOT enough evidence.
        payload = {"loggedIn": True, "authMethod": "mystery", "apiProvider": "firstParty"}
        runner = _runner_map({"claude": ProbeResult(True, 0, json.dumps(payload), "")})
        [claude] = discover_engines(only=["claude"], which=_which_all, runner=runner)
        assert claude.auth_method == AUTH_UNKNOWN
        assert claude.subscription_backed is False


class TestCodexDetection:
    def test_chatgpt_oauth(self):
        runner = _runner_map({"codex": ProbeResult(True, 0, "Logged in using ChatGPT", "")})
        [codex] = discover_engines(only=["codex"], which=_which_all, runner=runner)
        assert codex.auth_method == AUTH_OAUTH_SUBSCRIPTION
        assert codex.subscription_backed is True
        assert codex.subscription_type == "chatgpt"

    def test_api_key_login(self):
        runner = _runner_map({"codex": ProbeResult(True, 0, "Logged in using an API key", "")})
        [codex] = discover_engines(only=["codex"], which=_which_all, runner=runner)
        assert codex.auth_method == AUTH_API_KEY
        assert codex.subscription_backed is False

    def test_logged_out(self):
        runner = _runner_map({"codex": ProbeResult(True, 1, "Not logged in", "")})
        [codex] = discover_engines(only=["codex"], which=_which_all, runner=runner)
        assert codex.logged_in is False
        assert codex.subscription_backed is False

    def test_generic_logged_in_fails_closed(self):
        # F3: a generic "logged in" line without "ChatGPT" must NOT be treated
        # as a verified subscription.
        runner = _runner_map({"codex": ProbeResult(True, 0, "Logged in", "")})
        [codex] = discover_engines(only=["codex"], which=_which_all, runner=runner)
        assert codex.logged_in is True
        assert codex.auth_method == AUTH_UNKNOWN
        assert codex.subscription_backed is False

    @pytest.mark.parametrize("text", [
        "Not logged in using ChatGPT",   # review-4 #3: contains chatgpt + "logged in" substring
        "Logged out",
        "Not signed in",
        "Please log in with ChatGPT",
    ])
    def test_negative_forms_are_not_logged_in(self, text):
        runner = _runner_map({"codex": ProbeResult(True, 1, text, "")})
        [codex] = discover_engines(only=["codex"], which=_which_all, runner=runner)
        assert codex.logged_in is False
        assert codex.subscription_backed is False

    def test_exact_positive_chatgpt(self):
        runner = _runner_map({"codex": ProbeResult(True, 0, "Logged in using ChatGPT", "")})
        [codex] = discover_engines(only=["codex"], which=_which_all, runner=runner)
        assert codex.auth_method == AUTH_OAUTH_SUBSCRIPTION and codex.subscription_backed is True


class TestGeminiFailsClosed:
    def test_creds_file_alone_is_not_subscription_ready(self, monkeypatch):
        # F3: a Google OAuth creds file does not prove a valid login or a tier.
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr("lib.engines._gemini_creds_present", lambda: True)
        [gem] = discover_engines(only=["gemini"], which=_which_all, runner=_runner_map({}))
        assert gem.installed is True
        assert gem.auth_method == AUTH_UNKNOWN
        assert gem.logged_in is False
        assert gem.subscription_backed is False
        assert gem.subscription_type is None  # never claim a tier the CLI can't verify

    def test_api_key_is_verifiable_but_not_subscription(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "value-not-inspected")
        [gem] = discover_engines(only=["gemini"], which=_which_all, runner=_runner_map({}))
        assert gem.auth_method == AUTH_API_KEY
        assert gem.subscription_backed is False

    def test_probe_auth_false_never_reports_gemini_ready(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr("lib.engines._gemini_creds_present", lambda: True)
        s = engines_summary(probe_auth=False, which=_which_all)
        assert "gemini" not in s["subscription_ready"]


class TestImageCapabilityIsHonest:
    """Requirement 2: image generation via a coding-agent OAuth session is NOT
    faked — every engine reports image_capable=False with an explicit blocker."""

    def test_no_engine_claims_image_capability(self):
        engines_list = discover_engines(which=_which_all, runner=_runner_map({
            "claude": ProbeResult(True, 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), ""),
            "codex": ProbeResult(True, 0, "Logged in using ChatGPT", ""),
            "gemini": ProbeResult(False, -1, "", ""),
        }))
        for e in engines_list:
            assert e.image_capable is False
            assert e.image_blocker  # a concrete reason is always present

    def test_summary_flag_matches(self):
        s = engines_summary(which=_which_all, runner=_runner_map({
            "codex": ProbeResult(True, 0, "Logged in using ChatGPT", ""),
        }), only=["codex"])
        assert s["image_via_subscription_supported"] is False


class TestZaiHonestPath:
    def test_absent_without_credentials(self, monkeypatch):
        for k in ("ZAI_API_KEY", "ZHIPUAI_API_KEY", "Z_AI_API_KEY", "GLM_API_KEY",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        [zai] = discover_engines(only=["zai"], which=_which_none, runner=_runner_map({}))
        assert zai.installed is False
        assert zai.blockers

    def test_api_key_path_detected(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "value-not-inspected")
        [zai] = discover_engines(only=["zai"], which=_which_none, runner=_runner_map({}))
        assert zai.installed is True
        assert zai.auth_method == AUTH_API_KEY
        # The env var NAME may appear as guidance, but never the value.
        assert "value-not-inspected" not in json.dumps(zai.to_dict())


class TestSummaryAndSafety:
    def test_probe_auth_false_skips_subprocess(self):
        called = {"n": 0}

        def runner(cmd, timeout):
            called["n"] += 1
            return ProbeResult(True, 0, "", "")

        discover_engines(probe_auth=False, which=_which_all, runner=runner)
        assert called["n"] == 0  # no auth probe ran

    def test_no_secret_value_patterns_in_summary(self, monkeypatch):
        # Even with keys set, only NAMES (never values) may appear.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SHOULD-NOT-APPEAR")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-SHOULD-NOT-APPEAR")
        s = engines_summary(which=_which_all, runner=_runner_map({
            "claude": ProbeResult(True, 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}), ""),
            "codex": ProbeResult(True, 0, "Logged in using ChatGPT", ""),
        }), only=["claude", "codex"])
        blob = json.dumps(s)
        assert "SHOULD-NOT-APPEAR" not in blob
        assert "sk-ant" not in blob and "sk-proj" not in blob
        # But the API-key alternative is advertised by NAME for fallback config.
        assert any(e.get("api_key_alternative") == "ANTHROPIC_API_KEY" for e in s["engines"])

    def test_summary_reports_subscription_ready(self, monkeypatch):
        # Isolate Antigravity: its probe is a separate code path (the real `agy
        # models`) not covered by the injected runner, so it would otherwise leak
        # this machine's live auth state into the assertion. Pin it not-installed
        # for a deterministic result.
        from lib import antigravity
        monkeypatch.setattr(antigravity, "is_installed", lambda: False)
        s = engines_summary(which=_which_all, runner=_runner_map({
            "claude": ProbeResult(True, 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), ""),
            "codex": ProbeResult(True, 0, "Logged in using ChatGPT", ""),
            "gemini": ProbeResult(False, -1, "", ""),
        }))
        assert set(s["subscription_ready"]) == {"claude", "codex"}
        assert s["any_subscription_ready"] is True
