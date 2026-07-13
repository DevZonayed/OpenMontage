"""Secure, explicit orchestration port for the production agent.

The production agent does NOT fabricate identity. A production run may only be
opened once a real, durable session has been created through an injected
:class:`HermesOrchestratorClient` and has returned a canonical ``session_id`` +
``job_id``. If no agent is safely available from this process, Start Production
**fails closed** with a visible, actionable blocker — it never invents IDs, never
copies credentials into telemetry, never shells out with user input, and never
calls a paid service in tests.

This module is the OpenMontage-owned adapter SEAM so the backend can evolve. The
concrete live implementation is the native Hermes Agent client in
:mod:`lib.production_brain.hermes_agent`; the deterministic
:class:`FakeOrchestratorClient` here is TEST-ONLY.

External ids are validated against a strict, bounded allowlist before they are
persisted or interpolated anywhere (no traversal, control, or whitespace).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

CONTROL_ACTIONS = ("retry", "resume", "cancel")

# Strict, bounded canonical external-id allowlist: printable ASCII word chars plus
# a few id-safe punctuation marks. No slash/backslash/whitespace/control, and no
# ``..`` traversal (checked separately). 1..128 chars.
_CANON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


class OrchestratorUnavailable(RuntimeError):
    """No agent could create/attach a durable session — the run must NOT open."""


def is_canonical_id(value: Any) -> bool:
    """True only for a safe, bounded external id (no traversal/control/whitespace)."""
    return isinstance(value, str) and bool(_CANON_ID_RE.match(value)) and ".." not in value


@dataclass(frozen=True)
class OrchestratorHandle:
    """Canonical identity returned by the agent. Never fabricated locally."""

    session_id: str
    job_id: str
    engine: Optional[str] = None
    detail: Optional[str] = None

    def is_valid(self) -> bool:
        return is_canonical_id(self.session_id) and is_canonical_id(self.job_id)


@runtime_checkable
class HermesOrchestratorClient(Protocol):
    """The injected orchestration port.

    ``kind`` is ``"live"`` for a real agent and ``"fake"`` for the deterministic
    test client. ``create_job`` MUST be idempotent on ``idempotency_key``.
    ``control_job`` issues an explicit typed lifecycle action (retry/resume/cancel)
    to the external session."""

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
# Deterministic, offline, TEST-ONLY client.
# --------------------------------------------------------------------------- #
class FakeOrchestratorClient:
    """Deterministic offline agent for tests + the integration smoke.

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
            detail="deterministic fake agent (test-only)")
        self.created[idempotency_key] = handle
        return handle

    def control_job(self, *, job_id: str, action: str, idempotency_key: str) -> None:
        self.controls.append({"job_id": job_id, "action": action, "idempotency_key": idempotency_key})
        if action == "cancel":
            self.cancelled.append(job_id)
        if self._fail_control:
            raise OrchestratorUnavailable(f"fake agent {action} failed")

    def cancel_job(self, *, job_id: str) -> None:
        self.control_job(job_id=job_id, action="cancel", idempotency_key=f"cancel:{job_id}")


def default_orchestrator_client() -> HermesOrchestratorClient:
    """The production orchestration client — the native Hermes Agent, fail-closed
    when it is not configured on this machine."""
    from lib.production_brain.hermes_agent import build_live_client

    return build_live_client()
