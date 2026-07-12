"""Backlot data layer for the Hermes production brain (read + control).

Mirrors the ``providers_api`` / ``timeline_api`` convention: pure, synchronous
functions that take a ``project_dir`` / ``body`` and raise a typed error carrying
a ``.status``; ``backlot/server.py`` adds thin async routes that guard (CSRF +
origin + size), rate-limit, run off-thread, and translate the error.

Nothing here blocks: event reads are cursor-based snapshots (the board polls,
exactly like it polls ``/run``), so long polling never ties up a server thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from lib.production_brain import learning as _learn
from lib.production_brain.adapter import BrainUnavailable, default_adapter
from lib.production_brain.store import BrainStoreError, ProductionBrainStore

_MAX_EVENTS_PAGE = 500


class BrainApiError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _store(project_dir: Path) -> ProductionBrainStore:
    return ProductionBrainStore(project_dir)


def _require_run_id(body: dict) -> str:
    rid = body.get("run_id")
    if not isinstance(rid, str) or not rid:
        raise BrainApiError("run_id is required", status=400)
    return rid


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def build_run_payload(project_dir: Path) -> dict:
    """The canonical, materialized run state (with live elapsed on the active stage)."""
    return _store(project_dir).payload()


def read_events_payload(project_dir: Path, *, after: int = 0, limit: int = 200) -> dict:
    """Cursor page of the append-only event history. Non-blocking snapshot."""
    try:
        after = max(0, int(after))
    except (TypeError, ValueError):
        after = 0
    try:
        limit = max(1, min(_MAX_EVENTS_PAGE, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    store = _store(project_dir)
    events = store.read_events(after=after, limit=limit)
    max_seq = store._max_seq()
    next_cursor = events[-1]["seq"] if events else after
    return {
        "events": events,
        "cursor": after,
        "next_cursor": next_cursor,
        "latest_seq": max_seq,
        "count": len(events),
        "has_more": bool(events) and next_cursor < max_seq,
    }


def assets_payload(project_dir: Path) -> dict:
    """Every asset/artifact the run has produced so far, newest last."""
    st = _store(project_dir).read_state()
    outputs = st.get("outputs", [])
    return {"outputs": outputs, "count": len(outputs), "run_id": st.get("run_id"),
            "actual_duration_seconds": st.get("actual_duration_seconds")}


# --------------------------------------------------------------------------- #
# Run control
# --------------------------------------------------------------------------- #
def start_run(project_dir: Path, body: dict, *, adapter=None) -> dict:
    """Open a production run under the real Hermes brain (fail-closed).

    Records ``run_started`` under the brain's identity, or raises 409 if the
    brain is unavailable. Idempotent — an already-active run is returned as-is.
    Advancing stages is agent-driven (Rule Zero); this only opens the run.
    """
    adapter = adapter or default_adapter()
    store = _store(project_dir)
    target = _intake_target(project_dir)
    try:
        # No message override: the adapter emits the HONEST default — the run is
        # opened + a real session/job attached, but stages advance only as the
        # agent-driven brain works (no "online orchestrator" claim).
        state = adapter.start(store, requested_duration_seconds=target)
    except BrainUnavailable as exc:
        # Honest, structured blocker instead of a fabricated run.
        raise BrainApiError(str(exc), status=409)
    return state


def grant_approval(project_dir: Path, body: dict) -> dict:
    rid = _require_run_id(body)
    try:
        return _store(project_dir).grant_approval(
            rid, approval_id=body.get("approval_id"), stage=body.get("stage"),
            by=body.get("by") or "user", note=body.get("note"))
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def reject_approval(project_dir: Path, body: dict) -> dict:
    rid = _require_run_id(body)
    try:
        return _store(project_dir).reject_approval(
            rid, approval_id=body.get("approval_id"), stage=body.get("stage"),
            by=body.get("by") or "user", note=body.get("note"))
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def _client(orchestrator):
    if orchestrator is not None:
        return orchestrator
    # Control the SAME live orchestrator Start used (env/persisted Mochlet MCP),
    # not the bare env-only REST client — otherwise cancel/retry/resume could be
    # dispatched to the WRONG service. build_live_client is itself defensive and
    # returns a fail-closed client when unconfigured, so we do NOT swap in a
    # different (env-REST) client on error.
    from lib.production_brain.connection import build_live_client

    return build_live_client()


def _record_successor(store, run_id: str, stage: Optional[str], successor) -> None:
    """If a control produced a SUCCESSOR external job, record the new handle.

    Fail-closed at the persistence boundary: only a CANONICAL UUID handle is ever
    written into the run's live external handle (mirrors the initial-provision guard)."""
    from lib.production_brain.mochlet import is_uuid

    job_id = getattr(successor, "job_id", None)
    session_id = getattr(successor, "session_id", None)
    if not is_uuid(job_id):
        return
    if not is_uuid(session_id):
        session_id = None
    try:
        store.update_external_handle(
            run_id, session_id=session_id, job_id=job_id,
            stage=stage, message=f"Production continued under successor job {job_id}.")
    except BrainStoreError:
        pass


