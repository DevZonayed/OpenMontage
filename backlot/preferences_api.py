"""Backlot data layer for learned Style preferences (visible, auditable, reversible).

This is the ONLY surviving piece of the former "production brain" API: the Studio's
Style panel reads and edits durable style preferences. There is no production-run
automation here — OpenMontage is manual-first. Pure, synchronous functions; the
async routes in ``backlot/server.py`` add the guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from lib.production_brain import learning as _learn


class PreferencesApiError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _learning_store(scope: str, project_dir: Optional[Path]) -> _learn.StyleLearningStore:
    if scope == "global":
        return _learn.StyleLearningStore.global_store()
    if scope == "project":
        if project_dir is None:
            raise PreferencesApiError("project scope requires a project", status=400)
        return _learn.StyleLearningStore.project_store(project_dir)
    raise PreferencesApiError("scope must be 'global' or 'project'", status=400)


def read_preferences(project_dir: Optional[Path] = None, *, scope: str = "all",
                     category: Optional[str] = None) -> dict:
    """Read learned preferences. ``scope`` = global | project | all (merged)."""
    result: dict[str, Any] = {"categories": list(_learn.CATEGORIES)}
    if scope in ("global", "all"):
        g = _learn.StyleLearningStore.global_store()
        result["global"] = {"opted_out": g.is_opted_out(),
                            "preferences": g.preferences(category=category)}
    if scope in ("project", "all") and project_dir is not None:
        p = _learn.StyleLearningStore.project_store(project_dir)
        result["project"] = {"opted_out": p.is_opted_out(),
                            "preferences": p.preferences(category=category)}
    return result


def update_preference(body: dict, *, project_dir: Optional[Path] = None, evidence: Any = None) -> dict:
    """Dispatch an explicit learning action (learn|promote|correct|reject|delete|opt_out)."""
    action = body.get("action")
    scope = body.get("scope") or "global"
    try:
        if action == "learn":
            source = body.get("source")
            if source not in ("approval", "correction"):
                raise PreferencesApiError("learn requires an explicit source of 'approval' or 'correction'")
            if scope != "project":
                raise PreferencesApiError(
                    "global preferences cannot be learned directly — promote a verified "
                    "project preference (action='promote') or record an explicit correction",
                    status=400)
            if project_dir is None:
                raise PreferencesApiError("project scope requires a project", status=400)
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
                raise PreferencesApiError("promotion requires the source project", status=400)
            pref_id = body.get("pref_id")
            if not isinstance(pref_id, str) or not pref_id:
                raise PreferencesApiError("promote requires pref_id (a verified project preference)")
            proj = _learn.StyleLearningStore.project_store(project_dir)
            src = proj.get(pref_id)
            if src is None or src.get("status") != "applied":
                raise PreferencesApiError("preference not found or not applied", status=404)
            if not (src.get("provenance") or {}).get("verified"):
                raise PreferencesApiError(
                    "only a verified (event-log-backed) project preference can be promoted to global",
                    status=409)
            g = _learn.StyleLearningStore.global_store()
            return g.record_promotion(category=src["category"], key=src["key"],
                                     value=src["value"], from_pref=pref_id, note=body.get("note"))

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
        raise PreferencesApiError("unknown action; expected learn|promote|correct|reject|delete|opt_out")
    except _learn.LearningError as exc:
        raise PreferencesApiError(str(exc), status=exc.status)


def reset_preferences(body: dict, *, project_dir: Optional[Path] = None) -> dict:
    scope = body.get("scope") or "global"
    store = _learning_store(scope, project_dir)
    return store.reset()
