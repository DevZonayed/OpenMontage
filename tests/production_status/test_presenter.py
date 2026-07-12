"""Canonical production-status presenter — reconciliation invariants (TDD)."""

from __future__ import annotations

import pytest

from lib.production_brain import schema as S
from lib.production_status import (
    CANONICAL_STAGE_COUNT,
    CANONICAL_STAGES,
    build_status_view,
    canonical_stage,
    canonical_stage_index,
)

CONN_OK = {"status": "connected", "available": True, "headline": "Hermes connected"}
CONN_OFF = {"status": "needs_setup", "available": False,
            "headline": "Hermes isn't connected on this machine yet."}


# --------------------------------------------------------------------------- #
# Stage vocabulary + legacy mapping
# --------------------------------------------------------------------------- #
def test_canonical_vocabulary_is_eleven_stages():
    assert CANONICAL_STAGE_COUNT == 11
    assert CANONICAL_STAGES[0] == "research"
    assert CANONICAL_STAGES[-1] == "complete"


@pytest.mark.parametrize("legacy,canon", [
    ("idea", "proposal"),
    ("compose", "render"),
    ("publish", "approval"),
    ("storyboard", "scene_plan"),
    ("qa", "review"),
    ("research", "research"),
    ("assets", "assets"),
])
def test_legacy_stage_mapping(legacy, canon):
    assert canonical_stage(legacy) == canon


def test_unknown_stage_maps_to_none():
    assert canonical_stage("banana") is None
    assert canonical_stage(None) is None
    assert canonical_stage_index("compose") == CANONICAL_STAGES.index("render")


# --------------------------------------------------------------------------- #
# Not started / idle
# --------------------------------------------------------------------------- #
def test_not_started_offers_start_when_connected():
    v = build_status_view(brain=None, board={"project_id": "p"}, run={"state": "not_started"},
                          connection=CONN_OK)
    assert v["overall_state"] == "not_started"
    assert v["primary_action"]["id"] == "start"
    assert v["primary_action"]["owner"] == "hermes"
    assert v["stop_available"] is False
    assert v["mode"] == "idle"


def test_not_started_offers_connect_when_disconnected():
    v = build_status_view(board={"project_id": "p"}, run={"state": "not_started"},
                          connection=CONN_OFF)
    assert v["primary_action"]["id"] == "connect_hermes"
    assert v["primary_action"]["owner"] == "user"


# --------------------------------------------------------------------------- #
# The screenshot state — plan approved, no live run
# --------------------------------------------------------------------------- #
def _approved_board():
    return {
        "project_id": "the-electricity-bulb",
        "has_pipeline_state": True,
        "stages": [
            {"name": "research", "status": "completed"},
            {"name": "proposal", "status": "completed"},
            {"name": "script", "status": "pending"},
            {"name": "scene_plan", "status": "pending"},
            {"name": "assets", "status": "pending"},
        ],
        "events": [],
    }


def test_plan_approved_awaiting_hermes_has_one_primary_continue():
    v = build_status_view(
        board=_approved_board(),
        run={"state": "waiting_for_approval", "plan_approved": True},
        connection=CONN_OK)
    assert v["overall_state"] == "ready_to_produce"
    assert v["owner"] == "hermes"
    # exactly one primary action, and it is Continue with Hermes
    assert v["primary_action"]["id"] == "continue_hermes"
    assert v["primary_action"]["label"] == "Continue production with Hermes"
    # preview is secondary and does NOT advance production
    prev = [a for a in v["secondary_actions"] if a["id"] == "preview"]
    assert prev and prev[0]["advances_production"] is False
    # Stop is NOT offered on a merely-approved run
    assert v["stop_available"] is False
    # canonical stage vocab: the approved plan covers research→scene_plan, so the
    # next PRODUCTION stage is asset generation (Stage 5), not an intermediate
    # planning checkpoint.
    assert v["stage_count"] == 11
    assert v["current_stage"] == "assets"
    assert v["stage_number"] == 5
    assert v["why_waiting"]
    assert "raw" not in v["headline"].lower()


