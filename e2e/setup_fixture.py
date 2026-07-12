#!/usr/bin/env python3
"""Create the exact production command-center acceptance fixture.

Reproduces the rejected state: ``the-electricity-bulb`` with research + proposal
approved, ``run.json`` waiting_for_approval + plan_approved + requested 150s, and
NO timeline (so the composer would otherwise default to 60s / 1800 frames). Writes
under ``$OPENMONTAGE_PROJECTS_DIR`` (default /tmp/openmontage-acceptance-projects).

Run from the repo root:  python e2e/setup_fixture.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path
from lib.checkpoint import init_project  # noqa: E402

ROOT = Path(os.environ.get("OPENMONTAGE_PROJECTS_DIR") or "/tmp/openmontage-acceptance-projects")
PID = "the-electricity-bulb"


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    proj = ROOT / PID
    init_project(PID, title="The Electricity Bulb", pipeline_type="animation")
    now = datetime.now(timezone.utc).isoformat()
    # intake WITHOUT target_duration_seconds → composer would default to 60s.
    (proj / "intake.json").write_text(json.dumps({
        "version": "1.0", "project_id": PID, "title": "The Electricity Bulb",
        "brief": "A 2:30 animated explainer on how the incandescent light bulb was invented.",
        "pipeline_type": "animation", "created_at": now}))

    def cp(stage: str, status: str, **extra) -> None:
        (proj / f"checkpoint_{stage}.json").write_text(json.dumps({
            "version": "1.0", "project_id": PID, "stage": stage, "status": status,
            "pipeline_type": "animation", "timestamp": now, **extra}))

    cp("research", "completed")
    cp("proposal", "completed", human_approved=True)
    (proj / "run.json").write_text(json.dumps({
        "state": "waiting_for_approval", "run_id": "run_eb_001", "worker_pid": None,
        "worker_kind": "preflight_planner", "project_id": PID,
        "target_duration_seconds": 150, "requested_duration_seconds": 150,
        "phase": "proposal", "activity": "Plan ready — approved, awaiting production.",
        "error": None, "created_at": now, "started_at": now, "updated_at": now,
        "ended_at": None, "plan_approved": True, "approved_at": now}))
    (proj / "run_plan.json").write_text(json.dumps({
        "title": "The Electricity Bulb", "pipeline_type": "animation",
        "target_duration_seconds": 150, "requested_duration_seconds": 150,
        "render_runtime": "remotion"}))
    print(f"fixture ready: {proj}")


if __name__ == "__main__":
    main()
