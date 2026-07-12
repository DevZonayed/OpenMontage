"""Approval-evidence verification for learned style preferences.

A project-scoped learned preference may only be recorded when the authoritative
append-only production event log actually contains a matching user
approval/correction decision for that run + stage — one that has NOT been
rejected or superseded. This closes the hole where a client could assert
``source="approval"`` and have arbitrary preferences masquerade as user-approved
learning: the claim is checked against the log, not trusted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class ApprovalEvidence(Protocol):
    def verify(self, *, run_id: str, stage: str, decision_ref: str, source: str) -> bool:
        ...


class BrainLogEvidence:
    """Verifies a learning claim against a project's brain event log."""

    def __init__(self, project_dir: Path | str) -> None:
        self.project_dir = Path(project_dir)

    def _events(self) -> list[dict]:
        try:
            from lib.production_brain.store import ProductionBrainStore

            return ProductionBrainStore(self.project_dir).read_events_raw()
        except Exception:
            return []

    def verify(self, *, run_id: str, stage: str, decision_ref: str, source: str) -> bool:
        # All three anchors are mandatory: a claim missing any of them is
        # unverifiable and must be rejected by the caller before reaching here,
        # but we defend anyway.
        if not run_id or not stage or not decision_ref or source not in ("approval", "correction"):
            return False
        matched = False
        rejected = False
        for e in self._events():
            if e.get("run_id") != run_id:
                continue
            etype = e.get("type")
            est = e.get("stage")
            data = e.get("data") or {}
            # An approval can be later rejected — that rejection invalidates the
            # approval evidence even if the stage metadata drifted.
            if source == "approval" and etype == "approval_rejected" and data.get("approval_id") == decision_ref:
                rejected = True
            if est != stage:
                continue
            if source == "approval":
                # ONLY a granted approval, referenced by its approval_id, counts.
                if etype == "approval_granted" and data.get("approval_id") == decision_ref:
                    matched = True
            else:  # source == "correction"
                # ONLY a DISTINCT authoritative ``correction`` event counts — an
                # approval_granted or a generic ``decision`` event must NOT
                # masquerade as user-correction evidence.
                if etype == "correction" and (
                    data.get("decision_ref") == decision_ref
                    or data.get("correction_id") == decision_ref
                ):
                    matched = True
        return matched and not rejected


class NullEvidence:
    """Verifies nothing — used to prove that unverifiable claims are rejected."""

    def verify(self, *, run_id: str, stage: str, decision_ref: str, source: str) -> bool:  # noqa: ARG002
        return False
