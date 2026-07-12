"""Root pytest configuration — global test isolation.

CRITICAL: replace the OS keyring with a deterministic, in-process backend for the
ENTIRE test session so no test can query or block on the real system keychain.
The real macOS Keychain (`SecItemCopyMatching`) blocks on a Keychain permission
prompt in clean/CI environments, which hung `test_providers_api` (and any sibling
that reaches `secret_store` via `build_providers_payload -> zai_credentials.status`).

Only pytest is affected — production code still selects the real secure OS backend.
`secret_store` runs its real code path here; only the *backend* is swapped, exactly
as the `keyring` library is designed to allow. The backend's module name
intentionally avoids the fail/null/chainer suffixes so `secret_store.backend_available()`
reports a genuine secure backend (production-like), while never touching the OS.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class InMemoryKeyring(KeyringBackend):
    """A process-local keyring backend — deterministic, cross-platform, no OS access."""

    priority = 1  # > 0 so it is a viable backend and reports as available

    def __init__(self) -> None:
        super().__init__()
        self._store: Dict[Tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as exc:  # pragma: no cover - defensive
            raise PasswordDeleteError("not found") from exc


# Install at import/collection time (before any test module is imported) so even
# import-time credential access can never reach the real OS keychain.
keyring.set_keyring(InMemoryKeyring())


@pytest.fixture(autouse=True)
def _isolate_os_keyring():
    """Give every test a fresh, empty in-memory keyring — deterministic, no OS access.

    Tests that need to exercise fail-closed / specific-backend behavior still
    monkeypatch ``secret_store._keyring`` directly (unaffected by this global swap).
    """
    keyring.set_keyring(InMemoryKeyring())
    yield
