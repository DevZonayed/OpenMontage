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
        state = adapter.start(store, requested_duration_seconds=target,
                             message="Start Production — Hermes brain online.")
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


def cancel_run(project_dir: Path, body: dict) -> dict:
    rid = _require_run_id(body)
    try:
        return _store(project_dir).cancel(rid)
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def retry_stage(project_dir: Path, body: dict) -> dict:
    stage = body.get("stage")
    if not isinstance(stage, str) or not stage:
        raise BrainApiError("stage is required", status=400)
    try:
        return _store(project_dir).retry_stage(stage, run_id=body.get("run_id"))
    except BrainStoreError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def resume_run(project_dir: Path, body: dict) -> dict:
    return _store(project_dir).resume()


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


def update_preference(body: dict, *, project_dir: Optional[Path] = None) -> dict:
    """Dispatch an explicit learning action.

    body = { action, scope, ... }. action ∈
    {learn, correct, reject, delete, opt_out}. All learning is from explicit
    user choices only (enforced by the store).
    """
    action = body.get("action")
    scope = body.get("scope") or "global"
    store = _learning_store(scope, project_dir)
    try:
        if action == "learn":
            return store.learn(
                category=body.get("category"), key=body.get("key"), value=body.get("value"),
                source=body.get("source") or "approval", confidence=body.get("confidence", 0.5),
                run_id=body.get("run_id"), stage=body.get("stage"),
                decision_ref=body.get("decision_ref"), note=body.get("note"),
                corrects=body.get("corrects"))
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
        raise BrainApiError("unknown action; expected learn|correct|reject|delete|opt_out")
    except _learn.LearningError as exc:
        raise BrainApiError(str(exc), status=exc.status)


def reset_preferences(body: dict, *, project_dir: Optional[Path] = None) -> dict:
    scope = body.get("scope") or "global"
    store = _learning_store(scope, project_dir)
    return store.reset()
