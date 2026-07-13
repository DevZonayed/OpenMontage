"""Read-only project OVERVIEW presenter — reconciliation invariants.

OpenMontage is manual-first: there is NO autonomous production worker. The
presenter folds on-disk artifacts (checkpoint milestones + timeline + outputs)
into a single read-only overview. These tests pin that contract:

  * NO ``connection`` block, NO ``overall_state``, NO agent/identity fields.
  * Three plain-language headline states driven only by timeline + milestones.
  * Truthful target duration (pending vs requested), never a composer minimum.
  * ``render.renderable`` follows layer count; ``render.active`` a real render.
  * Milestones come from the checkpoint rail as INFORMATIONAL history, with
    legacy stage names canonically mapped.
  * ``owner`` is always "you"; the single action is ``open_studio``.
"""

from __future__ import annotations

import pytest

from lib.production_status import (
    CANONICAL_STAGE_COUNT,
    CANONICAL_STAGES,
    build_status_view,
    canonical_stage,
    canonical_stage_index,
)

# Keys the manual-first overview must NEVER carry (agent/automation leftovers).
_FORBIDDEN_KEYS = ("connection", "overall_state", "identity", "is_live",
                   "stop_available", "why_waiting", "active_task")


# --------------------------------------------------------------------------- #
# Stage vocabulary + legacy mapping (informational labels only)
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
# Overview shape — no agent/automation surface
# --------------------------------------------------------------------------- #
def test_overview_shape_is_manual_first():
    v = build_status_view(project={"id": "p", "title": "P"})
    assert v["version"] == "2.0"
    assert v["kind"] == "project_overview"
    assert v["project_id"] == "p"
    assert v["title"] == "P"
    assert v["owner"] == "you"
    assert v["primary_action"]["id"] == "open_studio"
    for key in _FORBIDDEN_KEYS:
        assert key not in v


def test_never_raises_on_empty_inputs():
    v = build_status_view()
    assert v["kind"] == "project_overview"
    assert v["owner"] == "you"
    assert v["headline"] == "Set up your first scene"
    assert v["has_timeline"] is False
    assert v["layer_count"] == 0


# --------------------------------------------------------------------------- #
# The three headline states
# --------------------------------------------------------------------------- #
def test_headline_no_timeline_no_milestones_is_first_scene():
    v = build_status_view(project={"id": "p"})
    assert v["headline"] == "Set up your first scene"
    assert v["has_timeline"] is False
    assert v["milestones"] == []


def test_headline_ready_to_edit_when_milestones_but_no_layers():
    board = {"project_id": "p", "stages": [
        {"name": "research", "status": "completed"},
        {"name": "proposal", "status": "completed"},
    ]}
    v = build_status_view(board=board)
    assert v["headline"] == "Ready to edit"
    assert v["has_timeline"] is False
    assert v["milestones"]  # informational planning notes exist


