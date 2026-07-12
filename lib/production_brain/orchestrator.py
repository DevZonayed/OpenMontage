"""Secure, explicit orchestration port for the Hermes production brain.

The brain does NOT fabricate agent identity. A production run may only be opened
once a real, durable orchestrator job/session has been created through an injected
:class:`HermesOrchestratorClient` and has returned canonical ``session_id`` +
``job_id``. If no orchestrator is safely available from this process, Start
Production **fails closed** with a visible, actionable blocker — it never invents
IDs, never copies credentials into telemetry, never shells out with user input,
and never calls a paid service in tests.

Three pieces:

  * :class:`HermesOrchestratorClient` — the port (Protocol). Any real integration
    implements it; it is injected into the adapter so nothing here is hard-wired.
  * :class:`ConfiguredHermesOrchestratorClient` — the production client. It talks
    to the operator-approved orchestrator endpoint (config + a keyring token) and
    returns the canonical IDs the external service assigned. Unconfigured →
    ``available() is False`` → the adapter fails closed. It is NEVER exercised by
    the test suite.
  * :class:`FakeOrchestratorClient` — deterministic, offline, TEST-ONLY. It
    returns canonical-shaped IDs derived from the run id and records the
    start/cancel calls it receives. Runs driven by it are visibly labelled
    ``fake_driver`` so a fake can never masquerade as a live external job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

# Config + secret plumbing (non-secret config in env; the token stays in the OS
# keyring and is NEVER logged or placed in telemetry).
import os

ORCHESTRATOR_URL_ENV = "OPENMONTAGE_HERMES_ORCHESTRATOR_URL"
ORCHESTRATOR_TOKEN_ACCOUNT = "hermes_orchestrator_token"  # keyring account name
_HTTP_TIMEOUT_SECONDS = 15


class OrchestratorUnavailable(RuntimeError):
    """No orchestrator could create/attach a durable job — the run must NOT open."""


@dataclass(frozen=True)
class OrchestratorHandle:
    """Canonical identity returned by the orchestrator. Never fabricated locally."""

    session_id: str
    job_id: str
    engine: Optional[str] = None
    detail: Optional[str] = None

    def is_valid(self) -> bool:
        return bool(self.session_id) and bool(self.job_id)


@runtime_checkable
class HermesOrchestratorClient(Protocol):
    """The injected orchestration port.

    ``kind`` is ``"live"`` for a real external orchestrator and ``"fake"`` for the
    deterministic test client — the adapter surfaces it so a run is truthfully
    labelled. ``create_job`` MUST be idempotent on ``idempotency_key`` (a retry
    with the same key returns the SAME external job)."""

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


# --------------------------------------------------------------------------- #
# Production client — talks to the operator-approved orchestrator endpoint.
# --------------------------------------------------------------------------- #
class ConfiguredHermesOrchestratorClient:
    """Real orchestrator client. Unconfigured → unavailable → adapter fails closed.

    Config is non-secret (an endpoint URL via env). The bearer token, if any,
    lives ONLY in the OS keyring and is attached to the outbound request without
    ever being logged, returned, or written to telemetry."""

    kind = "live"

    def __init__(self, *, url: Optional[str] = None, engine: str = "hermes") -> None:
        self._url = url if url is not None else os.environ.get(ORCHESTRATOR_URL_ENV) or None
        self.engine = engine

    def available(self) -> bool:
        # Availability is purely "is an endpoint configured?" — a cheap, side-effect
        # free check. Whether the endpoint actually issues a job is proven by
        # create_job (which raises OrchestratorUnavailable on any failure).
        return bool(self._url)

    def _token(self) -> Optional[str]:
        try:
            from lib import secret_store

            return secret_store.get_secret(ORCHESTRATOR_TOKEN_ACCOUNT)
        except Exception:
            return None

    def create_job(self, *, project_id, run_id, requested_duration_seconds, idempotency_key) -> OrchestratorHandle:
        if not self._url:
            raise OrchestratorUnavailable(
                "No Hermes orchestrator is configured. Set "
                f"{ORCHESTRATOR_URL_ENV} to your approved orchestrator endpoint "
                "(and store its token via the credential settings) to start real "
                "production runs."
            )
        try:
            import requests  # a declared dependency

            headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
            token = self._token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(
                self._url.rstrip("/") + "/jobs",
                json={
                    "project_id": project_id,
                    "run_id": run_id,
                    "requested_duration_seconds": requested_duration_seconds,
                    "idempotency_key": idempotency_key,
                },
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except OrchestratorUnavailable:
            raise
        except Exception as exc:  # network / library error — sanitized, no secrets
            raise OrchestratorUnavailable(
                f"Hermes orchestrator request failed ({exc.__class__.__name__})."
            ) from exc
        if resp.status_code >= 400:
            raise OrchestratorUnavailable(
                f"Hermes orchestrator rejected the job (HTTP {resp.status_code})."
            )
        try:
            body = resp.json()
        except Exception as exc:
            raise OrchestratorUnavailable("Hermes orchestrator returned a non-JSON response.") from exc
        handle = OrchestratorHandle(
            session_id=str(body.get("session_id") or ""),
            job_id=str(body.get("job_id") or ""),
            engine=body.get("engine") or self.engine,
            detail="external Hermes orchestrator job",
        )
        if not handle.is_valid():
            raise OrchestratorUnavailable(
                "Hermes orchestrator did not return canonical session_id/job_id; "
                "refusing to open a run without a real external job."
            )
        return handle

    def cancel_job(self, *, job_id: str) -> None:
        if not self._url or not job_id:
            raise OrchestratorUnavailable("No orchestrator/job to cancel.")
        try:
            import requests

            headers = {"Content-Type": "application/json"}
            token = self._token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(
                self._url.rstrip("/") + f"/jobs/{job_id}/cancel",
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise OrchestratorUnavailable(
                f"Hermes orchestrator cancel failed ({exc.__class__.__name__})."
            ) from exc
        if resp.status_code >= 400:
            raise OrchestratorUnavailable(
                f"Hermes orchestrator cancel rejected (HTTP {resp.status_code})."
            )


# --------------------------------------------------------------------------- #
# Deterministic, offline, TEST-ONLY client.
# --------------------------------------------------------------------------- #
class FakeOrchestratorClient:
    """Deterministic offline orchestrator for tests + the integration smoke.

    Returns canonical-shaped IDs derived from the run id (idempotent on the key)
    and records the calls it receives. NEVER used in production and NEVER calls a
    paid service. Runs backed by it are labelled ``fake_driver``."""

    kind = "fake"

    def __init__(self, *, engine: str = "fake", available: bool = True) -> None:
        self.engine = engine
        self._available = available
        self.created: dict[str, OrchestratorHandle] = {}
        self.cancelled: list[str] = []

    def available(self) -> bool:
        return self._available

    def create_job(self, *, project_id, run_id, requested_duration_seconds, idempotency_key) -> OrchestratorHandle:
        # Idempotent on the key: a retry returns the SAME handle.
        if idempotency_key in self.created:
            return self.created[idempotency_key]
        handle = OrchestratorHandle(
            session_id=f"fake-sess-{run_id}",
            job_id=f"fake-job-{run_id}",
            engine=self.engine,
            detail="deterministic fake orchestrator (test-only)",
        )
        self.created[idempotency_key] = handle
        return handle

    def cancel_job(self, *, job_id: str) -> None:
        self.cancelled.append(job_id)


def default_orchestrator_client() -> HermesOrchestratorClient:
    """The production orchestrator client (unconfigured here → fail-closed)."""
    return ConfiguredHermesOrchestratorClient()
