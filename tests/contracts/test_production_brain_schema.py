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
from lib.production_brain.adapter import FakeBrain
from lib.production_brain.store import ProductionBrainStore

_STATE_SCHEMA = json.loads((REPO_ROOT / "schemas/artifacts/production_run_state.schema.json").read_text())
_EVENT_SCHEMA = json.loads((REPO_ROOT / "schemas/artifacts/production_event.schema.json").read_text())


def _clock():
    t = {"n": 0}

    def now():
        t["n"] += 1
        return f"2026-07-12T00:{t['n'] // 60:02d}:{t['n'] % 60:02d}+00:00"

    return now


def test_schemas_are_valid_jsonschema():
    jsonschema.Draft202012Validator.check_schema(_STATE_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(_EVENT_SCHEMA)


def test_every_event_and_final_state_validate(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    store = ProductionBrainStore(d, now=_clock(), gen_id=lambda: "run_c")
    FakeBrain().drive(store, requested_duration_seconds=300, run_id="run_c")

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
