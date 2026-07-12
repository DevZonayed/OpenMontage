"""Real, bounded, FREE local preflight & planning worker.

This is the worker ``lib.production_run`` spawns for ``START PRODUCTION``. It does
genuine, zero-cost operational work — it does NOT generate paid media and does
NOT make creative/provider decisions (that stays agent-driven per Rule Zero):

  1. validate the saved intake (pipeline + canonical ``target_duration_seconds``),
  2. run the mandatory ``provider_menu_summary()`` preflight (free capability read),
  3. build the frame-accurate canonical ``timeline.json`` from the target duration,
  4. write a free ``run_plan.json`` (frame/word budget, provider readiness, runtimes),
  5. transition to ``waiting_for_approval`` at the HONEST provider/agent boundary,
     then heartbeat there (cancellable) until cancelled or a bounded lifetime.

It never claims generation/render is happening. Cancellation is cooperative
(reads run.json between steps + a SIGTERM handler) so ``cancel_run`` stops it
promptly and cleanly.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from lib import duration as _dur
from lib import production_run as _run
from lib import timeline as _tl
from lib.paths import PROJECTS_DIR
from lib.pipeline_loader import list_pipelines
from lib.project_intake import read_intake

RUN_PLAN_FILENAME = "run_plan.json"
_MAX_LIFETIME_SECONDS = 1200.0  # 20 min: bounded wait-for-approval heartbeat

_CANCEL_REQUESTED = False


class _Cancelled(Exception):
    """Raised the moment a cancel is observed, so the worker bails immediately and
    never clobbers the controller's cancelling/cancelled state."""


def _install_sigterm():
    def _handler(signum, frame):
        global _CANCEL_REQUESTED
        _CANCEL_REQUESTED = True
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


def _cancel_pending(project_dir: Path) -> bool:
    if _CANCEL_REQUESTED:
        return True
    run = _run.read_run(project_dir) or {}
    return run.get("state") in ("cancelling", "cancelled")


_MEDIA_CAPS = ("image_generation", "video_generation", "tts", "music_generation",
               "music_search", "avatar", "analysis", "enhancement")


def _merge_update(project_dir: Path, now: Callable[[], str], *, log: Optional[str] = None,
                  **fields) -> None:
    run = _run.read_run(project_dir) or {}
    # NEVER overwrite an in-flight cancel — bail instead.
    if _CANCEL_REQUESTED or run.get("state") in ("cancelling", "cancelled"):
        raise _Cancelled()
    ts = now()
    run.update(fields)
    if log:
        entries = run.get("log") or []
        entries.append({"ts": ts, "phase": fields.get("phase") or run.get("phase"),
                        "message": log})
        run["log"] = entries[-80:]  # bounded activity log
    run["updated_at"] = ts
    _run._write_run(project_dir, run)


def _real_provider_summary() -> dict:
    from tools.tool_registry import registry
    registry.discover()
    return registry.provider_menu_summary()


def _provider_readiness(summary: dict) -> dict:
    caps = summary.get("capabilities", []) or []
    configured = sum(1 for c in caps if (c.get("configured") or 0) > 0)
    total = len(caps)
    runtimes = summary.get("composition_runtimes", {}) or {}
    by = {c.get("capability"): c for c in caps}
    # Per-capability media breakdown so the UI can show exactly what the run CAN
    # and CANNOT do (which model providers are configured vs. need a key).
    media = []
    for name in _MEDIA_CAPS:
        c = by.get(name)
        if not c:
            continue
        media.append({
            "capability": name,
            "configured": int(c.get("configured") or 0),
            "total": int(c.get("total") or 0),
            "available_providers": list(c.get("available_providers") or [])[:8],
        })
    return {
        "capabilities_configured": configured,
        "capabilities_total": total,
        "composition_runtimes": {k: bool(v) for k, v in runtimes.items()},
        "media_capabilities": media,
    }


def run_worker(project_id: str, *, project_dir: Optional[Path] = None,
               now: Callable[[], str] = _run._iso_now,
               provider_summary: Optional[Callable[[], dict]] = None,
               heartbeat: bool = True, max_lifetime: float = _MAX_LIFETIME_SECONDS,
               sleep: Callable[[float], None] = time.sleep) -> str:
    """Execute the preflight/planning worker. Returns the terminal state string."""
    project_dir = Path(project_dir) if project_dir is not None else (PROJECTS_DIR / project_id)
    if _cancel_pending(project_dir):
        return "cancelled"
    try:
        return _run_worker_body(project_dir, now=now, provider_summary=provider_summary,
                                heartbeat=heartbeat, max_lifetime=max_lifetime, sleep=sleep)
    except _Cancelled:
        return "cancelled"


