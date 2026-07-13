"""Production checkpoint telemetry + learned Style preferences (internal library).

OpenMontage is a manual-first editor — there is no autonomous production worker.
This package remains only as internal infrastructure: an append-only checkpoint
event log per project (``schema``/``store``) and the visible, auditable Style
learning store (``learning``/``evidence``) that powers the Studio Style panel.
Nothing here connects to, starts, or drives an external agent.

Layout (single writer per file — no duplicate writers):

    projects/<id>/brain/
      run_events.jsonl   # append-only, monotonically-sequenced event history
      state.json         # materialized view of the event log (rebuildable)
      learned_style.json # project-scope learned style preferences

Guarantees:
  * No secrets ever enter telemetry (see schema.redact_event).
  * Style is learned ONLY from explicit user approvals/corrections.
"""

from __future__ import annotations

from lib.production_brain.schema import (  # noqa: F401
    EVENT_TYPES,
    RUN_STATES,
    STAGES,
    STAGE_STATUSES,
    STAGE_TITLES,
    TERMINAL_RUN_STATES,
    default_stages,
    redact_event,
)
from lib.production_brain.store import (  # noqa: F401
    BrainStoreError,
    ProductionBrainStore,
)

__all__ = [
    "STAGES",
    "STAGE_TITLES",
    "STAGE_STATUSES",
    "RUN_STATES",
    "TERMINAL_RUN_STATES",
    "EVENT_TYPES",
    "default_stages",
    "redact_event",
    "ProductionBrainStore",
    "BrainStoreError",
]
