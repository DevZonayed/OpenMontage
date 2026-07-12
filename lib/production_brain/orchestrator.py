"""Secure, explicit orchestration port for the Hermes production brain.

The brain does NOT fabricate agent identity. A production run may only be opened
once a real, durable orchestrator job/session has been created through an injected
:class:`HermesOrchestratorClient` and has returned canonical ``session_id`` +
``job_id``. If no orchestrator is safely available from this process, Start
Production **fails closed** with a visible, actionable blocker — it never invents
IDs, never copies credentials into telemetry, never shells out with user input,
and never calls a paid service in tests.

Transport hardening (a bearer token may ride the request, so the transport is
locked down):
  * endpoint policy is validated fail-closed — HTTPS only, with an explicit
    loopback-HTTP exception for a local Hermes/Mochlet; no embedded credentials,
    fragments, non-http schemes, ambiguous hosts, or control characters;
  * redirects are DISABLED and any 3xx is rejected, so a token is never replayed
    to a redirect target;
  * external ids are validated against a strict bounded allowlist before they are
    persisted or interpolated into a URL path (which is also percent-encoded).

Three pieces:
  * :class:`HermesOrchestratorClient` — the port (Protocol).
  * :class:`ConfiguredHermesOrchestratorClient` — the production client.
  * :class:`FakeOrchestratorClient` — deterministic, offline, TEST-ONLY.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

ORCHESTRATOR_URL_ENV = "OPENMONTAGE_HERMES_ORCHESTRATOR_URL"
ORCHESTRATOR_TOKEN_ACCOUNT = "hermes_orchestrator_token"  # keyring account name
_HTTP_TIMEOUT_SECONDS = 15

CONTROL_ACTIONS = ("retry", "resume", "cancel")

# Hosts allowed to speak plain HTTP (a local orchestrator only).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "ip6-localhost"}

# Strict, bounded canonical external-id allowlist: printable ASCII word chars plus
# a few id-safe punctuation marks. No slash/backslash/whitespace/control, and no
# ``..`` traversal (checked separately). 1..128 chars.
_CANON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


class OrchestratorUnavailable(RuntimeError):
    """No orchestrator could create/attach a durable job — the run must NOT open."""


def is_canonical_id(value: Any) -> bool:
    """True only for a safe, bounded external id (no traversal/control/whitespace)."""
    return isinstance(value, str) and bool(_CANON_ID_RE.match(value)) and ".." not in value


def validate_endpoint(url: Any) -> str:
    """Validate an orchestrator endpoint fail-closed; return it, or raise.

    HTTPS is required except for an explicit loopback host over HTTP (local
    Hermes/Mochlet). Rejects embedded credentials, fragments, non-http schemes,
    empty/ambiguous hosts, and any control/whitespace character."""
    if not isinstance(url, str) or not url:
        raise OrchestratorUnavailable(
            "no Hermes orchestrator is configured. Set "
            f"{ORCHESTRATOR_URL_ENV} to your approved orchestrator endpoint "
            "(and store its token via credential settings) to start real production runs.")
    if any((ord(c) < 0x20) or c.isspace() for c in url):
        raise OrchestratorUnavailable("orchestrator endpoint contains control/whitespace characters")
    # ALL parser access is wrapped: a malformed bracketed host or a bad port makes
    # ``.hostname``/``.port`` raise ValueError — every such failure must fail
    # closed as OrchestratorUnavailable, never bubble up (available() only catches
    # OrchestratorUnavailable) and never be silently accepted.
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        netloc = parts.netloc or ""
        username = parts.username
        password = parts.password
        fragment = parts.fragment
        hostname = parts.hostname
        port = parts.port  # raises ValueError for a non-numeric/out-of-range port
    except ValueError as exc:
        raise OrchestratorUnavailable(f"orchestrator endpoint is malformed ({exc.__class__.__name__})") from exc
    if scheme not in ("http", "https"):
        raise OrchestratorUnavailable("orchestrator endpoint must use http or https")
    if "@" in netloc or username or password:
        raise OrchestratorUnavailable("orchestrator endpoint must not embed credentials")
    if fragment:
        raise OrchestratorUnavailable("orchestrator endpoint must not contain a fragment")
    if isinstance(port, int) and not (0 < port <= 65535):
        raise OrchestratorUnavailable("orchestrator endpoint has an invalid port")
    host = (hostname or "").lower()
    if not host:
        raise OrchestratorUnavailable("orchestrator endpoint has no host")
    # Reject an ambiguous host (a bracket/colon that urlsplit could not parse into
    # a clean hostname, or a trailing dot).
    if host.endswith(".") or " " in host:
        raise OrchestratorUnavailable("orchestrator endpoint host is ambiguous")
    if scheme == "http":
        is_loopback = host in _LOOPBACK_HOSTS
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise OrchestratorUnavailable(
                "plain-HTTP orchestrator endpoint is only allowed on loopback; "
                "use https for a remote endpoint")
    return url


@dataclass(frozen=True)
class OrchestratorHandle:
    """Canonical identity returned by the orchestrator. Never fabricated locally."""

    session_id: str
    job_id: str
    engine: Optional[str] = None
    detail: Optional[str] = None

    def is_valid(self) -> bool:
        return is_canonical_id(self.session_id) and is_canonical_id(self.job_id)


@runtime_checkable
class HermesOrchestratorClient(Protocol):
    """The injected orchestration port.

    ``kind`` is ``"live"`` for a real external orchestrator and ``"fake"`` for the
    deterministic test client. ``create_job`` MUST be idempotent on
    ``idempotency_key``. ``control_job`` issues an explicit typed lifecycle action
    (retry/resume/cancel) to the external job."""

    kind: str

    def available(self) -> bool:
        ...

    def create_job(
        self,
        *,
        project_id: str,
        run_id: str,
        requested_duration_seconds: Optional[int],
        idempotency_key: str,
    ) -> OrchestratorHandle:
        ...

    def cancel_job(self, *, job_id: str) -> None:
        ...

    def control_job(self, *, job_id: str, action: str, idempotency_key: str) -> None:
        ...


# --------------------------------------------------------------------------- #
# Production client — talks to the operator-approved orchestrator endpoint.
# --------------------------------------------------------------------------- #
class ConfiguredHermesOrchestratorClient:
    """Real orchestrator client. Unconfigured/invalid endpoint → unavailable →
    the adapter fails closed.

    The bearer token lives ONLY in the OS keyring and is attached to the outbound
    request without ever being logged, returned, or written to telemetry. The
    transport disables redirects and rejects any 3xx so the token is never
    replayed to a redirect target. ``transport`` is injectable for tests (never a
    real network call in the suite)."""

    kind = "live"

    def __init__(self, *, url: Optional[str] = None, engine: str = "hermes",
                 transport: Optional[Callable[..., Any]] = None) -> None:
        self._url = url if url is not None else os.environ.get(ORCHESTRATOR_URL_ENV) or None
        self.engine = engine
        self._transport = transport

    def available(self) -> bool:
        try:
            validate_endpoint(self._url)
            return True
        except OrchestratorUnavailable:
            return False

    def _token(self) -> Optional[str]:
        try:
            from lib import secret_store

            return secret_store.get_secret(ORCHESTRATOR_TOKEN_ACCOUNT)
        except Exception:
            return None

    def _auth_headers(self, idempotency_key: Optional[str] = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _post(self, url: str, *, json: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
        """POST with redirects DISABLED; reject any 3xx (never replay the token)."""
        if self._transport is not None:
            resp = self._transport(url, json=json, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS)
        else:
            import requests  # a declared dependency

            resp = requests.post(url, json=json, headers=headers,
                                 timeout=_HTTP_TIMEOUT_SECONDS, allow_redirects=False)
        status = int(getattr(resp, "status_code", 0) or 0)
        if 300 <= status < 400:
            raise OrchestratorUnavailable(
                f"orchestrator returned a redirect (HTTP {status}); refusing to follow "
                "(a bearer token must never be replayed to a redirect target)")
        if status >= 400:
            raise OrchestratorUnavailable(f"orchestrator rejected the request (HTTP {status})")
        return resp

    def create_job(self, *, project_id, run_id, requested_duration_seconds, idempotency_key) -> OrchestratorHandle:
        base = validate_endpoint(self._url)  # fail closed on a bad endpoint
        try:
            resp = self._post(
                base.rstrip("/") + "/jobs",
                json={
                    "project_id": project_id,
                    "run_id": run_id,
                    "requested_duration_seconds": requested_duration_seconds,
                    "idempotency_key": idempotency_key,
                },
                headers=self._auth_headers(idempotency_key),
            )
        except OrchestratorUnavailable:
            raise
        except Exception as exc:  # network / library error — sanitized, no secrets
            raise OrchestratorUnavailable(
                f"Hermes orchestrator request failed ({exc.__class__.__name__}).") from exc
        try:
            body = resp.json()
        except Exception as exc:
            raise OrchestratorUnavailable("Hermes orchestrator returned a non-JSON response.") from exc
        if not isinstance(body, dict):
            raise OrchestratorUnavailable("Hermes orchestrator returned an unexpected payload.")
        sid = body.get("session_id")
        jid = body.get("job_id")
        # NEVER coerce arbitrary objects with str(): a non-string id is invalid.
        if not isinstance(sid, str) or not isinstance(jid, str):
            raise OrchestratorUnavailable("Hermes orchestrator returned non-string session/job ids.")
        eng = body.get("engine")
        handle = OrchestratorHandle(
            session_id=sid, job_id=jid,
            engine=eng if isinstance(eng, str) else self.engine,
            detail="external Hermes orchestrator job")
        if not handle.is_valid():
            raise OrchestratorUnavailable(
                "Hermes orchestrator returned non-canonical session/job ids; refusing "
                "to open a run without safe external ids.")
        return handle

    def cancel_job(self, *, job_id: str) -> None:
        self.control_job(job_id=job_id, action="cancel", idempotency_key=f"cancel:{job_id}")

    def control_job(self, *, job_id: str, action: str, idempotency_key: str) -> None:
        base = validate_endpoint(self._url)
        if action not in CONTROL_ACTIONS:
            raise OrchestratorUnavailable(f"unsupported control action: {action}")
        if not is_canonical_id(job_id):
            raise OrchestratorUnavailable("refusing to control a non-canonical job id")
        # Defensive percent-encoding of the path segment even though job_id is
        # already validated to a safe allowlist.
        url = base.rstrip("/") + f"/jobs/{quote(job_id, safe='')}/{action}"
        try:
            self._post(url, json={"idempotency_key": idempotency_key},
                      headers=self._auth_headers(idempotency_key))
        except OrchestratorUnavailable:
            raise
        except Exception as exc:
            raise OrchestratorUnavailable(
                f"Hermes orchestrator {action} failed ({exc.__class__.__name__}).") from exc


# --------------------------------------------------------------------------- #
# Deterministic, offline, TEST-ONLY client.
# --------------------------------------------------------------------------- #
class FakeOrchestratorClient:
    """Deterministic offline orchestrator for tests + the integration smoke.

    Returns canonical-shaped ids derived from the run id (idempotent on the key)
    and records the create/cancel/control calls it receives. NEVER used in
    production and NEVER calls a paid service."""

    kind = "fake"

    def __init__(self, *, engine: str = "fake", available: bool = True,
                 fail_control: bool = False) -> None:
        self.engine = engine
        self._available = available
        self._fail_control = fail_control
        self.created: dict[str, OrchestratorHandle] = {}
        self.cancelled: list[str] = []
        self.controls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def create_job(self, *, project_id, run_id, requested_duration_seconds, idempotency_key) -> OrchestratorHandle:
        if idempotency_key in self.created:
            return self.created[idempotency_key]
        handle = OrchestratorHandle(
            session_id=f"fake-sess-{run_id}",
            job_id=f"fake-job-{run_id}",
            engine=self.engine,
            detail="deterministic fake orchestrator (test-only)")
        self.created[idempotency_key] = handle
        return handle

    def control_job(self, *, job_id: str, action: str, idempotency_key: str) -> None:
        self.controls.append({"job_id": job_id, "action": action, "idempotency_key": idempotency_key})
        if action == "cancel":
            self.cancelled.append(job_id)
        if self._fail_control:
            raise OrchestratorUnavailable(f"fake orchestrator {action} failed")

    def cancel_job(self, *, job_id: str) -> None:
        self.control_job(job_id=job_id, action="cancel", idempotency_key=f"cancel:{job_id}")


def default_orchestrator_client() -> HermesOrchestratorClient:
    """The production orchestrator client (unconfigured here → fail-closed)."""
    return ConfiguredHermesOrchestratorClient()