def _run_worker_body(project_dir, *, now, provider_summary, heartbeat, max_lifetime, sleep) -> str:
    # --- 1. validate intake / pipeline / duration ---
    _merge_update(project_dir, now, state="running", phase="preflight",
                  activity="Validating the saved brief, pipeline and duration…", error=None,
                  log="Preflight started — validating intake, pipeline and duration.")
    intake = read_intake(project_dir) or {}
    pipeline = intake.get("pipeline_type")
    if pipeline not in set(list_pipelines()):
        _merge_update(project_dir, now, state="failed", phase="preflight",
                      error="The project's pipeline is not recognized.",
                      activity="Preflight failed: unknown pipeline.", ended_at=now())
        return "failed"
    try:
        secs = _dur.validate_target_seconds(_dur.infer_target_seconds(intake))
    except _dur.DurationError:
        _merge_update(project_dir, now, state="failed", phase="preflight",
                      error="The project's target duration is invalid.",
                      activity="Preflight failed: invalid duration.", ended_at=now())
        return "failed"
    if _cancel_pending(project_dir):
        return "cancelled"

    _merge_update(project_dir, now, phase="preflight",
                  log=f"Intake OK — pipeline '{pipeline}', target {_dur.format_mmss(secs)} "
                      f"({_dur.frames_for(secs)} frames @{_dur.DEFAULT_FPS}fps).")

    # --- 2. provider capability preflight (free) ---
    _merge_update(project_dir, now, phase="preflight",
                  activity="Reading the provider capability menu (provider_menu_summary)…",
                  log="Running provider_menu_summary() — reading configured tools/models.")
    try:
        summary = (provider_summary or _real_provider_summary)()
    except Exception:
        summary = {}
    readiness = _provider_readiness(summary)
    for m in readiness.get("media_capabilities", []):
        prov = ", ".join(m["available_providers"]) if m["available_providers"] else "none configured"
        _merge_update(project_dir, now, log=f"{m['capability']}: {m['configured']}/{m['total']} "
                                            f"configured ({prov}).")
    if _cancel_pending(project_dir):
        return "cancelled"

    # --- 3. build the frame-accurate canonical timeline ---
    fmt = _dur.format_mmss(secs)
    _merge_update(project_dir, now, phase="planning",
                  activity=f"Planning a frame-accurate {fmt} timeline "
                           f"({_dur.frames_for(secs)} frames, ≈{_dur.word_budget(secs)} words)…",
                  log=f"Planning timeline: {_dur.frames_for(secs)} frames, "
                      f"≈{_dur.word_budget(secs)}-word narration budget.")
    tl = _tl.build_timeline(intake)
    existing, tag = _tl.read_timeline(project_dir)
    try:
        _tl.save_timeline(project_dir, tl, if_match=tag if existing is not None else None)
    except _tl.TimelineError:
        pass  # a hand-edited timeline already exists — keep it, don't clobber
    if _cancel_pending(project_dir):
        return "cancelled"

    # --- 4. free run_plan artifact ---
    plan = {
        "version": "1.0",
        "target_duration_seconds": secs,
        "target_formatted": fmt,
        "fps": tl["fps"],
        "total_frames": tl["total_frames"],
        "word_budget": _dur.word_budget(secs),
        "provider_readiness": readiness,
        "next_boundary": "provider_and_proposal_approval",
    }
    (project_dir / RUN_PLAN_FILENAME).write_text(json.dumps(plan, indent=2), encoding="utf-8")

    # --- 5. honest waiting-for-approval boundary ---
    cfg, tot = readiness["capabilities_configured"], readiness["capabilities_total"]
    boundary = (
        f"Preflight complete: {cfg} of {tot} capability groups are configured; "
        f"a {fmt} timeline ({tl['total_frames']} frames) was planned. "
        "Next step is agent-driven: approve providers + the proposal to generate assets. "
        "No paid generation has run."
    )
    _merge_update(project_dir, now, state="waiting_for_approval", phase="proposal_gate",
                  activity=boundary, next_boundary="provider_and_proposal_approval",
                  log="Preflight & planning complete — waiting for your approval. No paid "
                      "generation ran; asset generation is agent-driven from here.")

    # --- 6. bounded, cancellable heartbeat while waiting ---
    # Cancel is checked every second (read-only), but we only WRITE a liveness
    # timestamp occasionally so a durable waiting state doesn't spam run.json
    # (which would otherwise churn the UI).
    if heartbeat:
        elapsed = 0.0
        while elapsed < max_lifetime:
            if _cancel_pending(project_dir):
                return "cancelled"
            sleep(1.0)
            elapsed += 1.0
            if _cancel_pending(project_dir):
                return "cancelled"
            run = _run.read_run(project_dir) or {}
            if run.get("state") != "waiting_for_approval":
                return run.get("state", "waiting_for_approval")
            if int(elapsed) % 20 == 0:  # liveness write ~every 20s, not every 1s
                run["updated_at"] = now()
                _run._write_run(project_dir, run)
    return "waiting_for_approval"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 2
    project_id = argv[0]
    _install_sigterm()
    try:
        run_worker(project_id)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