def test_plan_approved_but_disconnected_routes_to_connect():
    v = build_status_view(
        board=_approved_board(),
        run={"state": "waiting_for_approval", "plan_approved": True},
        connection=CONN_OFF)
    assert v["overall_state"] == "ready_to_produce"
    assert v["primary_action"]["id"] == "connect_hermes"
    assert v["owner"] == "user"


def test_awaiting_plan_approval_owner_user():
    v = build_status_view(
        board=_approved_board(),
        run={"state": "waiting_for_approval", "plan_approved": False},
        connection=CONN_OK)
    assert v["overall_state"] == "awaiting_plan_approval"
    assert v["owner"] == "user"
    assert v["primary_action"]["id"] == "approve_plan"
    assert v["stop_available"] is False


# --------------------------------------------------------------------------- #
# Live brain run
# --------------------------------------------------------------------------- #
def _brain_running(stage="assets", orchestration="external_job"):
    st = S.empty_state("p")
    st["run_id"] = "run-1"
    st["state"] = "running"
    st["current_stage"] = stage
    st["brain"] = {"agent_id": "agent-x", "job_id": "job-9", "session_id": "sess-3",
                   "engine": "hermes", "orchestration": orchestration}
    for s in st["stages"]:
        if s["id"] == stage:
            s["status"] = "active"
            s["tool"] = "image_selector"
            s["provider"] = "flux"
            s["elapsed_seconds"] = 12.0
    idx = CANONICAL_STAGES.index(stage)
    for s in st["stages"]:
        if CANONICAL_STAGES.index(s["id"]) < idx:
            s["status"] = "done"
    st["activity"] = "Generating scene 2 of 5"
    return st


def test_live_run_is_live_mode_with_real_handles():
    v = build_status_view(brain=_brain_running(), connection=CONN_OK)
    assert v["mode"] == "live"
    assert v["is_live"] is True
    assert v["overall_state"] == "producing"
    assert v["owner"] == "hermes"
    assert v["identity"]["job"] == "job-9"
    assert v["identity"]["tool"] == "image_selector"
    assert v["identity"]["provider"] == "flux"
    assert v["stop_available"] is True  # a real active run → Stop offered
    assert v["current_stage"] == "assets"
    assert v["stage_number"] == CANONICAL_STAGES.index("assets") + 1
    assert v["elapsed_seconds"] == 12.0


def test_fake_driver_run_is_fixture_not_live():
    v = build_status_view(brain=_brain_running(orchestration="fake_driver"), connection=CONN_OK)
    assert v["mode"] == "fixture"
    assert v["is_live"] is False
    assert v["is_fixture"] is True


def test_demo_flag_forces_demo_mode_only_when_requested():
    v = build_status_view(brain=_brain_running(), connection=CONN_OK, demo=True)
    assert v["mode"] == "demo"
    assert v["is_demo"] is True
    # without the flag it is never demo
    v2 = build_status_view(brain=_brain_running(), connection=CONN_OK)
    assert v2["mode"] != "demo"


# --------------------------------------------------------------------------- #
# Approval gate / blockers
# --------------------------------------------------------------------------- #
def test_brain_awaiting_approval_one_primary_approve():
    st = _brain_running(stage="proposal")
    st["state"] = "awaiting_approval"
    st["approvals"] = [{"approval_id": "a1", "stage": "proposal", "status": "pending",
                        "prompt": "Approve the concept?"}]
    v = build_status_view(brain=st, connection=CONN_OK)
    assert v["overall_state"] == "awaiting_approval"
    assert v["owner"] == "user"
    assert v["primary_action"]["id"] == "approve"
    assert v["primary_action"]["approval_id"] == "a1"
    assert v["active_task"] == "Approve the concept?"


