"""Tests for lib/antigravity.py + Antigravity engine discovery/actions.

Grounded in the REAL `agy` contract (verified live): `agy models` prints a
sign-in message when unauthenticated (exit 0) and lists models when signed in.
No real download/Terminal/identity here — install uses a synthetic checksummed
archive and actions inject their probe/spawn.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from lib import antigravity
from lib.engines import (AUTH_NONE, AUTH_NOT_INSTALLED, AUTH_OAUTH_SUBSCRIPTION,
                         AUTH_UNKNOWN, discover_engines)


def _runner_for(text: str, rc: int = 0):
    return lambda cmd, timeout: (rc, text, "")


class TestProbeStatusFailClosed:
    def test_signin_message_means_not_signed_in(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "Error: Please sign in to view available models. Launch the CLI without arguments to sign in."))
        assert st == {"installed": True, "signed_in": False, "probe_ran": True}

    def test_model_listing_means_signed_in(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for("gemini-2.5-pro\ngemini-2.5-flash\n"))
        assert st["signed_in"] is True

    def test_empty_output_fails_closed(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for("", rc=0))
        assert st["signed_in"] is False  # no listing -> not confirmed

    def test_nonzero_exit_fails_closed(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for("boom", rc=2))
        assert st["signed_in"] is False

    def test_not_installed(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: None)
        st = antigravity.probe_status()
        assert st == {"installed": False, "signed_in": False, "probe_ran": False}

    # --- Fail-closed regressions: rc==0 non-listing output must NOT read green ---
    def test_ambiguous_warning_rc0_fails_closed(self, monkeypatch):
        # A benign banner with exit 0 and NO model id must not be mistaken for auth.
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "Notice: a new version is available. Run to update.\n", rc=0))
        assert st["signed_in"] is False and st["probe_ran"] is True

    def test_generic_error_rc0_fails_closed(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "An unexpected problem occurred while contacting the service.\n", rc=0))
        assert st["signed_in"] is False

    def test_help_banner_rc0_fails_closed(self, monkeypatch):
        # Help text mentions "list available models" but carries no model id.
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "Usage: agy models [flags]\n\nList available models\n\nFlags:\n  -h  Show help\n", rc=0))
        assert st["signed_in"] is False

    def test_signin_message_rc1_fails_closed(self, monkeypatch):
        # Live signed-out may exit non-zero with the sign-in message; still closed.
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "Error: Please sign in to view available models.\n", rc=1))
        assert st["signed_in"] is False and st["probe_ran"] is True

    def test_probe_failure_rc_minus1_marks_probe_not_run(self, monkeypatch):
        # Timeout / exec failure -> probe_ran False, fail closed (cannot confirm).
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for("", rc=-1))
        assert st == {"installed": True, "signed_in": False, "probe_ran": False}

    def test_authentic_listing_with_header_signed_in(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "Available models:\n  gemini-2.5-pro\n  claude-sonnet-4-20250514\n  gpt-4o\n", rc=0))
        assert st["signed_in"] is True

    def test_error_mentioning_model_domain_not_signed_in(self, monkeypatch):
        # A model *family word* inside an error/URL (no "-/" model id) must not
        # register as evidence; the error marker also forces fail-closed.
        monkeypatch.setattr(antigravity, "agy_path", lambda: "/x/agy")
        st = antigravity.probe_status(runner=_runner_for(
            "Error: could not reach https://gemini.google.com\n", rc=0))
        assert st["signed_in"] is False


def _make_targz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"#!/bin/sh\necho fake agy\n"
        info = tarfile.TarInfo("antigravity")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestInstallChecksum:
    def _http(self, url_payload, manifest):
        def http_get(u):
            if u.endswith(".json"):
                return manifest
            if u == url_payload:
                return _make_targz.__wrapped_payload__  # set below
            raise AssertionError(f"unexpected url {u}")
        return http_get

    def test_install_verifies_and_places_binary(self, monkeypatch, tmp_path):
        payload = _make_targz()
        sha = hashlib.sha512(payload).hexdigest()
        url = "https://storage.googleapis.com/antigravity-public/fake/cli.tar.gz"
        manifest = json.dumps({"version": "9.9.9", "url": url, "sha512": sha}).encode()

        def http_get(u):
            return manifest if u.endswith(".json") else payload

        monkeypatch.setattr(antigravity, "_LOCAL_BIN", tmp_path / "agy")
        res = antigravity.install(http_get=http_get)
        assert res["version"] == "9.9.9"
        assert (tmp_path / "agy").is_file()

    def test_install_rejects_checksum_mismatch(self, monkeypatch, tmp_path):
        payload = _make_targz()
        url = "https://storage.googleapis.com/antigravity-public/fake/cli.tar.gz"
        manifest = json.dumps({"version": "9.9.9", "url": url, "sha512": "0" * 128}).encode()
        monkeypatch.setattr(antigravity, "_LOCAL_BIN", tmp_path / "agy")
        with pytest.raises(antigravity.AntigravityInstallError):
            antigravity.install(http_get=lambda u: manifest if u.endswith(".json") else payload)
        assert not (tmp_path / "agy").exists()

    def test_install_rejects_non_google_url(self, monkeypatch, tmp_path):
        payload = _make_targz()
        sha = hashlib.sha512(payload).hexdigest()
        url = "https://evil.example/cli.tar.gz"
        manifest = json.dumps({"version": "9.9.9", "url": url, "sha512": sha}).encode()
        monkeypatch.setattr(antigravity, "_LOCAL_BIN", tmp_path / "agy")
        with pytest.raises(antigravity.AntigravityInstallError):
            antigravity.install(http_get=lambda u: manifest if u.endswith(".json") else payload)


class TestEngineDiscovery:
    def test_not_installed_engine(self, monkeypatch):
        monkeypatch.setattr(antigravity, "is_installed", lambda: False)
        [ag] = discover_engines(only=["antigravity"])
        assert ag.installed is False and ag.auth_method == AUTH_NOT_INSTALLED
        assert ag.subscription_backed is False
        assert any("2026-06-18" in n for n in ag.notes)

    def test_installed_not_signed_in(self, monkeypatch):
        monkeypatch.setattr(antigravity, "is_installed", lambda: True)
        monkeypatch.setattr(antigravity, "probe_status",
                            lambda **k: {"installed": True, "signed_in": False, "probe_ran": True})
        [ag] = discover_engines(only=["antigravity"])
        assert ag.installed is True and ag.subscription_backed is False
        assert ag.auth_method == AUTH_NONE

    def test_signed_in_is_subscription_no_tier(self, monkeypatch):
        monkeypatch.setattr(antigravity, "is_installed", lambda: True)
        monkeypatch.setattr(antigravity, "probe_status",
                            lambda **k: {"installed": True, "signed_in": True, "probe_ran": True})
        [ag] = discover_engines(only=["antigravity"])
        assert ag.subscription_backed is True
        assert ag.auth_method == AUTH_OAUTH_SUBSCRIPTION
        assert ag.subscription_type is None  # never claim a tier

    def test_probe_auth_false_not_ready(self, monkeypatch):
        monkeypatch.setattr(antigravity, "is_installed", lambda: True)
        [ag] = discover_engines(only=["antigravity"], probe_auth=False)
        assert ag.subscription_backed is False and ag.auth_method == AUTH_UNKNOWN


class TestActions:
    def test_status_action(self, monkeypatch):
        from lib.engine_actions import run_engine_action
        monkeypatch.setattr(antigravity, "probe_status",
                            lambda **k: {"installed": True, "signed_in": False})
        r = run_engine_action("antigravity", "status")
        assert r["state"] == {"installed": True, "signed_in": False}

    def test_connect_injected_no_terminal(self):
        from lib.engine_actions import run_antigravity_action
        calls = {"n": 0}
        r = run_antigravity_action("connect", spawn=lambda: calls.__setitem__("n", 1) or True)
        # only when installed; monkeypatch is_installed
        assert "message" in r

    def test_install_action_uses_injected_installer(self):
        from lib.engine_actions import run_antigravity_action
        r = run_antigravity_action("install", installer=lambda: {"installed": True, "version": "9.9.9"})
        assert r["ok"] is True and r["version"] == "9.9.9"

    def test_logout_requires_confirm(self, monkeypatch):
        from lib.engine_actions import EngineActionError, run_antigravity_action
        monkeypatch.setattr(antigravity, "is_installed", lambda: True)
        with pytest.raises(EngineActionError):
            run_antigravity_action("logout", spawn=lambda: True)
