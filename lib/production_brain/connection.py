"""Hermes / Mochlet connection layer — status, guided connect, live-client build.

The production brain talks to a durable orchestrator over the port defined in
:mod:`lib.production_brain.orchestrator`. Historically the ONLY way to point it at
a service was to set an env var by hand and drop a token somewhere — a raw,
error-prone UX that surfaced to the user as a blunt "hermes brain is unavailable".

This module makes the local **Mochlet** orchestrator a first-class, guided
connection:

  * it knows the fixed local Mochlet loopback endpoint (``127.0.0.1:9235``);
  * it performs a real ``/health`` verify handshake (transport injectable — the
    test suite NEVER hits the network);
  * it stores the bearer token ONLY in the OS keyring (never echoed/logged/
    persisted to a file), and the non-secret endpoint in a gitignored config;
  * it reports a structured, plain-language connection status the UI can act on;
  * and it builds the live orchestrator client the adapter actually uses, so a
    successful Connect immediately enables Start/Continue production.

Security posture is preserved end-to-end: endpoints are validated fail-closed
(HTTPS, or HTTP only on loopback), no shell interpolation, redirects rejected by
the underlying client, and tokens live only in the keyring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from lib.paths import REPO_ROOT
from lib.production_brain.orchestrator import (
    ORCHESTRATOR_TOKEN_ACCOUNT,
    ORCHESTRATOR_URL_ENV,
    ConfiguredHermesOrchestratorClient,
    OrchestratorUnavailable,
    validate_endpoint,
)

# The fixed local Mochlet supervisor loopback endpoint. Suggested + probed, but
# only ADOPTED as the live endpoint once the user connects (verify + persist).
DEFAULT_MOCHLET_URL = "http://127.0.0.1:9235"
HEALTH_PATH = "/health"
# Bounded so a hung orchestrator can't pin a status-request thread for long; the
# result is cached (see backlot/status_api.connection_view) so board/studio
# polling probes at most once per cache window, not once per poll.
_PROBE_TIMEOUT_SECONDS = 1.5

# Gitignored, non-secret connection config (endpoint only — never the token).
_CONFIG_DIRNAME = ".backlot"
_CONFIG_FILENAME = "hermes_connection.json"


class ConnectionError(RuntimeError):
    """A guided-connect failure carrying an HTTP-ish ``status`` for the API."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Config persistence (endpoint only — the token lives in the keyring)
# --------------------------------------------------------------------------- #
def _config_dir(base_dir: Optional[Path] = None) -> Path:
    return Path(base_dir or REPO_ROOT) / _CONFIG_DIRNAME


def _config_path(base_dir: Optional[Path] = None) -> Path:
    return _config_dir(base_dir) / _CONFIG_FILENAME