def cancel_run(project_dir: Path, body: dict, *, orchestrator=None) -> dict:
    """Cancel the exact active run, TRUTHFULLY correlated with the external job.

    For a run backed by a real external job, the external job is cancelled FIRST.
    Only on the orchestrator's acknowledgment is the local run marked terminal
    ``cancelled``. If the external cancel is unconfirmed (timeout/5xx/etc.), the
    run moves to a NON-terminal ``cancelling`` state with a ``control_unconfirmed``
    blocker so the user can retry — it is never reported as terminally cancelled
    on an unconfirmed external cancel. ``orchestrator`` is injectable for tests."""
    rid = _require_run_id(body)
    store = _store(project_dir)
    state = store.read_state()
    brain = state.get("brain") or {}
    is_external = state.get("run_id") == rid and brain.get("external") and brain.get("job_id")
    try:
        if is_external:
            job_id = brain["job_id"]
            try:
                _client(orchestrator).cancel_job(job_id=job_id)
            except Exception:
                # Unconfirmed → non-terminal, retryable. NOT cancelled.
                return store.request_cancel(
                    rid, stage=state.get("current_stage"),
                    message=f"External cancellation of job {job_id} is unconfirmed — retry.")
            return store.cancel(
                rid, message=f"Production run cancelled. Completed work is preserved. "
                             f"(external job {job_id} cancelled)")
        # No external job — a purely local run cancels terminally.
        return store.cancel(rid)
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def retry_stage(project_dir: Path, body: dict, *, orchestrator=None) -> dict:
    """Retry a stage. For an external run the orchestrator is told to retry FIRST;
    local state advances only on acknowledgment. On failure the run is blocked
    with a truthful ``control_unconfirmed`` blocker (no fake local retry)."""
    stage = body.get("stage")
    if not isinstance(stage, str) or not stage:
        raise BrainApiError("stage is required", status=400)
    store = _store(project_dir)
    state = store.read_state()
    brain = state.get("brain") or {}
    run_id = body.get("run_id")
    try:
        if brain.get("external"):
            # A lifecycle control on a real external job requires the EXACT active
            # run_id AND the caller's job_id to match the persisted handle — a
            # stale caller must not be able to control whichever run is active.
            job_id = _require_control_handle(state, brain, body)
            key = f"{state['run_id']}:retry:{stage}"
            try:
                successor = _client(orchestrator).control_job(job_id=job_id, action="retry", idempotency_key=key)
            except Exception:
                store.raise_blocker(stage, kind="control_unconfirmed",
                                   message=f"External retry of stage '{stage}' is unconfirmed — retry.",
                                   options=["Retry"])
                return store.read_state()
            _record_successor(store, state["run_id"], stage, successor)
        return store.retry_stage(stage, run_id=run_id)
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def resume_run(project_dir: Path, body: dict, *, orchestrator=None) -> dict:
    """Resume a run. For an external run the orchestrator is told to resume FIRST;
    local state advances only on acknowledgment (else a truthful blocker)."""
    store = _store(project_dir)
    state = store.read_state()
    brain = state.get("brain") or {}
    try:
        if brain.get("external"):
            job_id = _require_control_handle(state, brain, body)
            key = f"{state['run_id']}:resume"
            try:
                successor = _client(orchestrator).control_job(job_id=job_id, action="resume", idempotency_key=key)
            except Exception:
                from lib.production_brain.schema import STAGES

                store.raise_blocker(state.get("current_stage") or STAGES[0],
                                   kind="control_unconfirmed",
                                   message="External resume is unconfirmed — retry.",
                                   options=["Retry"])
                return store.read_state()
            _record_successor(store, state["run_id"], state.get("current_stage"), successor)
        return store.resume()
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def _require_control_handle(state: dict, brain: dict, body: dict) -> str:
    """Gate an external lifecycle control on the EXACT caller-supplied handles.

    Requires an active run, a body ``run_id`` that matches the active run, and a
    ``job_id`` that matches the persisted external handle (when the caller supplies
    one). Returns the validated persisted ``job_id``."""
    from lib.production_brain.schema import ACTIVE_RUN_STATES

    if state.get("state") not in ACTIVE_RUN_STATES:
        raise BrainApiError("There is no active run for this control action.", status=409)
    persisted_job = brain.get("job_id")
    if not persisted_job:
        raise BrainApiError("The active run has no external job handle to control.", status=409)
    body_run_id = body.get("run_id")
    if not isinstance(body_run_id, str) or not body_run_id:
        raise BrainApiError("run_id is required to control an external run", status=400)
    if body_run_id != state.get("run_id"):
        raise BrainApiError("Run id does not match the active run.", status=409)
    body_job_id = body.get("job_id")
    if body_job_id is not None and body_job_id != persisted_job:
        raise BrainApiError("Job id does not match the active run's external job.", status=409)
    return persisted_job


