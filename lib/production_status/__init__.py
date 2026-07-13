"""Read-only project overview view model.

OpenMontage is manual-first: the Board renders a plain-language overview of the
on-disk artifacts (checkpoint milestones + timeline + outputs). There is no
autonomous production worker. See :mod:`lib.production_status.presenter`.
"""

from __future__ import annotations

from lib.production_status.presenter import (
    CANONICAL_STAGES,
    CANONICAL_STAGE_COUNT,
    LEGACY_STAGE_MAP,
    build_status_view,
    canonical_stage,
    canonical_stage_index,
)

__all__ = [
    "CANONICAL_STAGES",
    "CANONICAL_STAGE_COUNT",
    "LEGACY_STAGE_MAP",
    "build_status_view",
    "canonical_stage",
    "canonical_stage_index",
]