def test_headline_counts_scenes_when_layers_exist():
    v1 = build_status_view(timeline={"layer_count": 1})
    assert v1["headline"] == "1 scene on the timeline"
    v3 = build_status_view(timeline={"layers": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    assert v3["headline"] == "3 scenes on the timeline"
    assert v3["has_timeline"] is True
    assert v3["layer_count"] == 3


# --------------------------------------------------------------------------- #
# Target duration — truthful (pending vs requested vs real timeline)
# --------------------------------------------------------------------------- #
def test_target_pending_when_no_timeline_and_no_request():
    v = build_status_view(project={"id": "p"})
    t = v["target"]
    assert t["available"] is False
    assert t["label"] == "Duration set after first scene"
    assert t["frames"] is None


def test_target_uses_requested_not_composer_default():
    # requested 150s, no layered timeline → target 2:30 / 4500, never a 60s min.
    v = build_status_view(project={"id": "p"}, requested_duration_seconds=150,
                          timeline={"layers": []})
    t = v["target"]
    assert t["available"] is True
    assert t["is_target"] is True
    assert t["formatted"] == "2:30"
    assert t["frames"] == 4500
    assert t["fps"] == 30
    assert "4500 target frames" in t["label"]


def test_target_prefers_real_timeline_when_layers_exist():
    v = build_status_view(
        requested_duration_seconds=150,
        timeline={"layers": [{"id": "a"}], "target_duration_seconds": 90})
    t = v["target"]
    assert t["formatted"] == "1:30"
    assert t["frames"] == 2700
    assert t["is_target"] is False  # authoritative timeline, not a target
    assert t["source"] == "timeline"


# --------------------------------------------------------------------------- #
# Render gating — renderable follows layers; active only for a real render
# --------------------------------------------------------------------------- #
def test_render_not_renderable_without_layers():
    v = build_status_view(project={"id": "p"})
    assert v["render"]["renderable"] is False
    assert v["render"]["reason"]
    assert v["render"]["active"] is False
    assert v["render"]["layer_count"] == 0


def test_render_renderable_with_layers():
    v = build_status_view(timeline={"layer_count": 4})
    assert v["render"]["renderable"] is True
    assert v["render"]["reason"] is None
    assert v["render"]["layer_count"] == 4


def test_render_active_only_when_actually_rendering():
    idle = build_status_view(timeline={"layer_count": 3})
    assert idle["render"]["active"] is False
    from_timeline = build_status_view(timeline={"layer_count": 3, "rendering": True})
    assert from_timeline["render"]["active"] is True
    from_outputs = build_status_view(timeline={"layer_count": 3},
                                     outputs={"rendering": True})
    assert from_outputs["render"]["active"] is True


# --------------------------------------------------------------------------- #
# Milestones — informational history, canonically mapped, never live progress
# --------------------------------------------------------------------------- #
def test_milestones_are_informational_and_canonical_mapped():
    board = {"project_id": "p", "stages": [
        {"name": "research", "status": "completed"},
        {"name": "idea", "status": "completed"},        # legacy → proposal
        {"name": "compose", "status": "in_progress"},   # legacy → render
        {"name": "script", "status": "pending"},        # pending → dropped
    ]}
    v = build_status_view(board=board)
    ids = {m["id"]: m["status"] for m in v["milestones"]}
    assert ids["research"] == "done"
    assert ids["proposal"] == "done"           # idea mapped to proposal
    assert ids["render"] == "in_progress"      # compose mapped to render
    assert "script" not in ids                 # pending stages are not milestones
    # canonical ordering preserved
    order = [m["id"] for m in v["milestones"]]
    assert order == sorted(order, key=lambda s: CANONICAL_STAGES.index(s))
    # labels are human-readable titles
    assert all(m["label"] for m in v["milestones"])


def test_milestone_status_mapping_covers_review_and_failure():
    board = {"project_id": "p", "stages": [
        {"name": "review", "status": "awaiting_human"},
        {"name": "assets", "status": "failed"},
    ]}
    v = build_status_view(board=board)
    ids = {m["id"]: m["status"] for m in v["milestones"]}
    assert ids["review"] == "needs_review"
    assert ids["assets"] == "failed"


def test_milestone_progress_counts_done():
    board = {"project_id": "p", "stages": [
        {"name": "research", "status": "completed"},
        {"name": "proposal", "status": "completed"},
        {"name": "script", "status": "in_progress"},
    ]}
    v = build_status_view(board=board)
    assert v["milestone_progress"] == {"completed": 2, "total": 3}


# --------------------------------------------------------------------------- #
# Blockers, last-saved, outputs
# --------------------------------------------------------------------------- #
def test_failed_stage_becomes_a_blocker():
    board = {"project_id": "p", "stages": [
        {"name": "assets", "status": "failed", "message": "Provider quota exhausted."},
    ]}
    v = build_status_view(board=board)
    assert v["blockers"] == [{"message": "Provider quota exhausted.", "stage": "assets"}]


def test_last_saved_prefers_latest_event():
    board = {"project_id": "p", "events": [
        {"message": "Scene added", "ts": "2026-07-13T10:00:00Z"},
        {"message": "Timeline saved", "ts": "2026-07-13T10:05:00Z"},
    ]}
    v = build_status_view(board=board)
    assert v["last_saved"]["label"] == "Timeline saved"
    assert v["last_saved"]["ts"] == "2026-07-13T10:05:00Z"


def test_outputs_summarize_renders_and_assets():
    outputs = {
        "renders": [{"path": "renders/a.mp4"}, {"path": "renders/final.mp4"}],
        "asset_count": 7,
    }
    v = build_status_view(timeline={"layer_count": 2}, outputs=outputs)
    o = v["outputs"]
    assert o["render_count"] == 2
    assert o["latest_render"] == {"path": "renders/final.mp4"}
    assert o["asset_count"] == 7


# --------------------------------------------------------------------------- #
# Mode flags — demo / fixture / stale
# --------------------------------------------------------------------------- #
def test_mode_defaults_to_local():
    v = build_status_view(project={"id": "p"})
    assert v["mode"] == "local"
    assert v["is_demo"] is False
    assert v["is_fixture"] is False
    assert v["stale"] is False
    assert v["diagnostics"] == []


def test_demo_flag_forces_demo_mode_only_when_requested():
    assert build_status_view(project={"id": "p"}, demo=True)["mode"] == "demo"
    assert build_status_view(project={"id": "p"})["mode"] != "demo"


def test_fixture_mode_from_outputs():
    v = build_status_view(project={"id": "p"}, outputs={"fixture": True})
    assert v["mode"] == "fixture"
    assert v["is_fixture"] is True


def test_stale_flag_adds_diagnostic_and_preserves_view():
    v = build_status_view(timeline={"layer_count": 2}, stale=True)
    assert v["stale"] is True
    assert any(d["kind"] == "stale" for d in v["diagnostics"])
    assert v["headline"] == "2 scenes on the timeline"  # state preserved