def _intake_target(project_dir: Path) -> Optional[int]:
    try:
        from lib.project_intake import read_intake

        intake = read_intake(project_dir) or {}
        val = intake.get("target_duration_seconds")
        return int(val) if val is not None else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Style learning (visible, auditable, reversible)
# --------------------------------------------------------------------------- #
def _learning_store(scope: str, project_dir: Optional[Path]) -> _learn.StyleLearningStore:
    if scope == "global":
        return _learn.StyleLearningStore.global_store()
    if scope == "project":
        if project_dir is None:
            raise BrainApiError("project scope requires a project", status=400)
        return _learn.StyleLearningStore.project_store(project_dir)
    raise BrainApiError("scope must be 'global' or 'project'", status=400)


def read_preferences(project_dir: Optional[Path] = None, *, scope: str = "all",
                     category: Optional[str] = None) -> dict:
    """Read learned preferences. ``scope`` = global | project | all (merged)."""
    result: dict[str, Any] = {"categories": list(_learn.CATEGORIES)}
    if scope in ("global", "all"):
        g = _learn.StyleLearningStore.global_store()
        result["global"] = {
            "opted_out": g.is_opted_out(),
            "preferences": g.preferences(category=category),
        }
    if scope in ("project", "all") and project_dir is not None:
        p = _learn.StyleLearningStore.project_store(project_dir)
        result["project"] = {
            "opted_out": p.is_opted_out(),
            "preferences": p.preferences(category=category),
        }
    return result


def update_preference(body: dict, *, project_dir: Optional[Path] = None, evidence: Any = None) -> dict:
    """Dispatch an explicit learning action.

    body = { action, scope, ... }. action ∈
    {learn, promote, correct, reject, delete, opt_out}.

    Honesty rules enforced here:
      * ``source`` is NEVER defaulted — a ``learn`` without an explicit
        approval/correction source is rejected.
      * ``learn`` is only allowed at PROJECT scope, and only when the run's
        authoritative event log actually contains a matching, non-rejected user
        approval/correction (verified via the injected/derived evidence checker).
      * a GLOBAL preference cannot be learned directly — it must be ``promote``d
        from a VERIFIED project preference (or edited via an explicit correction).
      * correct/reject/delete/opt_out are explicit user actions with provenance.
    """
    action = body.get("action")
    scope = body.get("scope") or "global"
    try:
        if action == "learn":
            source = body.get("source")
            if source not in ("approval", "correction"):
                raise BrainApiError("learn requires an explicit source of 'approval' or 'correction'")
            if scope != "project":
                raise BrainApiError(
                    "global preferences cannot be learned directly — promote a "
                    "verified project preference (action='promote') or record an "
                    "explicit correction", status=400)
            if project_dir is None:
                raise BrainApiError("project scope requires a project", status=400)
            store = _learn.StyleLearningStore.project_store(project_dir)
            ev = evidence
            if ev is None:
                from lib.production_brain.evidence import BrainLogEvidence

                ev = BrainLogEvidence(project_dir)
            return store.learn(
                category=body.get("category"), key=body.get("key"), value=body.get("value"),
                source=source, confidence=body.get("confidence", 0.5),
                run_id=body.get("run_id"), stage=body.get("stage"),
                decision_ref=body.get("decision_ref"), note=body.get("note"),
                require_evidence=True, evidence=ev)

        if action == "promote":
            if project_dir is None:
                raise BrainApiError("promotion requires the source project", status=400)
            pref_id = body.get("pref_id")
            if not isinstance(pref_id, str) or not pref_id:
                raise BrainApiError("promote requires pref_id (a verified project preference)")
            proj = _learn.StyleLearningStore.project_store(project_dir)
            src = proj.get(pref_id)
            if src is None or src.get("status") != "applied":
                raise BrainApiError("preference not found or not applied", status=404)
            if not (src.get("provenance") or {}).get("verified"):
                raise BrainApiError(
                    "only a verified (event-log-backed) project preference can be "
                    "promoted to global", status=409)
            g = _learn.StyleLearningStore.global_store()
            return g.record_promotion(category=src["category"], key=src["key"],
                                     value=src["value"], from_pref=pref_id,
                                     note=body.get("note"))

        store = _learning_store(scope, project_dir)
        if action == "correct":
            return store.correct(body.get("pref_id"), value=body.get("value"),
                                confidence=body.get("confidence", 0.6), run_id=body.get("run_id"),
                                stage=body.get("stage"), decision_ref=body.get("decision_ref"),
                                note=body.get("note"))
        if action == "reject":
            return store.reject(body.get("pref_id"), note=body.get("note"))
        if action == "delete":
            return store.delete(body.get("pref_id"))
        if action == "opt_out":
            return store.set_opt_out(bool(body.get("opted_out", True)), wipe=bool(body.get("wipe", False)))
        raise BrainApiError("unknown action; expected learn|promote|correct|reject|delete|opt_out")
    except _learn.LearningError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def reset_preferences(body: dict, *, project_dir: Optional[Path] = None) -> dict:
    scope = body.get("scope") or "global"
    store = _learning_store(scope, project_dir)
    return store.reset()
