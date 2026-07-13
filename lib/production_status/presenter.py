"""Pure reconciliation of on-disk project state into ONE read-only overview model.

OpenMontage is a **manual-first** video editor. There is no autonomous production
worker in the product: the user builds the timeline in the Studio. This module
folds the durable, on-disk artifacts — checkpoint *milestones*, the timeline
(layers + duration), and rendered *outputs* — into a single plain-language
overview the Board renders. It is PURE (no I/O): callers load the source dicts and
pass them in, so every rule is unit-testable in isolation.

Design guarantees (covered by tests):
  * NO agent/automation concept — no "producing", no active worker inferred, no
    connection block, no start/stop/retry/resume.
  * Milestones (from checkpoints) are shown as INFORMATIONAL history, never as an
    autonomous worker's live progress.
  * ``render.renderable`` is True only when the timeline actually has layers, and
    ``render.active`` only when a real render is genuinely in flight.
  * The target duration is truthful — a pending duration is ``available: False``
    ("Duration set after first scene"), NEVER the composer's internal minimum.
  * The Board's single action is ``open_studio``; there are no production controls.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.production_status.stages import STAGES, STAGE_TITLES

# --------------------------------------------------------------------------- #
# Canonical stage vocabulary (informational milestone labels only)
# --------------------------------------------------------------------------- #
CANONICAL_STAGES: tuple[str, ...] = tuple(STAGES)
CANONICAL_STAGE_COUNT: int = len(CANONICAL_STAGES)
CANONICAL_STAGE_TITLES: dict[str, str] = dict(STAGE_TITLES)
_STAGE_INDEX: dict[str, int] = {name: i for i, name in enumerate(CANONICAL_STAGES)}

# Legacy / pipeline-manifest stage names → canonical stage.
LEGACY_STAGE_MAP: dict[str, str] = {
    "research": "research", "idea": "proposal", "proposal": "proposal",
    "script": "script", "scene_plan": "scene_plan", "storyboard": "scene_plan",
    "assets": "assets", "asset": "assets", "narration": "narration",
    "audio": "narration", "voiceover": "narration", "edit": "edit",
    "editing": "edit", "compose": "render", "composition": "render",
    "render": "render", "review": "review", "qa": "review", "approval": "approval",
    "publish": "approval", "delivery": "approval", "complete": "complete",
    "completed": "complete", "done": "complete",
}

OWNER_YOU = "you"
_TARGET_FPS = 30


def canonical_stage(name: Optional[str]) -> Optional[str]:
    if not name or not isinstance(name, str):
        return None
    key = name.strip().lower()
    if key in _STAGE_INDEX:
        return key
    return LEGACY_STAGE_MAP.get(key)


def canonical_stage_index(name: Optional[str]) -> Optional[int]:
    c = canonical_stage(name)
    return _STAGE_INDEX.get(c) if c else None


def _fmt_stage(name: Optional[str]) -> str:
    c = canonical_stage(name)
    if c:
        return CANONICAL_STAGE_TITLES.get(c, (name or "").replace("_", " ").title())
    return (name or "").replace("_", " ").title()


# --------------------------------------------------------------------------- #
# Timeline + checkpoint helpers
# --------------------------------------------------------------------------- #
def _timeline_layers(timeline: Optional[dict]) -> int:
    if not timeline:
        return 0
    n = timeline.get("layer_count")
    if isinstance(n, int):
        return n
    layers = timeline.get("layers")
    if isinstance(layers, list):
        return len(layers)
    tracks = timeline.get("tracks")
    if isinstance(tracks, list):
        total = 0
        for t in tracks:
            ls = (t or {}).get("layers") if isinstance(t, dict) else None
            if isinstance(ls, list):
                total += len(ls)
        return total
    return 0


def _milestones(board: Optional[dict]) -> list[dict]:
    """Informational milestones from the checkpoint rail — history, not a worker.

    One entry per canonical stage that has a checkpoint, in canonical order, with a
    neutral display status (done | in_progress | needs_review | failed)."""
    statuses: dict[str, str] = {}
    details: dict[str, Any] = {}
    rank = {"pending": 0, "unknown": 0, "in_progress": 2, "awaiting_human": 3,
            "failed": 3, "completed": 4}
    for entry in (board or {}).get("stages") or []:
        if not isinstance(entry, dict):
            continue
        canon = canonical_stage(entry.get("name"))
        if not canon:
            continue
        status = entry.get("status") or "pending"
        if rank.get(status, 1) >= rank.get(statuses.get(canon), 1):
            statuses[canon] = status
            details[canon] = entry.get("updated_at") or entry.get("ts")
    _MAP = {"completed": "done", "in_progress": "in_progress",
            "awaiting_human": "needs_review", "failed": "failed"}
    out: list[dict] = []
    for sid in CANONICAL_STAGES:
        st = statuses.get(sid)
        if st in (None, "pending", "unknown"):
            continue
        out.append({"id": sid, "label": CANONICAL_STAGE_TITLES[sid],
                    "status": _MAP.get(st, "done"), "ts": details.get(sid)})
    return out


def _last_saved(board: Optional[dict], timeline: Optional[dict]) -> Optional[dict]:
    events = (board or {}).get("events") or []
    if events:
        last = events[-1]
        label = last.get("message") or last.get("event") or last.get("type")
        if label:
            return {"label": label, "ts": last.get("ts") or last.get("timestamp")}
    if timeline and timeline.get("updated_at"):
        return {"label": "Timeline saved", "ts": timeline.get("updated_at")}
    return None


def _blockers(board: Optional[dict]) -> list[dict]:
    out: list[dict] = []
    for entry in (board or {}).get("stages") or []:
        if isinstance(entry, dict) and entry.get("status") == "failed":
            msg = entry.get("message") or entry.get("error") or (
                f"{_fmt_stage(entry.get('name'))} needs attention.")
            out.append({"message": msg, "stage": entry.get("name")})
    return out


# --------------------------------------------------------------------------- #
# Target duration (truthful — never the composer's internal minimum)
# --------------------------------------------------------------------------- #
def _fmt_mmss(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    return f"{s // 60}:{s % 60:02d}"


def _target(seconds, fps, source, *, is_target: bool) -> dict:
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        secs = 0.0
    frames = int(round(secs * fps))
    label = (f"target {_fmt_mmss(secs)} · {frames} target frames" if is_target
             else f"{_fmt_mmss(secs)} · {frames} frames")
    return {"available": True, "duration_seconds": round(secs, 3),
            "formatted": _fmt_mmss(secs), "frames": frames, "fps": fps,
            "source": source, "is_target": is_target, "label": label}


def _target_block(*, timeline: Optional[dict], requested_duration_seconds) -> dict:
    """A real timeline wins; else the requested/target duration; else PENDING.

    A pending target is ``available: False`` with the guidance label — we never
    present the composer's internal minimum (e.g. 60s / 1800 frames) as the user's
    chosen duration."""
    fps = _TARGET_FPS
    layers = _timeline_layers(timeline)
    tl = timeline or {}
    if layers > 0 and tl.get("target_duration_seconds"):
        return _target(tl["target_duration_seconds"], fps, "timeline", is_target=False)
    if requested_duration_seconds:
        return _target(requested_duration_seconds, fps, "requested", is_target=True)
    return {"available": False, "duration_seconds": None, "formatted": None,
            "frames": None, "fps": fps, "source": "pending", "is_target": True,
            "label": "Duration set after first scene"}


# --------------------------------------------------------------------------- #
# Render block (renderable only with layers; active only for a real render)
# --------------------------------------------------------------------------- #
def _render_block(*, timeline: Optional[dict], outputs: Optional[dict]) -> dict:
    layers = _timeline_layers(timeline)
    renderable = layers > 0
    active = bool(timeline and timeline.get("rendering")) or bool(
        (outputs or {}).get("rendering"))
    reason = None if renderable else (
        "Add scenes to the timeline in the Studio to enable rendering.")
    return {"renderable": renderable, "active": active, "reason": reason,
            "layer_count": layers}


# --------------------------------------------------------------------------- #
# Main — the read-only project overview
# --------------------------------------------------------------------------- #
def build_status_view(
    *,
    project: Optional[dict] = None,
    board: Optional[dict] = None,
    timeline: Optional[dict] = None,
    requested_duration_seconds: Optional[float] = None,
    outputs: Optional[dict] = None,
    demo: bool = False,
    stale: bool = False,
) -> dict:
    """Fold on-disk artifacts into the manual-first project overview.

    Every input is optional and defensively defaulted, so a brand-new project
    degrades to a clear "ready to edit" view rather than raising."""
    project = project or {}
    board = board or {}
    outputs = outputs or {}
    layers = _timeline_layers(timeline)
    milestones = _milestones(board)
    blockers = _blockers(board)
    render = _render_block(timeline=timeline, outputs=outputs)
    target = _target_block(timeline=timeline,
                           requested_duration_seconds=requested_duration_seconds)

    # Plain-language, manual headline + guidance — never an automation claim.
    if layers > 0:
        headline = f"{layers} scene{'s' if layers != 1 else ''} on the timeline"
        guidance = "Open the Studio to keep editing, preview, and render."
    elif milestones:
        headline = "Ready to edit"
        guidance = ("Planning notes exist for this project — open the Studio to "
                    "build the timeline from them.")
    else:
        headline = "Set up your first scene"
        guidance = ("This project has no timeline yet. Open the Studio and add your "
                    "first scene to begin.")

    renders = outputs.get("renders") or []
    output_block = {
        "renders": renders,
        "render_count": len(renders),
        "latest_render": renders[-1] if renders else None,
        "asset_count": int(outputs.get("asset_count") or 0),
    }

    mode = "demo" if demo else ("fixture" if outputs.get("fixture") else "local")

    return {
        "version": "2.0",
        "kind": "project_overview",
        "project_id": project.get("id") or board.get("project_id"),
        "title": project.get("title") or board.get("title"),
        "owner": OWNER_YOU,
        "mode": mode,
        "headline": headline,
        "guidance": guidance,
        "has_timeline": layers > 0,
        "layer_count": layers,
        "milestones": milestones,
        "milestone_progress": {"completed": sum(1 for m in milestones if m["status"] == "done"),
                               "total": len(milestones)},
        "last_saved": _last_saved(board, timeline),
        "blockers": blockers,
        "outputs": output_block,
        "target": target,
        "render": render,
        "primary_action": {"id": "open_studio", "label": "Open Production Studio"},
        "diagnostics": ([{"kind": "stale",
                          "message": "Showing the last known state — reconnecting…"}]
                        if stale else []),
        "stale": bool(stale),
        "is_demo": mode == "demo",
        "is_fixture": mode == "fixture",
    }