def stored_endpoint(base_dir: Optional[Path] = None) -> Optional[str]:
    """The endpoint persisted by a successful Connect, or None. Validated."""
    path = _config_path(base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("endpoint")
    try:
        return validate_endpoint(url)
    except OrchestratorUnavailable:
        return None


def _persist_endpoint(url: str, *, base_dir: Optional[Path] = None) -> None:
    d = _config_dir(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (_CONFIG_FILENAME + ".tmp")
    tmp.write_text(json.dumps({"endpoint": url, "kind": "hermes_connection"}), encoding="utf-8")
    tmp.replace(_config_path(base_dir))


def _clear_endpoint(base_dir: Optional[Path] = None) -> None:
    try:
        _config_path(base_dir).unlink()
    except OSError:
        pass


def configured_endpoint(*, env: Optional[dict] = None,
                        base_dir: Optional[Path] = None) -> Optional[str]:
    """The live endpoint the adapter should use: env var → persisted config.

    The Mochlet loopback default is deliberately NOT auto-adopted here — it is a
    *suggestion* the guided Connect flow verifies + persists, so Start stays
    fail-closed until the user has actually connected.
    """
    environ = env if env is not None else os.environ
    raw = environ.get(ORCHESTRATOR_URL_ENV)
    if raw:
        try:
            return validate_endpoint(raw)
        except OrchestratorUnavailable:
            return None
    return stored_endpoint(base_dir)


def probe_target(*, env: Optional[dict] = None, base_dir: Optional[Path] = None) -> str:
    """The endpoint to probe/suggest: the configured one, else local Mochlet."""
    return configured_endpoint(env=env, base_dir=base_dir) or DEFAULT_MOCHLET_URL


# --------------------------------------------------------------------------- #
# Health handshake
# --------------------------------------------------------------------------- #
def _default_transport(url: str, *, timeout: float):
    import requests  # a declared dependency

    return requests.get(url, timeout=timeout, allow_redirects=False)


def probe_health(url: str, *, transport: Optional[Callable[..., Any]] = None,
                 timeout: float = _PROBE_TIMEOUT_SECONDS) -> dict:
    """GET ``<url>/health`` and classify the result. Never raises.

    ``transport(url, timeout=...)`` is injectable so the suite never hits the
    network. Returns ``{reachable, healthy, status_code, service, detail}``.
    """
    try:
        base = validate_endpoint(url)
    except OrchestratorUnavailable as exc:
        return {"reachable": False, "healthy": False, "status_code": None,
                "service": None, "detail": str(exc)}
    target = base.rstrip("/") + HEALTH_PATH
    call = transport or _default_transport
    try:
        resp = call(target, timeout=timeout)
    except Exception as exc:  # connection refused / timeout / library error
        return {"reachable": False, "healthy": False, "status_code": None,
                "service": None,
                "detail": f"Local Hermes/Mochlet did not respond ({exc.__class__.__name__})."}
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 0:
        # A response object with no usable status is NOT a healthy connection.
        return {"reachable": False, "healthy": False, "status_code": None,
                "service": None, "detail": "Health check returned no status code."}
    if 300 <= status < 400:
        return {"reachable": True, "healthy": False, "status_code": status,
                "service": None, "detail": "Endpoint redirected the health check."}
    if not (200 <= status < 300):
        # Reachable but unhealthy/unauthorized (4xx/5xx, or an unexpected 1xx).
        return {"reachable": True, "healthy": False, "status_code": status,
                "service": None,
                "detail": f"Health check returned HTTP {status}."}
    service = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            service = body.get("service") or body.get("name")
    except Exception:
        body = None
    return {"reachable": True, "healthy": True, "status_code": status or 200,
            "service": service, "detail": "Connected."}


# --------------------------------------------------------------------------- #
# Structured connection status
# --------------------------------------------------------------------------- #
def connection_status(
    *,
    env: Optional[dict] = None,
    base_dir: Optional[Path] = None,
    transport: Optional[Callable[..., Any]] = None,
    probe: bool = True,
    secret_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> dict:
    """Plain-language, actionable Hermes connection status for the UI.

    Never raises; never returns a token. ``probe`` runs the health handshake
    (default on); pass ``probe=False`` for a cheap config-only read.
    """
    endpoint = configured_endpoint(env=env, base_dir=base_dir)
    getter = secret_getter or _keyring_getter
    token_present = bool(getter(ORCHESTRATOR_TOKEN_ACCOUNT))

    def _base(status: str, available: bool, headline: str, detail: str,
              actions: list[dict]) -> dict:
        return {
            "status": status,
            "available": available,
            "endpoint": endpoint,
            "suggested_endpoint": DEFAULT_MOCHLET_URL,
            "loopback": _is_loopback(endpoint or DEFAULT_MOCHLET_URL),
            "token_configured": token_present,
            "headline": headline,
            "detail": detail,
            "actions": actions,
        }

    connect_action = {"id": "connect_hermes", "label": "Connect Hermes"}
    retry_action = {"id": "retry_connect", "label": "Retry connection"}

    if not endpoint:
        # Not configured. Detect whether local Mochlet is already running so the
        # guided connect can be one click.
        if probe:
            health = probe_health(DEFAULT_MOCHLET_URL, transport=transport)
            if health["reachable"]:
                return _base(
                    "detected", False,
                    "Local Hermes (Mochlet) detected — connect to start production",
                    "Mochlet is running on this Mac. Connect to verify and enable "
                    "production runs.",
                    [{"id": "connect_hermes", "label": "Connect Hermes (Mochlet)"}])
        return _base(
            "needs_setup", False,
            "Hermes isn't connected yet",
            "OpenMontage runs production through the Hermes brain. Connect a local "
            "Mochlet orchestrator (127.0.0.1:9235) or configure an endpoint to begin.",
            [{"id": "connect_hermes", "label": "Connect Hermes"}])

    if not probe:
        return _base("configured", True, "Hermes configured",
                     "An orchestrator endpoint is configured.", [])

    health = probe_health(endpoint, transport=transport)
    if health["healthy"]:
        svc = health.get("service")
        return _base(
            "connected", True,
            f"Connected to Hermes{f' ({svc})' if svc else ''}",
            "Production runs are enabled.", [])
    if health["reachable"] and health.get("status_code") in (401, 403):
        return _base(
            "needs_token", False,
            "Hermes needs a credential",
            "The orchestrator is reachable but rejected the request. Reconnect with "
            "a valid token.",
            [{"id": "connect_hermes", "label": "Reconnect Hermes"}])
    # Configured but not reachable — the service is down.
    return _base(
        "unreachable", False,
        "Hermes is configured but not responding",
        health.get("detail") or "The configured orchestrator did not respond.",
        [retry_action, connect_action])


# --------------------------------------------------------------------------- #
# Guided connect / disconnect
# --------------------------------------------------------------------------- #
def connect(
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    env: Optional[dict] = None,
    base_dir: Optional[Path] = None,
    transport: Optional[Callable[..., Any]] = None,
    secret_setter: Optional[Callable[[str, str], None]] = None,
    secret_getter: Optional[Callable[[str], Optional[str]]] = None,
    persist: bool = True,
) -> dict:
    """Guided connect: validate endpoint → store token → verify → persist.

    Returns a connection-status dict. The token is stored in the OS keyring and
    NEVER returned, logged, or written to a file. Raises :class:`ConnectionError`
    on an invalid endpoint. A failed health handshake does NOT persist the
    endpoint (fail-closed) and returns an ``unreachable``/``needs_token`` status.
    """
    target = url or DEFAULT_MOCHLET_URL
    try:
        target = validate_endpoint(target)
    except OrchestratorUnavailable as exc:
        raise ConnectionError(str(exc), status=400) from exc

    # Store the token first (so the verify handshake can authenticate), but only a
    # real, non-empty string — never an empty/whitespace value.
    if token is not None:
        if not isinstance(token, str) or not token.strip():
            raise ConnectionError("token must be a non-empty string", status=400)
        setter = secret_setter or _keyring_setter
        try:
            setter(ORCHESTRATOR_TOKEN_ACCOUNT, token)
        except Exception as exc:
            raise ConnectionError(
                f"could not store the credential securely ({exc.__class__.__name__})",
                status=500) from exc

    health = probe_health(target, transport=transport)
    if not health["reachable"]:
        # Do not persist an endpoint we can't reach.
        return connection_status(env=env, base_dir=base_dir, transport=transport,
                                 secret_getter=secret_getter, probe=False) | {
            "status": "unreachable",
            "available": False,
            "endpoint": None,
            "headline": "Couldn't reach Hermes",
            "detail": health.get("detail")
            or "No local Hermes/Mochlet responded. Start Mochlet and retry.",
            "actions": [{"id": "retry_connect", "label": "Retry connection"}],
        }
    if not health["healthy"]:
        code = health.get("status_code")
        detail = ("The orchestrator rejected the credential." if code in (401, 403)
                  else health.get("detail") or "The orchestrator is unhealthy.")
        return {
            "status": "needs_token" if code in (401, 403) else "unreachable",
            "available": False,
            "endpoint": None,
            "suggested_endpoint": DEFAULT_MOCHLET_URL,
            "loopback": _is_loopback(target),
            "token_configured": bool((secret_getter or _keyring_getter)(ORCHESTRATOR_TOKEN_ACCOUNT)),
            "headline": "Hermes rejected the connection",
            "detail": detail,
            "actions": [{"id": "connect_hermes", "label": "Reconnect Hermes"}],
        }

    if persist:
        _persist_endpoint(target, base_dir=base_dir)
    # Re-read the authoritative status (config-only, no second probe needed).
    status = connection_status(env=env, base_dir=base_dir, transport=transport,
                               secret_getter=secret_getter, probe=True)
    return status


def disconnect(*, base_dir: Optional[Path] = None,
               secret_deleter: Optional[Callable[[str], bool]] = None,
               wipe_token: bool = False) -> dict:
    """Forget the persisted endpoint (and optionally the token)."""
    _clear_endpoint(base_dir)
    if wipe_token:
        deleter = secret_deleter or _keyring_deleter
        try:
            deleter(ORCHESTRATOR_TOKEN_ACCOUNT)
        except Exception:
            pass
    return connection_status(base_dir=base_dir, probe=False)


# --------------------------------------------------------------------------- #
# Live client build (what the adapter uses)
# --------------------------------------------------------------------------- #
def build_live_client(*, env: Optional[dict] = None, base_dir: Optional[Path] = None,
                      transport: Optional[Callable[..., Any]] = None):
    """Build the orchestrator client the adapter uses (env → persisted config).

    Returns a :class:`ConfiguredHermesOrchestratorClient` bound to the resolved
    endpoint (or ``None`` endpoint → fail-closed unavailable). ``transport`` is
    injectable for tests.
    """
    endpoint = configured_endpoint(env=env, base_dir=base_dir)
    return ConfiguredHermesOrchestratorClient(url=endpoint, transport=transport)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _is_loopback(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host in {"127.0.0.1", "::1", "localhost", "ip6-localhost"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _keyring_getter(account: str) -> Optional[str]:
    try:
        from lib import secret_store

        return secret_store.get_secret(account)
    except Exception:
        return None


def _keyring_setter(account: str, value: str) -> None:
    from lib import secret_store

    secret_store.set_secret(account, value)


def _keyring_deleter(account: str) -> bool:
    try:
        from lib import secret_store

        return secret_store.delete_secret(account)
    except Exception:
        return False
