"""Hermes production brain — canonical, observable production-run telemetry.

This package turns the Hermes agent into the *persistent, transparent video-
production brain* rather than a hidden subprocess. It owns a versioned,
append-only run/event history per project so an observer can always answer:
which agent/job/tool/provider is doing which task, at which stage, with what
progress, elapsed time, latest event, outputs, approvals, blockers, and errors.

Layout (single writer per file — no duplicate writers):

    projects/<id>/brain/
      run_events.jsonl   # append-only, monotonically-sequenced event history
                         #   (AUTHORITATIVE — the state doc is a view of this)
      state.json         # materialized production_run_state (rebuildable)
      learned_style.json # project-scope learned style preferences

Honesty guarantees:
  * The brain fails CLOSED when Hermes is unavailable (see adapter.py).
  * No secrets ever enter telemetry (see schema.redact_event).
  * Terminal states are truthful; a crashed run reconciles from the log.
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
