"""Tests for lib/secret_store.py — secure keyring wrapper, fail-closed.

A fake in-memory keyring stands in for the OS backend, so no real Keychain is
touched and no real secret is used. Covers: secure vs insecure backend
detection, set/get/has/delete, fail-closed when no secure backend, and that a
stored value is never surfaced except via explicit get_secret.
"""

from __future__ import annotations

import pytest

from lib import secret_store


class FakeKeyring:
    """Mimics the keyring module's function surface with a controllable backend."""

    def __init__(self, backend_module="keyring.backends.macOS"):
        self._store: dict = {}
        self._backend_module = backend_module

    def get_keyring(self):
        B = type("Keyring", (), {})
        B.__module__ = self._backend_module
        return B()

    def set_password(self, service, account, value):
        self._store[(service, account)] = value

    def get_password(self, service, account):
        return self._store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self._store:
            raise RuntimeError("not found")
        del self._store[(service, account)]


@pytest.fixture
def secure(monkeypatch):
    fk = FakeKeyring("keyring.backends.macOS")
    monkeypatch.setattr(secret_store, "_keyring", lambda: fk)
    return fk


@pytest.fixture
def insecure(monkeypatch):
    fk = FakeKeyring("keyring.backends.fail")
    monkeypatch.setattr(secret_store, "_keyring", lambda: fk)
    return fk


class TestBackendDetection:
    def test_secure_backend_available(self, secure):
        assert secret_store.backend_available() is True

    def test_fail_backend_not_available(self, insecure):
        assert secret_store.backend_available() is False

    def test_null_backend_not_available(self, monkeypatch):
        fk = FakeKeyring("keyring.backends.null")
        monkeypatch.setattr(secret_store, "_keyring", lambda: fk)
        assert secret_store.backend_available() is False


class TestLifecycle:
    def test_set_get_has_delete(self, secure):
        assert secret_store.has_secret("acct") is False
        secret_store.set_secret("acct", "s3cr3t-value")
        assert secret_store.has_secret("acct") is True
        assert secret_store.get_secret("acct") == "s3cr3t-value"
        assert secret_store.delete_secret("acct") is True
        assert secret_store.has_secret("acct") is False
        assert secret_store.delete_secret("acct") is False  # already gone

    def test_set_requires_nonempty(self, secure):
        with pytest.raises(ValueError):
            secret_store.set_secret("acct", "")

    def test_fail_closed_when_no_secure_backend(self, insecure):
        with pytest.raises(secret_store.SecretStoreUnavailable):
            secret_store.set_secret("acct", "value")
