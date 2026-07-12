"""Z.AI (GLM) credential management — secure store, verify, scoped launcher.

The API key lives ONLY in the OS keychain (via lib.secret_store). Everything
here handles the key by reference: it is fetched at the moment of use and never
logged, returned, echoed in errors, or written to project files. Only non-secret
metadata (plan type + coarse status) is persisted, under .backlot/ (gitignored).

Official Z.AI endpoints (documented):
  * GLM Coding Plan — OpenAI-compatible: https://api.z.ai/api/coding/paas/v4
                      Claude-Code scoped:  https://api.z.ai/api/anthropic
  * General API     — OpenAI-compatible: https://api.z.ai/api/paas/v4
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from lib.paths import REPO_ROOT
from lib import secret_store

# Keychain account for the Z.AI API key.
ACCOUNT = "zai_api_key"

# Non-secret metadata (plan type + status) — never the key.
_META_PATH = REPO_ROOT / ".backlot" / "zai_meta.json"

PLANS = ("coding", "general")

# OpenAI-compatible base per plan (used for the /models verify probe) + the
# Anthropic-compatible base used by the scoped Claude-Code launcher.
_OPENAI_BASE = {
    "coding": "https://api.z.ai/api/coding/paas/v4",
    "general": "https://api.z.ai/api/paas/v4",
}
ANTHROPIC_BASE = "https://api.z.ai/api/anthropic"

# Status values surfaced to the UI (never OAuth / "logged in").
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_STORED_UNVERIFIED = "stored_unverified"
STATUS_VERIFIED = "verified"
STATUS_INVALID = "invalid"

_VERIFY_TIMEOUT = 10  # seconds, strict


class ZaiCredentialError(ValueError):
    """Raised for invalid input / storage failures (message is UI-safe)."""


# ---------------------------------------------------------------------------
# Input validation (defensive; no brittle prefix assumptions)
# ---------------------------------------------------------------------------

def validate_key(key: str) -> None:
    """Validate a candidate key defensively. Raises ZaiCredentialError (UI-safe).

    We do NOT assume a specific vendor prefix (those change). We require a
    reasonable length and reject control characters / whitespace that indicate a
    paste error — never echo the value.
    """
    if not isinstance(key, str):
        raise ZaiCredentialError("Key must be text.")
    if not (8 <= len(key) <= 512):
        raise ZaiCredentialError("Key length looks wrong (expected 8–512 characters).")
    if re.search(r"[\s\x00-\x1f\x7f]", key):
        raise ZaiCredentialError("Key contains whitespace or control characters — check the paste.")


def _validate_plan(plan: str) -> str:
    if plan not in PLANS:
        raise ZaiCredentialError(f"Unknown plan type. Choose one of: {', '.join(PLANS)}.")
    return plan


# ---------------------------------------------------------------------------
# Non-secret metadata
# ---------------------------------------------------------------------------

def _read_meta() -> dict:
    try:
        return json.loads(_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(plan_type: Optional[str], status: str) -> None:
    _META_PATH.parent.mkdir(parents=True, exist_ok=True)
    meta = {"plan_type": plan_type, "status": status}
    tmp = _META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    import os
    os.replace(tmp, _META_PATH)


# ---------------------------------------------------------------------------
# Store / status / remove
# ---------------------------------------------------------------------------

def store_key(key: str, plan_type: str) -> dict:
    """Validate + store the key in the OS keychain; record non-secret metadata.

    Fails closed if no secure keyring backend exists (never plaintext). Returns a
    non-secret status dict. The key is never logged or returned.
    """
    plan = _validate_plan(plan_type)
    validate_key(key)
    if not secret_store.backend_available():
        raise ZaiCredentialError(
            "No secure system keychain is available on this machine, so the key "
            "cannot be stored safely. Nothing was saved.")
    secret_store.set_secret(ACCOUNT, key)
    _write_meta(plan, STATUS_STORED_UNVERIFIED)
    return status()


def remove_key() -> dict:
    """Delete the keychain item and clear metadata; Z.AI becomes unavailable."""
    secret_store.delete_secret(ACCOUNT)
    _write_meta(None, STATUS_NOT_CONFIGURED)
    return status()


def status() -> dict:
    """Non-secret status: not_configured / stored_unverified / verified / invalid.

    Never OAuth or "logged in" — this is stored-credential state, not a session.
    """
    configured = secret_store.has_secret(ACCOUNT)
    meta = _read_meta()
    if not configured:
        return {"engine": "zai", "configured": False, "status": STATUS_NOT_CONFIGURED,
                "plan_type": None, "keychain_available": secret_store.backend_available()}
    st = meta.get("status") or STATUS_STORED_UNVERIFIED
    if st not in (STATUS_STORED_UNVERIFIED, STATUS_VERIFIED, STATUS_INVALID):
        st = STATUS_STORED_UNVERIFIED
    return {"engine": "zai", "configured": True, "status": st,
            "plan_type": meta.get("plan_type"), "keychain_available": True}


# ---------------------------------------------------------------------------
# Verification (official HTTPS models probe; sanitized result only)
# ---------------------------------------------------------------------------

def verify(plan_type: Optional[str] = None, *, http_get: Optional[Callable] = None) -> dict:
    """Verify the stored key against the official Z.AI OpenAI-compatible /models
    endpoint (a non-billable listing). Returns a sanitized status dict — NEVER the
    response body, key, identity, or raw exception text.

    ``http_get`` is injectable for tests; defaults to ``requests.get``.
    """
    key = secret_store.get_secret(ACCOUNT)
    if not key:
        _write_meta(None, STATUS_NOT_CONFIGURED)
        return status()
    plan = _validate_plan(plan_type or _read_meta().get("plan_type") or "coding")
    base = _OPENAI_BASE[plan]

    getter = http_get
    if getter is None:
        import requests
        getter = requests.get

    outcome = STATUS_STORED_UNVERIFIED
    try:
        resp = getter(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_VERIFY_TIMEOUT,
        )
        code = getattr(resp, "status_code", None)
        if code == 200:
            outcome = STATUS_VERIFIED
        elif code in (401, 403):
            outcome = STATUS_INVALID
        else:
            outcome = STATUS_STORED_UNVERIFIED  # inconclusive — do NOT claim verified
    except Exception:
        # Network/timeout/anything — inconclusive. Never surface the exception.
        outcome = STATUS_STORED_UNVERIFIED
    finally:
        del key  # drop the reference promptly

    _write_meta(plan, outcome)
    return status()


# ---------------------------------------------------------------------------
# Scoped launcher env (child-process only; NEVER mutates ~/.claude/settings.json)
# ---------------------------------------------------------------------------

def build_scoped_env(base_env: Optional[dict] = None) -> dict:
    """Return a COPY of the environment with ONLY the Z.AI scoping added, for a
    child Claude-Code process. The token is read from the keychain here and set
    on the returned dict — it is never written to any file or argv.

    Raises ZaiCredentialError if no key is stored.
    """
    import os
    key = secret_store.get_secret(ACCOUNT)
    if not key:
        raise ZaiCredentialError("No Z.AI key is stored; nothing to launch with.")
    env = dict(base_env if base_env is not None else os.environ)
    env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE
    env["ANTHROPIC_AUTH_TOKEN"] = key
    # A strict client timeout for the scoped session (non-secret).
    env.setdefault("ANTHROPIC_REQUEST_TIMEOUT", "600")
    return env
