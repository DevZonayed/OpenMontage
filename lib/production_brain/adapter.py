"""Hermes brain adapter — the secure, explicit orchestrator contract.

The *brain* is the intelligence that drives a production run through its stages.
OpenMontage has NO internal LLM-calling layer by design (Rule Zero: production is
agent-driven), so this adapter does exactly two honest things:

  1. Establishes **stable, non-secret identity** (name / adapter / agent_id /
     session_id / backing engine) that gets stamped onto every telemetry event,
     so an observer always knows *which* brain/session/job did each task.
  2. **Fails closed** when Hermes is unavailable — it never fabricates an LLM,
     never silently degrades to a stub, and (optionally) records an honest
     ``brain_unavailable`` blocker instead of pretending to make progress.

Two adapters ship:

  * :class:`HermesBrainAdapter` — the real one. Availability is probed from the
    subscription-engine layer (``lib.engines``): the Hermes brain is "available"
    only when a consumer-plan engine is actually logged in and ready. No probe,
    no run.
  * :class:`FakeBrain` — deterministic, offline, dependency-free. It NEVER calls
    a paid service. Used by tests and the smoke harness to drive the whole state
    machine and prove visible stage/task changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from lib.production_brain import schema as S
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

    def to_brain_block(self) -> dict:
        # Only non-secret identity fields ever leave this method.
        return {
            "name": self.name,
            "adapter": self.adapter,
            "available": bool(self.available),
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "engine": self.engine,
        }


class BrainAdapter:
    """Base contract. Subclasses decide availability + identity."""

    name = "brain"
    adapter_id = "base"

    def available(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def identity(self) -> BrainIdentity:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict:
        ident = self.identity()
        return {"available": self.available(), "identity": ident.to_brain_block(),
                "detail": ident.detail}

    def start(
        self,
        store: ProductionBrainStore,
        *,
        run_id: Optional[str] = None,
        requested_duration_seconds: Optional[int] = None,
        message: Optional[str] = None,
    ) -> dict:
        """Start a run on ``store`` under this brain's identity — or fail closed.

        If the brain is unavailable this raises :class:`BrainUnavailable` and
        does NOT start a run (no fabricated progress).
        """
        if not self.available():
            raise BrainUnavailable(
                f"{self.name} brain is unavailable; a production run cannot be "
                "orchestrated. Sign in to the brain engine and retry."
            )
        ident = self.identity()
        return store.start(
            run_id=run_id,
            brain=ident.to_brain_block(),
            requested_duration_seconds=requested_duration_seconds,
            agent_id=ident.agent_id,
            session_id=ident.session_id,
            message=message,
        )


# --------------------------------------------------------------------------- #
# Real Hermes adapter — availability probed from the subscription-engine layer.
# --------------------------------------------------------------------------- #
def _default_probe() -> dict:
    """Non-secret availability probe. Returns {available, engine, detail}."""
    try:
        from lib import engines as _eng

        summary = _eng.engines_summary(probe_auth=True)
    except Exception as exc:  # engines layer or CLI missing → unavailable
        return {"available": False, "engine": None, "detail": f"engine probe failed: {exc.__class__.__name__}"}
    ready = list(summary.get("subscription_ready") or [])
    # Hermes prefers the Claude engine but any ready subscription engine can back it.
    engine = "claude" if "claude" in ready else (ready[0] if ready else None)
    return {
        "available": bool(ready),
        "engine": engine,
        "detail": (f"brain backed by '{engine}' subscription engine"
                   if engine else "no subscription-ready brain engine is signed in"),
    }


class HermesBrainAdapter(BrainAdapter):
    """The real brain. Available only when a subscription engine is signed in.

    ``probe`` is injectable (no wall-clock / no secrets) so availability can be
    driven deterministically in tests. ``session_id`` is derived from a caller-
    supplied stable token (never random) so identity is reproducible per run.
    """

    name = "hermes"
    adapter_id = "hermes"

    def __init__(
        self,
        *,
        probe: Callable[[], dict] = _default_probe,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        self._probe = probe
        self._session_id = session_id
        self._agent_id = agent_id
        self._cache: Optional[dict] = None

    def _probed(self) -> dict:
        if self._cache is None:
            try:
                self._cache = dict(self._probe() or {})
            except Exception:
                self._cache = {"available": False, "engine": None, "detail": "probe error"}
        return self._cache

    def available(self) -> bool:
        return bool(self._probed().get("available"))

    def identity(self) -> BrainIdentity:
        p = self._probed()
        engine = p.get("engine")
        return BrainIdentity(
            name="hermes",
            adapter="hermes",
            available=bool(p.get("available")),
            agent_id=self._agent_id or (f"hermes:{engine}" if engine else None),
            session_id=self._session_id,
            engine=engine,
            detail=p.get("detail"),
        )


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
            detail="deterministic offline brain (no paid services)",
        )

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
        state = self.start(store, run_id=run_id,
                           requested_duration_seconds=requested_duration_seconds,
                           message="Hermes brain online — starting production.")
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
    """Return the brain adapter to use in production (real Hermes, fail-closed)."""
    return HermesBrainAdapter()
