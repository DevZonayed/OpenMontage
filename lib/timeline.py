"""Canonical, versioned, project-local timeline/composition artifact.

ONE artifact (``projects/<id>/timeline.json``) is the single source of truth
consumed by BOTH the editor/Remotion Player preview AND the render argv/input —
so preview always matches output. It is frame-accurate: ``fps`` +
``total_frames`` derive from the project's canonical ``target_duration_seconds``
via exact integer math.

Safety: layer media must be PROJECT-LOCAL (no ``..`` traversal, no absolute
external path); timing must be non-negative integers; numbers must be finite;
layer IDs unique; types allowlisted. Writes are atomic with an optimistic-
concurrency ETag and append-only version history — a stale writer is rejected,
never silently clobbered.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Optional

from lib import duration as _duration

TIMELINE_FILENAME = "timeline.json"
SCHEMA_VERSION = "1.0"

LAYER_TYPES = ("video", "image", "text", "shape", "caption", "narration", "music", "sfx")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_INT_TIMING_FIELDS = ("track", "start_frame", "duration_frames", "z")
_FINITE_FIELDS = ("opacity",)


class TimelineError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def build_timeline(intake: dict, *, fps: int = _duration.DEFAULT_FPS) -> dict:
    """Build an empty, frame-accurate timeline skeleton from a project's intake."""
    secs = _duration.infer_target_seconds(intake)
    return {
        "version": SCHEMA_VERSION,
        "fps": int(fps),
        "target_duration_seconds": secs,
        "total_frames": _duration.frames_for(secs, fps=fps),
        "layers": [],
    }


def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _source_is_project_local(source: str, project_dir: Path) -> bool:
    if not isinstance(source, str) or not source or "\x00" in source:
        return False
    p = Path(source)
    if p.is_absolute() or ".." in p.parts:
        return False
    try:
        resolved = (project_dir / p).resolve()
        resolved.relative_to(project_dir.resolve())
        return True
    except (ValueError, OSError):
        return False


def validate_timeline(t: dict, *, project_dir: Path) -> None:
    """Raise TimelineError on any structural / safety violation."""
    if not isinstance(t, dict):
        raise TimelineError("timeline must be an object")
    if t.get("version") != SCHEMA_VERSION:
        raise TimelineError("unsupported timeline version")
    if not isinstance(t.get("fps"), int) or t["fps"] <= 0 or isinstance(t["fps"], bool):
        raise TimelineError("fps must be a positive integer")
    _duration.validate_target_seconds(t.get("target_duration_seconds"))
    if t.get("total_frames") != _duration.frames_for(t["target_duration_seconds"], fps=t["fps"]):
        raise TimelineError("total_frames must equal target_duration_seconds * fps")

    layers = t.get("layers")
    if not isinstance(layers, list):
        raise TimelineError("layers must be a list")
    seen: set[str] = set()
    for L in layers:
        if not isinstance(L, dict):
            raise TimelineError("each layer must be an object")
        lid = L.get("id")
        if not isinstance(lid, str) or not _ID_RE.match(lid):
            raise TimelineError("invalid layer id")
        if lid in seen:
            raise TimelineError(f"duplicate layer id: {lid}")
        seen.add(lid)
        if L.get("type") not in LAYER_TYPES:
            raise TimelineError("invalid layer type")
        for f in _INT_TIMING_FIELDS:
            v = L.get(f, 0)
            if isinstance(v, bool) or not isinstance(v, int):
                raise TimelineError(f"layer {f} must be an integer")
        if L["start_frame"] < 0 or L["duration_frames"] <= 0:
            raise TimelineError("layer timing must be non-negative (duration > 0)")
        for f in _FINITE_FIELDS:
            if f in L and not _is_finite_number(L[f]):
                raise TimelineError(f"layer {f} must be a finite number")
        for b in ("enabled", "locked"):
            if b in L and not isinstance(L[b], bool):
                raise TimelineError(f"layer {b} must be a boolean")
        src = L.get("source")
        if src is not None and not _source_is_project_local(src, project_dir):
            raise TimelineError("layer source must be a project-local path")


def _canonical(t: dict) -> bytes:
    return json.dumps(t, sort_keys=True, separators=(",", ":")).encode("utf-8")


def etag(t: dict) -> str:
    return hashlib.sha256(_canonical(t)).hexdigest()[:32]


def read_timeline(project_dir: Path) -> tuple[Optional[dict], Optional[str]]:
    p = Path(project_dir) / TIMELINE_FILENAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    return data, etag(data)


def save_timeline(project_dir: Path, t: dict, *, if_match: Optional[str] = None,
                  timestamp: Optional[str] = None) -> dict:
    """Validate + atomically write the timeline. Optimistic concurrency: when the
    file already exists, ``if_match`` MUST equal its current ETag or a 409 is
    raised. The prior version is appended to ``history/``. Returns {"etag": ...}."""
    project_dir = Path(project_dir)
    validate_timeline(t, project_dir=project_dir)
    p = project_dir / TIMELINE_FILENAME

    current, current_etag = read_timeline(project_dir)
    if current is not None:
        if if_match is None or if_match != current_etag:
            raise TimelineError("timeline was modified by someone else — reload and retry",
                                status=409)
        # version the prior copy (append-only history)
        hist = project_dir / "history"
        hist.mkdir(exist_ok=True)
        stamp = timestamp or str(int(time.time() * 1000)) if timestamp is not None else str(int(time.time() * 1000))
        (hist / f"timeline_{stamp}.json").write_text(json.dumps(current, indent=2), encoding="utf-8")

    tmp = project_dir / (TIMELINE_FILENAME + ".tmp")
    tmp.write_text(json.dumps(t, indent=2), encoding="utf-8")
    tmp.replace(p)
    return {"etag": etag(t)}


# Legacy edit-decision cut shape → canonical layers (non-destructive).
_CUT_TYPE_MAP = {
    "text_card": "text", "text": "text", "title": "text", "caption": "caption",
    "image": "image", "still": "image", "video": "video", "clip": "video",
    "narration": "narration", "music": "music", "sfx": "sfx",
}


def import_from_edit_decisions(edit_decisions: dict, intake: dict, *,
                               fps: int = _duration.DEFAULT_FPS) -> dict:
    """Build a canonical timeline from legacy ``edit_decisions.cuts`` WITHOUT
    mutating the source. Unknown cut types map to 'shape' as an inert placeholder."""
    t = build_timeline(intake, fps=fps)
    cuts = (edit_decisions or {}).get("cuts") or []
    layers = []
    for i, cut in enumerate(cuts):
        try:
            in_s = float(cut.get("in_seconds", cut.get("start_seconds", 0)) or 0)
            out_s = float(cut.get("out_seconds", cut.get("end_seconds", in_s)) or in_s)
        except (TypeError, ValueError):
            continue
        start = int(round(in_s * fps))
        dur = max(1, int(round((out_s - in_s) * fps)))
        ctype = _CUT_TYPE_MAP.get(str(cut.get("type", "")).lower(), "shape")
        layers.append({
            "id": f"cut{i+1}", "type": ctype, "track": 0,
            "start_frame": start, "duration_frames": dur, "z": i,
            "enabled": True, "locked": False, "opacity": 1.0,
            "source": cut.get("source") if _looks_local(cut.get("source")) else None,
            "provenance": {"origin": "edit_decisions", "revision": 0, "index": i},
        })
    t["layers"] = layers
    return t


def _looks_local(src: Any) -> bool:
    return isinstance(src, str) and bool(src) and not Path(src).is_absolute() and ".." not in Path(src).parts
