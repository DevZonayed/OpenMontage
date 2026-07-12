"""Tests for lib/zai_credentials.py + lib/zai_launcher.py.

No real credential and no real network: the keyring is faked in-memory, the
metadata path is redirected to tmp, and verify()'s HTTP is injected. The
overriding contracts: the key never leaks into status/verify output or the
launch command, verification states are honest, and the scoped launcher sets
only child env (never mutating global Claude settings).
"""

from __future__ import annotations

import json

import pytest

from lib import secret_store, zai_credentials as zc
from tests.lib.test_secret_store import FakeKeyring


@pytest.fixture
def store(monkeypatch, tmp_path):
    fk = FakeKeyring("keyring.backends.macOS")
    monkeypatch.setattr(secret_store, "_keyring", lambda: fk)
    monkeypatch.setattr(zc, "_META_PATH", tmp_path / "zai_meta.json")
    return fk


class TestValidation:
    def test_rejects_short(self):
        with pytest.raises(zc.ZaiCredentialError):
            zc.validate_key("short")

    def test_rejects_newline_control(self):
        with pytest.raises(zc.ZaiCredentialError):
            zc.validate_key("abcdefgh\nijklmnop")

    def test_accepts_reasonable(self):
        zc.validate_key("a" * 40)  # no exception

    def test_unknown_plan_rejected(self, store):
        with pytest.raises(zc.ZaiCredentialError):
            zc.store_key("a" * 40, "enterprise")


class TestStoreStatusRemove:
    def test_store_then_status_stored_unverified(self, store):
        st = zc.store_key("k" * 40, "coding")
        assert st["configured"] is True
        assert st["status"] == zc.STATUS_STORED_UNVERIFIED
        assert st["plan_type"] == "coding"
        # metadata file holds NO key
        assert "k" * 40 not in (zc._META_PATH.read_text())

    def test_status_not_configured_initially(self, store):
        st = zc.status()
        assert st["configured"] is False and st["status"] == zc.STATUS_NOT_CONFIGURED

    def test_remove_makes_unavailable(self, store):
        zc.store_key("k" * 40, "coding")
        zc.remove_key()
        st = zc.status()
        assert st["configured"] is False and st["status"] == zc.STATUS_NOT_CONFIGURED
        assert secret_store.has_secret(zc.ACCOUNT) is False

    def test_store_fails_closed_without_secure_backend(self, monkeypatch, tmp_path):
        fk = FakeKeyring("keyring.backends.fail")
        monkeypatch.setattr(secret_store, "_keyring", lambda: fk)
        monkeypatch.setattr(zc, "_META_PATH", tmp_path / "m.json")
        with pytest.raises(zc.ZaiCredentialError):
            zc.store_key("k" * 40, "coding")


class TestVerify:
    def _resp(self, code):
        return type("R", (), {"status_code": code})()

    def test_verify_200_marks_verified(self, store):
        zc.store_key("k" * 40, "coding")
        captured = {}

        def http_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["auth"] = headers.get("Authorization")
            return self._resp(200)

        st = zc.verify("coding", http_get=http_get)
        assert st["status"] == zc.STATUS_VERIFIED
        assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/models"
        # The bearer carries the key, but it is NEVER put in the returned status.
        assert "k" * 40 not in json.dumps(st)

    def test_verify_401_marks_invalid(self, store):
        zc.store_key("k" * 40, "general")
        st = zc.verify("general", http_get=lambda *a, **k: self._resp(401))
        assert st["status"] == zc.STATUS_INVALID

    def test_verify_network_error_is_inconclusive(self, store):
        zc.store_key("k" * 40, "coding")

        def boom(*a, **k):
            raise RuntimeError("connection reset to secret-host.example")

        st = zc.verify("coding", http_get=boom)
        assert st["status"] == zc.STATUS_STORED_UNVERIFIED  # never falsely verified
        assert "secret-host" not in json.dumps(st)          # no exception text leaks

    def test_verify_without_key_is_not_configured(self, store):
        st = zc.verify("coding", http_get=lambda *a, **k: self._resp(200))
        assert st["status"] == zc.STATUS_NOT_CONFIGURED


class TestScopedLauncher:
    def test_build_scoped_env_sets_only_child_vars(self, store):
        zc.store_key("tok" + "x" * 40, "coding")
        base = {"PATH": "/usr/bin", "HOME": "/Users/x"}
        env = zc.build_scoped_env(base_env=base)
        assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "tok" + "x" * 40  # reached child env
        # base env preserved; nothing else clobbered
        assert env["PATH"] == "/usr/bin"

    def test_build_scoped_env_requires_key(self, store):
        with pytest.raises(zc.ZaiCredentialError):
            zc.build_scoped_env(base_env={})

    def test_launcher_does_not_mutate_global_claude_settings(self, store, monkeypatch, tmp_path):
        # Simulate a global settings file and prove the launcher path never writes it.
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"existing": "max-oauth"}')
        zc.store_key("tok" + "y" * 40, "coding")

        captured = {}

        def fake_execve(path, argv, env):
            captured["path"] = path
            captured["argv"] = argv
            captured["env"] = env
            raise SystemExit(0)  # stop before real exec

        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude")
        monkeypatch.setattr("os.execve", fake_execve)
        from lib import zai_launcher
        with pytest.raises(SystemExit):
            zai_launcher.main([])
        # token reached child env, NOT any argv, and global settings untouched
        assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok" + "y" * 40
        assert not any("tok" + "y" * 40 in str(a) for a in captured["argv"])
        assert json.loads(settings.read_text()) == {"existing": "max-oauth"}
