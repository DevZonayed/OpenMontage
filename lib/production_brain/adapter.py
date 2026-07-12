"""Hermes brain adapter — the secure, explicit orchestrator contract.

A production run may only be opened once a REAL, durable orchestrator job/session
has been created through an injected orchestration port and has returned canonical
``session_id`` + ``job_id`` (see ``lib.production_brain.orchestrator``). The
adapter never fabricates those IDs. If no orchestrator is safely available, Start
Production **fails closed** with an actionable blocker rather than opening a run.

What the adapter does:

  1. Provisions a durable job through the injected
     :class:`~lib.production_brain.orchestrator.HermesOrchestratorClient` and
     records the **returned** canonical session/job identity (never minted).
  2. Fails closed when the orchestrator is unavailable or returns no valid IDs —
     it never runs an LLM, never fabricates progress, never calls a paid service.
  3. Stamps that non-secret identity onto every telemetry event and correlates
     cancellation with the external handle truthfully.

Two adapters ship:

  * :class:`HermesBrainAdapter` — the real one, backed by the orchestration port.
    Unconfigured on this machine → unavailable → fail-closed.
  * :class:`FakeBrain` — deterministic, offline, dependency-free, TEST-ONLY. It
    NEVER calls a paid service and is visibly labelled ``fake_driver``; it drives
    the whole state machine to prove visible stage/task changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from lib.production_brain import schema as S
from lib.production_brain.orchestrator import (
    HermesOrchestratorClient,
    OrchestratorUnavailable,
    default_orchestrator_client,
    is_canonical_id,
)
from lib.production_brain.store import ProductionBrainStore


class BrainUnavailable(RuntimeError):
    """Raised when the brain cannot orchestrate — the run must NOT proceed."""


@dataclass
class BrainIdentity:
    name: str = "hermes"
    adapter: str = "hermes"
    available: bool = False
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    engine: Optional[str] = None
    detail: Optional[str] = None
    # How this run is actually driven. Truthful, non-secret:
    #   "external_job" — a REAL durable orchestrator job was created and its
    #                    canonical session_id/job_id were returned by the external
    #                    service (never fabricated here).
    #   "fake_driver"  — the deterministic offline test driver (or a fake
    #                    orchestrator client). Visibly a fake — never a live job.
    orchestration: str = "external_job"

    def to_brain_block(self) -> dict:
        # Only non-secret identity fields ever leave this method.
        return {
            "name": self.name,
            "adapter": self.adapter,
            "available": bool(self.available),
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "engine": self.engine,
            "orchestration": self.orchestration,
        }


class BrainAdapter:
    """Base contract. Subclasses decide availability + identity + provisioning."""

    name = "brain"
    adapter_id = "base"
    # True when a run is backed by an external orchestrator client (live OR the
    # test fake), so cancellation can correlate with the external handle. False
    # for the pure offline driver, which has no external service to cancel.
    _external = False

    def available(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def identity(self) -> BrainIdentity:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict:
        ident = self.identity()
        return {"available": self.available(), "identity": ident.to_brain_block(),
                "detail": ident.detail}

    # Subclasses return (session_id, job_id, orchestration_label) from a REAL
    # source, or raise BrainUnavailable. The base implementation is for offline
    # test drivers only.
    def _provision(self, *, project_id: str, run_id: str,
                   requested_duration_seconds: Optional[int]) -> tuple[Optional[str], Optional[str], str]:
        ident = self.identity()
        return ident.session_id, None, ident.orchestration

    def start(
        self,
        store: ProductionBrainStore,
        *,
        run_id: Optional[str] = None,
        requested_duration_seconds: Optional[int] = None,
        message: Optional[str] = None,
    ) -> dict:
        """Open a run on ``store`` — or fail closed.

        A run is opened ONLY after a real orchestrator job/session has been
        provisioned (``_provision``) and returned canonical ids. The returned ids
        are recorded verbatim; nothing here fabricates identity. Idempotent: an
        already-active run keeps its existing external job (no second job is
        provisioned).
        """
        if not self.available():
            raise BrainUnavailable(
                f"{self.name} brain is unavailable; a production run cannot be "
                "orchestrated. Configure/sign in to the orchestrator and retry."
            )

        def _provisioner(rid: str):
            # Invoked by the store ONLY for the winner of the idempotency race,
            # while it holds the per-project lock — so exactly one external job is
            # ever created (the loser never reaches here). Raising here fails the
            # start closed without opening a run.
            session_id, job_id, orchestration = self._provision(
                project_id=store.project_dir.name, run_id=rid,
                requested_duration_seconds=requested_duration_seconds)
            ident = self.identity()
            brain_extra = {
                **ident.to_brain_block(),
                "session_id": session_id,
                "job_id": job_id,
                "orchestration": orchestration,
                "external": bool(self._external),
            }
            msg = message or self._start_message(ident, job_id, orchestration)
            # One-shot compensator: if the store fails to durably record the run
            # after this external job exists, cancel it exactly once.
            compensate = self._compensator(job_id) if self._external else None
            return session_id, job_id, brain_extra, msg, compensate

        return store.start_provisioned(
            provision=_provisioner,
            run_id=run_id,
            requested_duration_seconds=requested_duration_seconds,
            message=message,
        )

    def _compensator(self, job_id: Optional[str]):
        """Base: no external job to compensate (offline driver)."""
        return None

    def _start_message(self, ident: "BrainIdentity", job: Optional[str], orchestration: str) -> str:
        if orchestration == "external_job":
            eng = f" (engine {ident.engine})" if ident.engine else ""
            return (
                f"Production run opened under the {ident.name} brain{eng} — "
                f"attached to durable orchestrator job {job}. Stages advance as "
                "that job executes them."
            )
        return f"{ident.name} brain (fake driver, test-only) starting production."


# --------------------------------------------------------------------------- #
# Real Hermes adapter — backed by the injected orchestration port.
# --------------------------------------------------------------------------- #
class HermesBrainAdapter(BrainAdapter):
    """The real brain. Availability + identity come from the orchestration port.

    A run opens ONLY when the injected ``client`` reports it is available AND
    ``create_job`` returns canonical session/job ids. Unconfigured on this machine
    → unavailable → fail closed. The ``client`` is injectable so tests use the
    deterministic fake client (never a paid service).
    """

    name = "hermes"
    adapter_id = "hermes"

    def __init__(
        self,
        *,
        client: Optional[HermesOrchestratorClient] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        self._client = client if client is not None else default_orchestrator_client()
        self._agent_id = agent_id
        self._handle = None  # OrchestratorHandle, once a job is provisioned

    def available(self) -> bool:
        try:
            return bool(self._client) and bool(self._client.available())
        except Exception:
            return False

    @property
    def client(self) -> HermesOrchestratorClient:
        return self._client

    def identity(self) -> BrainIdentity:
        h = self._handle
        kind = getattr(self._client, "kind", "live")
        engine = (h.engine if h else getattr(self._client, "engine", None)) or ("hermes" if kind == "live" else "fake")
        orchestration = "external_job" if kind == "live" else "fake_driver"
        detail = (h.detail if h else None) or (
            "orchestrator endpoint configured" if self.available()
            else "no Hermes orchestrator is configured/available")
        return BrainIdentity(
            name="hermes",
            adapter="hermes",
            available=self.available(),
            agent_id=self._agent_id or (h.job_id if h else None),
            session_id=(h.session_id if h else None),
            engine=engine,
            detail=detail,
            orchestration=orchestration,
        )

    def _provision(self, *, project_id, run_id, requested_duration_seconds):
        try:
            handle = self._client.create_job(
                project_id=project_id, run_id=run_id,
                requested_duration_seconds=requested_duration_seconds,
                idempotency_key=f"{project_id}:{run_id}")
        except OrchestratorUnavailable as exc:
            raise BrainUnavailable(str(exc)) from exc
        except Exception as exc:  # any client fault → fail closed, no fabricated run
            raise BrainUnavailable(
                f"Hermes orchestrator could not create a job ({exc.__class__.__name__})."
            ) from exc
        # Enforce canonical id validation at the PERSISTENCE boundary — never trust
        # the client to have done it. A custom/misbehaving client returning
        # session_id="sess/1" or job_id="../x" is refused here, before run_started.
        sid = getattr(handle, "session_id", None)
        jid = getattr(handle, "job_id", None)
        if not is_canonical_id(sid) or not is_canonical_id(jid):
            raise BrainUnavailable(
                "Hermes orchestrator returned non-canonical session/job id; refusing "
                "to open a run without safe external ids.")
        self._handle = handle
        self._external = True  # an orchestrator client provisioned the job
        kind = getattr(self._client, "kind", "live")
        return sid, jid, ("external_job" if kind == "live" else "fake_driver")

    def cancel_external(self, *, job_id: Optional[str]) -> bool:
        """Best-effort: cancel the external job to keep the handle truthful.
        Returns True if the orchestrator acknowledged the cancel."""
        if not job_id:
            return False
        try:
            self._client.cancel_job(job_id=job_id)
            return True
        except Exception:
            return False

    def _compensator(self, job_id: Optional[str]):
        """A one-shot cancel of the just-provisioned external job, used by the
        store to undo an orphan if the local run_started write fails."""
        if not job_id:
            return None
        state = {"done": False}

        def _compensate():
            if state["done"]:
                return
            state["done"] = True
            self._client.cancel_job(job_id=job_id)  # may raise → store reports it

        return _compensate

    def control_external(self, *, job_id: Optional[str], action: str, idempotency_key: str) -> None:
        """Issue an explicit typed lifecycle action (retry/resume/cancel) to the
        external job. Raises OrchestratorUnavailable on failure (caller reports a
        truthful pending/blocked state instead of updating local state)."""
        if not job_id:
            raise OrchestratorUnavailable("no external job to control")
        self._client.control_job(job_id=job_id, action=action, idempotency_key=idempotency_key)


# --------------------------------------------------------------------------- #
# Deterministic offline brain — for tests + smoke. No paid services, ever.
# --------------------------------------------------------------------------- #
@dataclass
class _FakeStep:
    stage: str
    tool: Optional[str] = None
    provider: Optional[str] = None
    outputs: list[dict] = field(default_factory=list)
    approval: bool = False
    approval_prompt: Optional[str] = None


class FakeBrain(BrainAdapter):
    """A deterministic brain that drives the full stage machine offline.

    ``drive`` walks research→…→complete, emitting stage/tool/decision/output/
    approval events so the board shows visible, ordered stage & task changes.
    An ``approver`` callback (default: auto-approve) resolves approval gates so a
    non-interactive smoke run can complete; omit it to leave a gate pending.
    """

    name = "hermes"
    adapter_id = "fake"

    def __init__(self, *, agent_id: str = "fake-hermes-agent",
                 session_id: str = "fake-session-0001") -> None:
        self._agent_id = agent_id
        self._session_id = session_id

    def available(self) -> bool:
        return True

    def identity(self) -> BrainIdentity:
        return BrainIdentity(
            name="hermes", adapter="fake", available=True,
            agent_id=self._agent_id, session_id=self._session_id, engine="fake",
            orchestration="fake_driver",
            detail="deterministic offline brain (no paid services)",
        )

    def _provision(self, *, project_id, run_id, requested_duration_seconds):
        # Deterministic, visibly-fake ids — the offline driver genuinely drives
        # the run itself, so no external orchestrator is contacted.
        return self._session_id, f"fake-job-{run_id}", "fake_driver"

    def _plan(self, requested_duration_seconds: int) -> list[_FakeStep]:
        return [
            _FakeStep("research", tool="web_research", provider="local",
                      outputs=[{"kind": "artifact", "path": "artifacts/research_brief.json", "label": "Research brief"}]),
            _FakeStep("proposal", tool="proposal_writer", provider="hermes",
                      outputs=[{"kind": "artifact", "path": "artifacts/proposal_packet.json", "label": "Proposal"}],
                      approval=True, approval_prompt="Approve the concept + provider plan?"),
            _FakeStep("script", tool="script_writer", provider="hermes",
                      outputs=[{"kind": "artifact", "path": "artifacts/script.json", "label": "Script"}]),
            _FakeStep("scene_plan", tool="scene_planner", provider="hermes",
                      outputs=[{"kind": "artifact", "path": "artifacts/scene_plan.json", "label": "Scene plan"}]),
            _FakeStep("assets", tool="image_selector", provider="stub-image",
                      outputs=[{"kind": "image", "path": "assets/images/scene_01.png", "label": "Scene 1 still"}]),
            _FakeStep("narration", tool="tts_selector", provider="stub-tts",
                      outputs=[{"kind": "audio", "path": "assets/audio/narration.mp3", "label": "Narration"}]),
            _FakeStep("edit", tool="video_compose", provider="remotion",
                      outputs=[{"kind": "artifact", "path": "artifacts/edit_decisions.json", "label": "Edit decisions"}]),
            _FakeStep("render", tool="video_compose", provider="remotion",
                      outputs=[{"kind": "video", "path": "renders/final.mp4", "label": "Final render"}]),
            _FakeStep("review", tool="reviewer", provider="hermes",
                      outputs=[{"kind": "artifact", "path": "artifacts/final_review.json", "label": "Review"}]),
            _FakeStep("approval", tool=None, provider=None, approval=True,
                      approval_prompt="Approve the final cut for completion?"),
            _FakeStep("complete", tool=None, provider=None),
        ]

    def drive(
        self,
        store: ProductionBrainStore,
        *,
        requested_duration_seconds: int = 60,
        run_id: Optional[str] = None,
        approver: Optional[Callable[[dict], bool]] = "auto",  # type: ignore[assignment]
        stop_after: Optional[str] = None,
        step_delay: float = 0.0,
        sleep: Callable[[float], None] = None,  # type: ignore[assignment]
    ) -> dict:
        """Drive a full (or partial) run deterministically. Returns final state.

        ``approver`` may be:
          * ``"auto"`` (default) → auto-approve every gate;
          * a callable ``(state) -> bool`` → decide per gate;
          * ``None`` → leave the first gate pending (run stops at that gate).

        ``step_delay`` + injectable ``sleep`` space out stages for a live demo
        (default 0.0 — instant + deterministic for tests).
        """
        if sleep is None:
            import time as _time

            sleep = _time.sleep
        # The fake brain genuinely drives the run, so its honest default message
        # ("offline driver starting production") applies — no message override.
        state = self.start(store, run_id=run_id,
                           requested_duration_seconds=requested_duration_seconds)
        rid = state["run_id"]
        ident = self.identity()
        ev_kw = {"agent_id": ident.agent_id, "session_id": ident.session_id}
        actual = round(float(requested_duration_seconds), 3)

        for step in self._plan(requested_duration_seconds):
            store.enter_stage(step.stage,
                             message=f"Working on {S.STAGE_TITLES[step.stage]}.", **ev_kw)
            if step.tool:
                store.tool_call(step.stage, step.tool, provider=step.provider,
                               job_id=f"job-{step.stage}",
                               message=f"Calling {step.tool}"
                                       + (f" via {step.provider}" if step.provider else ""),
                               **ev_kw)
            store.stage_progress(step.stage, 0.5, message="Halfway.", **ev_kw)
            for out in step.outputs:
                kw = dict(out)
                store.output(step.stage, kind=kw.pop("kind"), **kw,
                            message=f"Produced {out.get('label') or out.get('kind')}.", **ev_kw)
            if step.approval:
                store.request_approval(step.stage, prompt=step.approval_prompt, **ev_kw)
                decide = None
                if approver == "auto":
                    decide = True
                elif callable(approver):
                    decide = bool(approver(store.read_state()))
                if decide is True:
                    store.grant_approval(rid, stage=step.stage, by="user")
                elif decide is False:
                    store.reject_approval(rid, stage=step.stage, by="user")
                    store.fail_run(rid, error=f"Approval rejected at {step.stage}.")
                    return store.read_state()
                else:
                    # Leave the gate pending — an interactive approval will resume it.
                    return store.read_state()
            if step.stage != "complete":
                store.complete_stage(step.stage,
                                    message=f"Completed {S.STAGE_TITLES[step.stage]}.", **ev_kw)
            if stop_after and step.stage == stop_after:
                return store.read_state()
            if step_delay:
                sleep(step_delay)

        return store.complete_run(rid, actual_duration_seconds=actual,
                                 message="Production complete — deliverable rendered.")


def default_adapter() -> BrainAdapter:
    """Return the brain adapter to use in production (real Hermes, fail-closed).

    The live orchestrator client is resolved through
    :mod:`lib.production_brain.connection` (env var → the endpoint persisted by a
    successful guided Connect), so connecting a local Mochlet actually enables
    Start/Continue production. Unconfigured on this machine → unavailable → the
    adapter still fails closed.
    """
    try:
        from lib.production_brain.connection import build_live_client

        return HermesBrainAdapter(client=build_live_client())
    except Exception:
        # Any resolution failure must not crash Start — fall back to the plain
        # env-var client, which is itself fail-closed when unconfigured.
        return HermesBrainAdapter()
