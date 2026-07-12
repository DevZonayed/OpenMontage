"""F2: allowlisted engine OAuth actions — status/connect/logout.

All paths use a FAKE runner/which so no real CLI runs (and nobody is ever
actually logged out). Asserts: allowlisting, injection rejection, output
sanitization (no identity/tokens), missing binary, timeout/failure, and the
confirm-required rule for logout.
"""

from __future__ import annotations

import json

import pytest

from lib.engine_actions import EngineActionError, run_engine_action, supported_actions
from lib.engines import ProbeResult


def _which_all(name):
    return f"/usr/bin/{name}"


def _which_none(_name):
    return None


class TestInjectionGuards:
    def test_unknown_engine_rejected(self):
        with pytest.raises(EngineActionError, match="unknown engine"):
            run_engine_action("rm -rf /", "status")

    def test_unknown_action_rejected(self):
        with pytest.raises(EngineActionError, match="unknown action"):
            run_engine_action("claude", "exfiltrate")

    def test_action_not_available_for_engine(self):
        # gemini logout is 'unsupported' — returns a result, not an execution.
        r = run_engine_action("gemini", "logout")
        assert r["ok"] is False and r["supported"] is False


class TestStatusSanitized:
    def test_status_returns_sanitized_state_no_identity(self):
        payload = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
                   "email": "leak@example.com", "orgId": "org-leak"}
        runner = lambda cmd, t: ProbeResult(True, 0, json.dumps(payload), "")
        r = run_engine_action("claude", "status", runner=runner, which=_which_all)
        assert r["ok"] is True
        assert r["state"]["logged_in"] is True
        assert r["state"]["subscription_backed"] is True
        blob = json.dumps(r)
        assert "leak@example.com" not in blob and "org-leak" not in blob  # sanitized
        # raw stdout is never echoed
        assert "authMethod" not in blob


class TestConnectManual:
    def test_connect_is_manual_and_not_success(self):
        # D: manual connect executes nothing and completes no OAuth, so it is NOT
        # a success — ok=False, started=False.
        calls = {"n": 0}

        def runner(cmd, t):
            calls["n"] += 1
            return ProbeResult(True, 0, "", "")

        r = run_engine_action("codex", "connect", runner=runner, which=_which_all)
        assert r["mode"] == "manual"
        assert r["ok"] is False and r["started"] is False
        assert r["command"] == "codex login"
        assert r["auth_kind"] == "oauth"
        assert calls["n"] == 0  # never runs the interactive login headlessly

    def test_zai_connect_is_token_not_oauth(self):
        r = run_engine_action("zai", "connect")
        assert r["mode"] == "manual" and r["ok"] is False
        assert r["auth_kind"] == "api_token"
        assert "ZAI_API_KEY" in r["command"]
        assert "oauth" not in r["message"].lower()  # must NOT imply OAuth for Z.AI


class TestLogout:
    def test_logout_requires_confirmation(self):
        with pytest.raises(EngineActionError, match="confirm"):
            run_engine_action("claude", "logout", confirm=False, which=_which_all,
                              runner=lambda c, t: ProbeResult(True, 0, "", ""))

    def test_logout_with_confirm_runs_and_sanitizes(self):
        # Even if the CLI printed identity on logout, it must not leak.
        runner = lambda cmd, t: ProbeResult(True, 0, "Logged out user leak@example.com", "")
        r = run_engine_action("codex", "logout", confirm=True, runner=runner, which=_which_all)
        assert r["ok"] is True
        assert "leak@example.com" not in json.dumps(r)

    def test_logout_missing_binary(self):
        r = run_engine_action("claude", "logout", confirm=True, which=_which_none,
                              runner=lambda c, t: ProbeResult(True, 0, "", ""))
        assert r["ok"] is False and r["installed"] is False

    def test_logout_failure_reports_not_ok(self):
        runner = lambda cmd, t: ProbeResult(True, 1, "", "error")
        r = run_engine_action("codex", "logout", confirm=True, runner=runner, which=_which_all)
        assert r["ok"] is False

    def test_logout_timeout_reports_not_ok(self):
        runner = lambda cmd, t: ProbeResult(False, -1, "", "timeout")
        r = run_engine_action("codex", "logout", confirm=True, runner=runner, which=_which_all)
        assert r["ok"] is False


class TestSupportedActions:
    def test_matrix_shapes(self):
        assert supported_actions("claude") == {"status": "auto", "connect": "manual", "logout": "auto"}
        assert supported_actions("gemini")["logout"] == "unsupported"
        assert supported_actions("zai")["logout"] == "unsupported"
