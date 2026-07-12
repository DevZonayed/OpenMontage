"""Duration edit-safety.

Changing a project's canonical ``target_duration_seconds`` is free while nothing
real depends on it. But once a timeline HAS LAYERS or the run plan is APPROVED,
changing it must NOT silently truncate/stretch/replan: the caller must pick an
explicit strategy after seeing the impact.

  * ``trim``   — deterministic: new (shorter) length; layers past the new end are
                 dropped, and a layer straddling the end is clamped.
  * ``extend`` — deterministic: new (longer) length; layers keep their positions
                 (empty tail added).
  * ``replan`` — queue an agent revision: new length recorded + ``pending_replan``
                 flag set; existing layers are PRESERVED (versioned), not touched.

The prior timeline is always versioned to ``history/`` before a change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from lib import duration as _dur
from lib import production_run as _pr
from lib import project_intake as _pi
from lib import timeline as _tl

_STRATEGIES = ("trim", "extend", "replan")


class DurationEditConflict(Exception):
    """Raised when a change needs an explicit strategy. Carries the impact so the
    UI can present old/new frames + the choice."""

    def __init__(self, impact: dict, status: int = 409):
        super().__init__("duration change requires an explicit strategy")
        self.impact = impact
        self.status = status


def _impact(old_secs: Optional[int], new_secs: int, fps: int) -> dict:
    old_frames = _dur.frames_for(old_secs, fps=fps) if old_secs else None
    new_frames = _dur.frames_for(new_secs, fps=fps)
    return {
        "old_seconds": old_secs, "new_seconds": new_secs,
        "old_formatted": _dur.format_mmss(old_secs) if old_secs else None,
        "new_formatted": _dur.format_mmss(new_secs),
        "old_frames": old_frames, "new_frames": new_frames,
        "frame_delta": (new_frames - old_frames) if old_frames is not None else None,
        "fps": fps,
    }


def _trim_layers(layers: list, new_total: int) -> list:
    kept = []
    for L in layers:
        start = int(L.get("start_frame", 0))
        dur = int(L.get("duration_frames", 0))
        if start >= new_total:
            continue  # entirely past the new end → drop
        if start + dur > new_total:
            L = {**L, "duration_frames": max(1, new_total - start)}  # clamp
        kept.append(L)
    return kept


def change_target_duration(project_dir: Path, new_value: Any, *,
                           strategy: Optional[str] = None) -> dict:
    """Apply a duration change with edit-safety. Raises DurationEditConflict when a
    strategy is required but not supplied; DurationError on an invalid value."""
    d = Path(project_dir)
    new_secs = _dur.parse_duration_input(new_value)  # validates 1..300
    if strategy is not None and strategy not in _STRATEGIES:
        raise DurationEditConflict(_impact(None, new_secs, _dur.DEFAULT_FPS), status=400)

    intake = _pi.read_intake(d) or {}
    old_secs = _dur.infer_target_seconds(intake)
    tl, tag = _tl.read_timeline(d)
    run = _pr.read_run(d) or {}
    fps = int((tl or {}).get("fps") or _dur.DEFAULT_FPS)

    has_layers = bool(tl and tl.get("layers"))
    approved = run.get("plan_approved") is True
    needs_strategy = has_layers or approved
    impact = _impact(old_secs, new_secs, fps)

    if needs_strategy and strategy not in _STRATEGIES:
        raise DurationEditConflict(impact)

    # Persist the new canonical duration first (source of truth).
    _pi.set_target_duration(d, new_secs)
    new_total = _dur.frames_for(new_secs, fps=fps)

    if tl is None:
        # No timeline yet — build a fresh frame-accurate skeleton.
        newtl = _tl.build_timeline({**intake, "target_duration_seconds": new_secs}, fps=fps)
        _tl.save_timeline(d, newtl)  # first write, no if_match
    else:
        newtl = dict(tl)
        newtl["target_duration_seconds"] = new_secs
        newtl["total_frames"] = new_total
        if needs_strategy and strategy == "trim":
            newtl["layers"] = _trim_layers(list(tl.get("layers", [])), new_total)
            newtl.pop("pending_replan", None)
        elif needs_strategy and strategy == "replan":
            newtl["pending_replan"] = True  # layers preserved; agent re-plans
        else:
            # extend (or free empty-skeleton update): keep layers as-is
            newtl.pop("pending_replan", None)
        _tl.save_timeline(d, newtl, if_match=tag)  # versions prior to history/

    return {"applied": True, "strategy": strategy if needs_strategy else None, "impact": impact}
