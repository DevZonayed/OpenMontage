"""Backlot timeline endpoint helpers — sanitized, read-mostly.

Builds the canonical timeline payload the editor consumes: the timeline artifact
(read from disk, or built/imported on demand), the target-vs-measured durations,
and the Remotion render-ready status. The SAME artifact drives a Remotion render,
so the editor preview and the final output cannot diverge.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from lib import duration as _dur
from lib import project_intake as _intake
from lib import remotion_runtime as _rr
from lib import timeline as _tl


def _read_edit_decisions(project_dir: Path) -> Optional[dict]:
    p = project_dir / "checkpoint_edit.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return (data.get("artifacts") or {}).get("edit_decisions")
    except Exception:
        return None


def _measured_output_seconds(project_dir: Path) -> Optional[float]:
    mp4 = project_dir / "renders" / "final.mp4"
    if not mp4.is_file():
        return None
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(mp4)],
            capture_output=True, text=True, timeout=30)
        return round(float(p.stdout.strip()), 2) if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None


def load_or_build_timeline(project_dir: Path) -> tuple[dict, str]:
    """Read the canonical timeline; if none exists, import from the legacy edit
    checkpoint, else build an empty frame-accurate skeleton from intake."""
    tl, tag = _tl.read_timeline(project_dir)
    if tl is None:
        intake = _intake.read_intake(project_dir) or {}
        ed = _read_edit_decisions(project_dir)
        tl = _tl.import_from_edit_decisions(ed, intake) if ed else _tl.build_timeline(intake)
        tag = _tl.etag(tl)
    return tl, tag


def build_timeline_payload(project_dir: Path) -> dict[str, Any]:
    tl, tag = load_or_build_timeline(project_dir)
    secs = tl.get("target_duration_seconds") or _dur.DEFAULT_TARGET_SECONDS
    doc = _rr.doctor()
    return {
        "timeline": tl,
        "etag": tag,
        "persisted": (project_dir / _tl.TIMELINE_FILENAME).is_file(),
        "fps": tl.get("fps"),
        "total_frames": tl.get("total_frames"),
        "target_duration_seconds": secs,
        "target_formatted": _dur.format_mmss(secs),
        "word_budget": _dur.word_budget(secs),
        "measured_output_seconds": _measured_output_seconds(project_dir),
        "remotion_render_ready": bool(doc["available"]),
        "remotion_reason": doc["reason"],
        "layer_types": list(_tl.LAYER_TYPES),
    }


def save_timeline_payload(project_dir: Path, body: dict) -> dict[str, Any]:
    """Validate + save an edited timeline with optimistic concurrency (ETag)."""
    tl = body.get("timeline")
    if not isinstance(tl, dict):
        raise _tl.TimelineError("timeline object required")
    res = _tl.save_timeline(project_dir, tl, if_match=body.get("if_match"))
    return {"ok": True, "etag": res["etag"]}