def test_blocked_brain_unavailable_routes_to_connect():
    st = _brain_running(stage="assets")
    st["state"] = "blocked"
    st["blockers"] = [{"blocker_id": "b1", "stage": "assets", "kind": "brain_unavailable",
                       "message": "Hermes went away.", "resolved": False}]
    v = build_status_view(brain=st, connection=CONN_OFF)
    assert v["overall_state"] == "blocked"
    assert v["primary_action"]["id"] == "connect_hermes"
    assert v["stop_available"] is True


def test_coarse_cancelling_is_not_mislabeled_planning():
    # run.json "cancelling" is in _RUN_ACTIVE; it must resolve to cancelling, not
    # the "Hermes is preparing your production" planning copy.
    v = build_status_view(board={"project_id": "p", "stages": [], "events": []},
                          run={"state": "cancelling", "run_id": "r1"}, connection=CONN_OK)
    assert v["overall_state"] == "cancelling"
    assert "preparing" not in (v["active_task"] or "").lower()


def test_cancellation_pending_is_non_terminal_cancelling():
    st = _brain_running()
    st["state"] = "cancelling"
    v = build_status_view(brain=st, connection=CONN_OK)
    assert v["overall_state"] == "cancelling"
    assert v["stop_available"] is True
    assert v["primary_action"]["advances_production"] is False


# --------------------------------------------------------------------------- #
# Conflicting sources → reconciliation, never simultaneous badges
# --------------------------------------------------------------------------- #
def test_conflicting_sources_produce_reconciliation_state():
    st = _brain_running()
    st["state"] = "completed"
    st["terminal"] = True
    st["current_stage"] = None
    v = build_status_view(brain=st, run={"state": "running"}, connection=CONN_OK)
    assert v["overall_state"] == "reconciling"
    assert any(d["kind"] == "source_conflict" for d in v["diagnostics"])


def test_single_primary_action_invariant_across_states():
    scenarios = [
        (None, {"state": "not_started"}, CONN_OK),
        (_approved_board(), {"state": "waiting_for_approval", "plan_approved": True}, CONN_OK),
        (_approved_board(), {"state": "waiting_for_approval", "plan_approved": False}, CONN_OK),
    ]
    for board, run, conn in scenarios:
        v = build_status_view(board=board or {"project_id": "p"}, run=run, connection=conn)
        assert isinstance(v["primary_action"], dict)
        assert v["primary_action"].get("id")


# --------------------------------------------------------------------------- #
# Render gating
# --------------------------------------------------------------------------- #
def test_render_not_renderable_without_layers():
    v = build_status_view(board=_approved_board(),
                          run={"state": "waiting_for_approval", "plan_approved": True},
                          timeline={"layer_count": 0}, connection=CONN_OK)
    assert v["render"]["renderable"] is False
    assert v["render"]["reason"]
    assert v["render"]["active"] is False


def test_render_renderable_with_layers():
    v = build_status_view(brain=_brain_running(stage="render"),
                          timeline={"layer_count": 4}, connection=CONN_OK)
    assert v["render"]["renderable"] is True
    assert v["render"]["reason"] is None


def test_render_active_only_when_render_stage_active():
    st = _brain_running(stage="render")
    v = build_status_view(brain=st, timeline={"layer_count": 3}, connection=CONN_OK)
    assert v["render"]["active"] is True


# --------------------------------------------------------------------------- #
# Stale network → keep last-known, flag reconnecting (never fake)
# --------------------------------------------------------------------------- #
def test_stale_flag_adds_diagnostic_and_preserves_state():
    v = build_status_view(brain=_brain_running(), connection=CONN_OK, stale=True)
    assert v["stale"] is True
    assert any(d["kind"] == "stale" for d in v["diagnostics"])
    assert v["overall_state"] == "producing"  # last known live state preserved


