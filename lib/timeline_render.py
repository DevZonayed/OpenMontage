"""Full-timeline Remotion render — a real, complete (capped) preview MP4.

Renders the cinematic ``TimelineFrame`` composition over the whole timeline (up
to a frame cap, so a 5-minute film doesn't block the editor) to
``<project>/renders/timeline_preview.mp4``. Free/local: pinned Remotion CLI +
resolved browser, bounded subprocess, atomic publish. This is the editor's
"complete Remotion render" — the exact pixels the final film uses, playable and
scrubbable in a real <video>, not a schematic.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from lib import duration as _dur
from lib import remotion_runtime as _rr
from lib import timeline as _tl

COMPOSITION_ID = "TimelineFrame"
ENTRY = "src/index.tsx"
PREVIEW_FILENAME = "timeline_preview.mp4"
DEFAULT_MAX_FRAMES = 900   # ~30s @30fps — a responsive but real preview window


def build_meta(project_dir: Path) -> dict:
    """Assemble the title-card metadata from the project's intake (pure-ish read)."""
    try:
        from lib.project_intake import read_intake
        intake = read_intake(project_dir) or {}
    except Exception:
        intake = {}
    secs = _dur.infer_target_seconds(intake) if hasattr(_dur, "infer_target_seconds") else \
        int(intake.get("target_duration_seconds") or 0)
    return {
        "title": (intake.get("title") or intake.get("project_id") or project_dir.name or "Untitled")[:80],
        "targetFormatted": _dur.format_mmss(secs) if secs else "0:00",
        "pipeline": intake.get("pipeline_type") or "animation",
    }


def _measured_seconds(path: Path) -> Optional[float]:
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", str(path)],
                           capture_output=True, text=True, timeout=30)
        return round(float(p.stdout.strip()), 2) if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None


def render_timeline_preview(project_dir: Path, *,
                            timeline: Optional[dict] = None,
                            max_frames: int = DEFAULT_MAX_FRAMES,
                            scale: float = 0.5,
                            runner: Optional[Callable] = None,
                            browser: Optional[str] = None,
                            timeout: int = 600,
                            doctor: Optional[Callable] = None) -> dict:
    """Render the timeline to a real MP4 (capped to ``max_frames``). Sanitized result."""
    d = Path(project_dir).resolve()
    if timeline is None:
        timeline, _tag = _tl.read_timeline(d)
    if not timeline or not isinstance(timeline, dict):
        return {"ok": False, "reason": "This project has no timeline yet."}

    total = int(timeline.get("total_frames") or 0)
    if total <= 0:
        return {"ok": False, "reason": "The timeline has no frames."}
    frames_rendered = min(total, max(1, int(max_frames)))
    truncated = frames_rendered < total
    end_index = frames_rendered - 1

    doc = (doctor or _rr.doctor)()
    if not doc.get("available"):
        return {"ok": False, "reason": doc.get("reason") or "Remotion is not render-ready."}

    out = d / "renders" / PREVIEW_FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.parent / f"timeline_preview.{secrets.token_hex(4)}.tmp.mp4"

    props = {"timeline": timeline, "meta": build_meta(d)}
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "timeline_props.json"
        pf.write_text(json.dumps(props), encoding="utf-8")
        be = browser if browser is not None else _rr.browser_executable()
        argv = _rr.render_argv(ENTRY, COMPOSITION_ID, str(tmp_out), props=str(pf),
                               extra=[f"--frames=0-{end_index}", f"--scale={scale}", "--log=error"],
                               browser=be)
        run = runner or (lambda a: subprocess.run(a, cwd=str(_rr.composer_dir()),
                                                  capture_output=True, text=True, timeout=timeout))
        try:
            proc = run(argv)
        except Exception:
            _safe_unlink(tmp_out)
            return {"ok": False, "reason": "The timeline render failed to start."}

    rc = getattr(proc, "returncode", 1)
    if rc != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
        _safe_unlink(tmp_out)
        return {"ok": False, "reason": "The timeline render did not complete."}
    os.replace(tmp_out, out)  # atomic publish
    return {
        "ok": True,
        "url": f"/media/{d.name}/renders/{PREVIEW_FILENAME}",
        "size_bytes": out.stat().st_size,
        "measured_seconds": _measured_seconds(out),
        "frames_rendered": frames_rendered,
        "total_frames": total,
        "truncated": truncated,
        "fps": int(timeline.get("fps") or 30),
    }


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass
