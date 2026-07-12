"""Secure secret storage via the OS credential store (never plaintext).

Credentials (e.g. a Z.AI API key) are stored ONLY in the native OS keyring —
macOS Keychain here, Secret Service / Windows Credential Locker elsewhere — via
the ``keyring`` library. There is deliberately NO plaintext fallback: if no
secure backend is available we fail closed so the UI can show an actionable
blocker rather than silently writing a key to disk.

Hard rules enforced here:
  * secret VALUES are never logged, never returned in errors, never written to
    project files / .env / shell profiles / Claude settings;
  * only non-secret metadata (which account exists, a plan label, a status) lives
    outside the keyring, and that metadata is written by callers, not this module.
"""

from __future__ import annotations

from typing import Optional

# The keyring "service" namespace all OpenMontage secrets live under.
SERVICE = "openmontage"

# Insecure backends we must refuse (they would drop the secret or store it
# in plaintext). We match by module name so we don't hard-import them.
_INSECURE_BACKEND_SUFFIXES = ("fail", "null", "chainer")


class SecretStoreUnavailable(RuntimeError):
    """Raised when no secure OS keyring backend is available (fail-closed)."""


def _keyring():
    import keyring  # imported lazily so import errors surface as unavailability
    return keyring


def backend_name() -> Optional[str]:
    """Return the active keyring backend's dotted class name, or None."""
    try:
        kr = _keyring().get_keyring()
        return f"{kr.__class__.__module__}.{kr.__class__.__name__}"
    except Exception:
        return None


def backend_available() -> bool:
    """True only when a genuine secure keyring backend is active.

    A ``fail``/``null`` backend (or an empty chainer) means the platform has no
    usable secure store — we treat that as unavailable and fail closed.
    """
    try:
        kr = _keyring().get_keyring()
    except Exception:
        return False
    mod = (kr.__class__.__module__ or "").lower()
    cls = (kr.__class__.__name__ or "").lower()
    if cls == "fail" or any(mod.endswith(s) or mod.endswith(f"backends.{s}") for s in _INSECURE_BACKEND_SUFFIXES):
        # A chainer *may* wrap a real backend, but we can't be sure it's secure;
        # be conservative. On the supported platforms the concrete secure backend
        # (macOS.Keyring, SecretService.Keyring, Windows.WinVaultKeyring) is active.
        if mod.endswith("chainer"):
            # Accept a chainer only if it advertises a viable secure backend.
            try:
                inner = getattr(kr, "backends", []) or []
                return any(getattr(b, "priority", 0) and "fail" not in b.__class__.__module__.lower()
                           for b in inner)
            except Exception:
                return False
        return False
    return True


def _require_backend() -> None:
    if not backend_available():
        raise SecretStoreUnavailable(
            "No secure OS keyring backend is available. A credential can only be "
            "stored in the system keychain, never in plaintext. Configure a keyring "
            "backend and try again."
        )


def set_secret(account: str, value: str) -> None:
    """Store a secret in the OS keyring under (SERVICE, account). Fails closed."""
    if not account or not isinstance(value, str) or value == "":
        raise ValueError("account and non-empty value are required")
    _require_backend()
    _keyring().set_password(SERVICE, account, value)


def get_secret(account: str) -> Optional[str]:
    """Return the stored secret for account, or None. (For runtime use only —
    callers must never log the return value.)"""
    try:
        return _keyring().get_password(SERVICE, account)
    except Exception:
        return None


def has_secret(account: str) -> bool:
    return get_secret(account) is not None


def delete_secret(account: str) -> bool:
    """Delete the stored secret. Returns True if something was removed."""
    try:
        kr = _keyring()
        if kr.get_password(SERVICE, account) is None:
            return False
        kr.delete_password(SERVICE, account)
        return True
    except Exception:
        return False