# --------------------------------------------------------------------------- #
# Stepper: 11 canonical stages, one current
# --------------------------------------------------------------------------- #
def test_stepper_has_eleven_stages_and_one_current():
    v = build_status_view(brain=_brain_running(stage="assets"), connection=CONN_OK)
    assert len(v["stages"]) == 11
    currents = [s for s in v["stages"] if s["status"] == "current"]
    assert len(currents) == 1
    assert currents[0]["id"] == "assets"
    completed = [s for s in v["stages"] if s["status"] == "completed"]
    assert {"research", "proposal", "script", "scene_plan"} <= {s["id"] for s in completed}


def test_stepper_from_checkpoints_maps_legacy_names():
    board = {
        "project_id": "p",
        "stages": [
            {"name": "research", "status": "completed"},
            {"name": "idea", "status": "completed"},       # legacy → proposal
            {"name": "compose", "status": "in_progress"},  # legacy → render
        ],
        "events": [],
    }
    v = build_status_view(board=board, run={"state": "not_started"}, connection=CONN_OK)
    ids = {s["id"]: s["status"] for s in v["stages"]}
    assert ids["proposal"] == "completed"
    assert ids["render"] == "current"


def test_checkpoint_awaiting_gate_is_not_a_broken_plan_approval():
    # A mid-pipeline checkpoint awaiting_human gate (no coarse run to POST-approve)
    # must NOT emit a clickable approve_plan (which would POST run_id=null → 400).
    board = {
        "project_id": "p",
        "stages": [
            {"name": "research", "status": "completed"},
            {"name": "proposal", "status": "completed"},
            {"name": "script", "status": "completed"},
            {"name": "scene_plan", "status": "completed"},
            {"name": "assets", "status": "completed"},
            {"name": "edit", "status": "completed"},
            {"name": "review", "status": "awaiting_human"},
        ],
        "events": [],
    }
    v = build_status_view(board=board, run={"state": "not_started"}, connection=CONN_OK)
    assert v["overall_state"] == "awaiting_plan_approval"
    assert v["current_stage"] == "review"
    # passive, no broken POST
    assert v["primary_action"]["id"] == "review_in_chat"
    assert v["primary_action"]["advances_production"] is False
    assert v["owner"] == "user"


def test_coarse_plan_gate_still_offers_approve_plan():
    v = build_status_view(
        board=_approved_board(),
        run={"state": "waiting_for_approval", "plan_approved": False},
        connection=CONN_OK)
    assert v["overall_state"] == "awaiting_plan_approval"
    assert v["primary_action"]["id"] == "approve_plan"


def test_target_duration_uses_requested_not_composer_default():
    # The screenshot fixture: run requested 150s, no timeline → target 2:30 / 4500,
    # never the invented 60s (1:00 / 1800) composer default.
    v = build_status_view(
        board=_approved_board(),
        run={"state": "waiting_for_approval", "plan_approved": True,
             "requested_duration_seconds": 150},
        timeline={"layers": []}, connection=CONN_OK)
    t = v["target"]
    assert t["available"] is True
    assert t["formatted"] == "2:30"
    assert t["frames"] == 4500
    assert t["is_target"] is True
    assert "4500 target frames" in t["label"]


def test_target_duration_pending_when_unknown():
    v = build_status_view(board={"project_id": "p"}, run={"state": "not_started"},
                          connection=CONN_OK)
    assert v["target"]["available"] is False
    assert v["target"]["label"] == "duration pending"


def test_target_prefers_real_timeline_when_layers_exist():
    v = build_status_view(brain=_brain_running(stage="edit"),
                          run={"requested_duration_seconds": 150},
                          timeline={"layers": [{"id": "a"}], "target_duration_seconds": 90},
                          connection=CONN_OK)
    assert v["target"]["formatted"] == "1:30"
    assert v["target"]["is_target"] is False  # authoritative, not a target


def test_never_raises_on_empty_inputs():
    v = build_status_view()
    assert v["overall_state"] == "not_started"
    assert len(v["stages"]) == 11
