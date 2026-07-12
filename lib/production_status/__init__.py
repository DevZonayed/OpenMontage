"""Canonical production-status view model.

One presenter, consumed by both the Backlot board and the Remotion Studio, so the
two surfaces can never disagree about *where the production is* or *what to do
next*. See :mod:`lib.production_status.presenter`.
"""

from __future__ import annotations

from lib.production_status.presenter import (
    CANONICAL_STAGES,
    CANONICAL_STAGE_COUNT,
    LEGACY_STAGE_MAP,
    OVERALL_STATES,
    build_status_view,
    canonical_stage,
    canonical_stage_index,
)

__all__ = [
    "CANONICAL_STAGES",
    "CANONICAL_STAGE_COUNT",
    "LEGACY_STAGE_MAP",
    "OVERALL_STATES",
    "build_status_view",
    "canonical_stage",
    "canonical_stage_index",
]
