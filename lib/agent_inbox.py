"""Consolidated view of work QUEUED for the OpenMontage agent (Rule Zero).

Backlot never generates media itself; it records honest, machine-readable requests
that the external agent picks up. This module aggregates those pending items across
a project so the operator can SEE exactly what the agent will do next:

  * queued per-layer regenerations (``revision_requests.json``),
  * a pending duration re-plan (``pending_replan`` flag on the timeline),
  * the production-run approval state (awaiting the user, or approved and awaiting
    the agent) from ``run.json``.

Read-only: it computes nothing and mutates nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from lib import revision_requests as _rev
from lib import timeline as _tl

try:  # production_run is optional in some minimal contexts
    from lib import production_run as _run
except Exception:  # pragma: no cover - defensive
    _run = None


def _summary(n_rev: int, replan: bool, approval: Optional[dict]) -> str:
    parts = []
    if n_rev:
        parts.append(f"{n_rev} layer regeneration{'s' if n_rev != 1 else ''}")
    if replan:
        parts.append("a duration re-plan")
    if approval and approval.get("needs") == "agent":
        parts.append("an approved plan to produce")
    if approval and approval.get("needs") == "user":
        parts.append("a plan awaiting your approval")
    if not parts:
        return "Nothing is queued for the agent."
    return "Queued for the agent: " + ", ".join(parts) + "."


def pending_agent_work(project_dir: Path) -> dict:
    """Aggregate everything currently waiting on the agent (or the user) for a project."""
    d = Path(project_dir)

    # 1) queued per-layer regenerations
    revisions = [
        {
            "id": r.get("id"),
            "layer_id": r.get("layer_id"),
            "layer_type": r.get("layer_type"),
            "prompt": r.get("prompt"),
            "created_at": r.get("created_at"),
        }
        for r in _rev.list_revisions(d)
        if isinstance(r, dict) and r.get("status") == "queued"
    ]

    # 2) pending duration re-plan (flag lives on the canonical timeline)
    try:
        tl, _tag = _tl.read_timeline(d)
    except Exception:  # pragma: no cover - defensive
        tl = None
    replan = bool(tl and tl.get("pending_replan"))

    # 3) production-run approval state
    approval: Optional[dict] = None
    if _run is not None:
        try:
            run = _run.read_run(d)
        except Exception:  # pragma: no cover - defensive
            run = None
        if isinstance(run, dict):
            if run.get("state") == "waiting_for_approval" and not run.get("plan_approved"):
                approval = {"state": "waiting_for_approval", "needs": "user",
                            "run_id": run.get("run_id")}
            elif run.get("plan_approved"):
                approval = {"state": "approved", "needs": "agent",
                            "run_id": run.get("run_id")}

    count = len(revisions) + (1 if replan else 0) + (1 if approval else 0)
    return {
        "revisions": revisions,
        "replan": replan,
        "approval": approval,
        "count": count,
        "summary": _summary(len(revisions), replan, approval),
    }
