"""Contract: brain telemetry validates against its published JSON Schemas.

Every event a run emits must validate against production_event.schema.json and
the materialized state against production_run_state.schema.json — this is the
stable contract Worker B (and any external consumer) reads.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from lib.paths import REPO_ROOT
from lib.production_brain import schema as S
from lib.production_brain.store import ProductionBrainStore

_STATE_SCHEMA = json.loads((REPO_ROOT / "schemas/artifacts/production_run_state.schema.json").read_text())
_EVENT_SCHEMA = json.loads((REPO_ROOT / "schemas/artifacts/production_event.schema.json").read_text())


def _clock():
    t = {"n": 0}

    def now():
        t["n"] += 1
        return f"2026-07-12T00:{t['n'] // 60:02d}:{t['n'] % 60:02d}+00:00"

    return now


# The adapter/FakeBrain that used to drive a run is gone. Exercise the SAME
# telemetry surface by driving the store's public API directly: a full
# research→complete walk that emits every telemetry event kind (stage/tool/
# progress/output/approval) plus the lifecycle events, exactly as a real run
# would, so both each event and the final state validate against the schemas.
_PLAN = [
    ("research", "web_research", "local",
     [{"kind": "artifact", "path": "artifacts/research_brief.json", "label": "Research brief"}], False),
    ("proposal", "proposal_writer", "hermes",
     [{"kind": "artifact", "path": "artifacts/proposal_packet.json", "label": "Proposal"}], True),
    ("script", "script_writer", "hermes",
     [{"kind": "artifact", "path": "artifacts/script.json", "label": "Script"}], False),
    ("scene_plan", "scene_planner", "hermes",
     [{"kind": "artifact", "path": "artifacts/scene_plan.json", "label": "Scene plan"}], False),
    ("assets", "image_selector", "stub-image",
     [{"kind": "image", "path": "assets/images/scene_01.png", "label": "Scene 1 still"}], False),
    ("narration", "tts_selector", "stub-tts",
     [{"kind": "audio", "path": "assets/audio/narration.mp3", "label": "Narration"}], False),
    ("edit", "video_compose", "remotion",
     [{"kind": "artifact", "path": "artifacts/edit_decisions.json", "label": "Edit decisions"}], False),
    ("render", "video_compose", "remotion",
     [{"kind": "video", "path": "renders/final.mp4", "label": "Final render"}], False),
    ("review", "reviewer", "hermes",
     [{"kind": "artifact", "path": "artifacts/final_review.json", "label": "Review"}], False),
    ("approval", None, None, [], True),
    ("complete", None, None, [], False),
]


def _drive_full_run(store, *, requested_duration_seconds, run_id):
    """Open a run and walk every stage to completion, auto-approving gates."""
    state = store.start_provisioned(
        provision=lambda rid: (f"sess-{rid}", f"job-{rid}", {"external": False},
                               "Production run started.", None),
        run_id=run_id, requested_duration_seconds=requested_duration_seconds)
    rid = state["run_id"]
    for stage, tool, provider, outputs, approval in _PLAN:
        store.enter_stage(stage, message=f"Working on {S.STAGE_TITLES[stage]}.")
        if tool:
            store.tool_call(stage, tool, provider=provider, job_id=f"job-{stage}",
                            message=f"Calling {tool}")
        store.stage_progress(stage, 0.5, message="Halfway.")
        for out in outputs:
            store.output(stage, kind=out["kind"], path=out["path"], label=out["label"],
                         message=f"Produced {out['label']}.")
        if approval:
            store.request_approval(stage, prompt=f"Approve {S.STAGE_TITLES[stage]}?")
            store.grant_approval(rid, stage=stage, by="user")
        if stage != "complete":
            store.complete_stage(stage, message=f"Completed {S.STAGE_TITLES[stage]}.")
    return store.complete_run(
        rid, actual_duration_seconds=round(float(requested_duration_seconds), 3),
        message="Production complete — deliverable rendered.")


def test_schemas_are_valid_jsonschema():
    jsonschema.Draft202012Validator.check_schema(_STATE_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(_EVENT_SCHEMA)


def test_every_event_and_final_state_validate(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    store = ProductionBrainStore(d, now=_clock(), gen_id=lambda: "run_c")
    _drive_full_run(store, requested_duration_seconds=300, run_id="run_c")

    for ev in store.read_events_raw():
        jsonschema.validate(ev, _EVENT_SCHEMA)

    jsonschema.validate(store.read_state(), _STATE_SCHEMA)


def test_event_types_and_stages_match_schema_enums():
    et = set(_EVENT_SCHEMA["properties"]["type"]["enum"])
    assert set(S.EVENT_TYPES) == et
    state_enum = set(_STATE_SCHEMA["properties"]["state"]["enum"])
    assert set(S.RUN_STATES) == state_enum


@pytest.mark.parametrize("secs", [1, 60, 300])
def test_requested_duration_within_schema_bounds(tmp_path, secs):
    d = tmp_path / f"p{secs}"
    d.mkdir()
    store = ProductionBrainStore(d, now=_clock(), gen_id=lambda: "r")
    store.start(requested_duration_seconds=secs)
    jsonschema.validate(store.read_state(), _STATE_SCHEMA)
