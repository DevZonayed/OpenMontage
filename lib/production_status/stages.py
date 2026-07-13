"""Canonical stage vocabulary for informational milestone labels.

OpenMontage's checkpoint pipeline records milestones under these stage names. The
Board renders them as INFORMATIONAL history (what has been planned/produced so
far) — never as an autonomous worker's live progress. Kept here, decoupled from
any run/automation machinery, so the read-only overview presenter has no
dependency on a production-run system.
"""

from __future__ import annotations

STAGES: tuple[str, ...] = (
    "research", "proposal", "script", "scene_plan", "assets", "narration",
    "edit", "render", "review", "approval", "complete",
)

STAGE_TITLES: dict[str, str] = {
    "research": "Research",
    "proposal": "Proposal",
    "script": "Script",
    "scene_plan": "Scene planning",
    "assets": "Asset generation",
    "narration": "Narration & music",
    "edit": "Editing",
    "render": "Rendering",
    "review": "Validation & review",
    "approval": "Approval",
    "complete": "Completion",
}
