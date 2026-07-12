"""Composition-runtime maintenance actions (Remotion install/repair/verify).

Contract: fixed allowlisted actions on allowlisted runtimes only; NO arbitrary
package/command/path from the caller; installer/browser-ensure are injectable so
tests never hit the network; every result is sanitized and carries a fresh
doctor report; unknown runtime/action are rejected.
"""

from __future__ import annotations

import pytest

from lib import runtime_actions as ra


def _fake_doctor(**kw):
    return {"available": True, "installed": True, "reason": "", "version": "4.0.484",
            "node_version": "v22", "browser_source": "system", "checks": {}}


class TestValidation:
    def test_unknown_runtime_rejected(self):
        with pytest.raises(ra.RuntimeActionError):
            ra.run_runtime_action("ffmpeg", "install")

    def test_unknown_action_rejected(self):
        with pytest.raises(ra.RuntimeActionError):
            ra.run_runtime_action("remotion", "sudo")

    def test_actions_and_runtimes_are_allowlists(self):
        assert ra.RUNTIMES == ("remotion",)
        assert set(ra.ACTIONS) == {"verify", "install", "repair"}


class TestVerify:
    def test_verify_returns_doctor(self, monkeypatch):
        monkeypatch.setattr(ra, "_doctor", _fake_doctor)
        r = ra.run_runtime_action("remotion", "verify")
        assert r["ok"] is True and r["action"] == "verify"
        assert r["doctor"]["available"] is True
        assert "version" in r["doctor"]


class TestInstall:
    def test_install_runs_fixed_npm_ci_then_doctor(self, monkeypatch):
        calls = {}
        def fake_installer():
            calls["ran"] = True
            return (0, "added 199 packages", "")
        monkeypatch.setattr(ra, "_doctor", _fake_doctor)
        r = ra.run_runtime_action("remotion", "install", installer=fake_installer)
        assert calls.get("ran") is True
        assert r["ok"] is True
        assert r["doctor"]["available"] is True

    def test_install_failure_is_sanitized(self, monkeypatch):
        def fake_installer():
            return (1, "", "npm ERR! network /Users/secret/path failed")
        # doctor still reports not-available after a failed install
        monkeypatch.setattr(ra, "_doctor", lambda **k: {
            "available": False, "installed": False, "reason": "deps missing", "checks": {}})
        r = ra.run_runtime_action("remotion", "install", installer=fake_installer)
        assert r["ok"] is False
        # no raw npm stderr / user paths leaked
        assert "/Users/secret" not in str(r)
        assert "npm ERR" not in str(r)

    def test_default_installer_uses_fixed_cwd_argv(self):
        # The real installer command must be fixed (npm ci in remotion-composer),
        # never derived from caller input.
        argv, cwd = ra.install_command()
        assert argv[0] == "npm" and "ci" in argv
        assert str(cwd).endswith("remotion-composer")


class TestRepair:
    def test_repair_installs_and_ensures_browser(self, monkeypatch):
        seq = []
        monkeypatch.setattr(ra, "_doctor", _fake_doctor)
        r = ra.run_runtime_action(
            "remotion", "repair",
            installer=lambda: seq.append("ci") or (0, "ok", ""),
            browser_ensurer=lambda: seq.append("browser") or (0, "ok", ""))
        assert seq == ["ci", "browser"]
        assert r["ok"] is True
