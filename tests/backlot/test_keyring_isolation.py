"""Regression: a clean test run must NEVER query or block on the real OS keychain.

`build_providers_payload -> zai_credentials.status -> secret_store.get_secret ->
keyring.get_password` reached the macOS Keychain (SecItemCopyMatching), which
blocks on a Keychain permission prompt in clean/CI environments. The root
`tests/conftest.py` installs a deterministic in-process keyring backend for the
whole session; these tests encode that invariant and prove the credential chain
routes through it (never the OS).
"""

from __future__ import annotations

import json

import keyring
import pytest

from lib import secret_store

# Substrings of every real OS-backed keyring backend. If the active backend
# matches any of these during a test, isolation is broken and a clean run would
# block on the system keychain.
_OS_BACKEND_MARKERS = (
    "macos", "secretservice", "libsecret", "kwallet", "windows", "chainer",
)


def _active_backend_dotted() -> str:
    kr = keyring.get_keyring()
    return f"{kr.__class__.__module__}.{kr.__class__.__name__}".lower()


def test_active_keyring_backend_is_isolated_in_tests():
    dotted = _active_backend_dotted()
    assert not any(m in dotted for m in _OS_BACKEND_MARKERS), (
        f"tests are using a real OS keyring backend ({dotted}); the in-memory "
        "isolation fixture is not active — a clean run would block on the OS keychain."
    )
    # It must still present as a genuine secure backend (production-like behavior).
    assert secret_store.backend_available() is True


def test_credential_chain_routes_through_in_memory_backend():
    from backlot import providers_api

    # Round-trip a secret entirely in the in-process backend (no OS access).
    secret_store.set_secret("test_probe_account", "sk-not-a-real-key")
    assert secret_store.get_secret("test_probe_account") == "sk-not-a-real-key"
    assert secret_store.has_secret("test_probe_account") is True
    assert secret_store.delete_secret("test_probe_account") is True
    assert secret_store.has_secret("test_probe_account") is False

    # The full providers payload builds without touching the OS keychain (was a hang)
    # and is JSON-serializable.
    payload = providers_api.build_providers_payload()
    json.dumps(payload)
