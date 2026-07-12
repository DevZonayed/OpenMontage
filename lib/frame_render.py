"""Real single-frame still render of the canonical timeline (pinned Remotion CLI).

Renders the generic ``TimelineFrame`` composition — which draws the project's
``timeline.json`` layers active at a given frame — to a PNG under
``<project>/renders/frames/frame_<n>.png``. Zero paid media, zero network: it
uses the render-ready Remotion runtime (pinned CLI + resolved browser) and a
bounded subprocess. It turns the editor's schematic "stage monitor" into an
actual rendered frame from the same canonical timeline the film render reads.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from lib import remotion_runtime as _rr
from lib import timeline as _tl

COMPOSITION_ID = "TimelineFrame"
ENTRY = "src/index.tsx"


class FrameRenderError(ValueError):
    """Bad request (e.g. a negative frame index)."""


def render_still(project_dir: Path, frame: int, *,
                 timeline: Optional[dict] = None,
                 runner: Optional[Callable] = None,
                 browser: Optional[str] = None,
                 timeout: int = 120,
                 doctor: Optional[Callable] = None) -> dict:
    """Render a single real frame of the timeline. Returns a sanitized result dict.

    ``frame`` is clamped to ``[0, total_frames-1]``; a negative frame is rejected.
    """
    if not isinstance(frame, int) or isinstance(frame, bool):
        raise FrameRenderError("frame must be an integer.")
    if frame < 0:
        raise FrameRenderError("frame must be >= 0.")

    # Absolute: the render subprocess runs with cwd=composer_dir, so a relative
    # output path would land under the composer, not the project.
    d = Path(project_dir).resolve()
    if timeline is None:
        timeline, _tag = _tl.read_timeline(d)
    if not timeline or not isinstance(timeline, dict):
        return {"ok": False, "reason": "This project has no timeline yet."}

    total = int(timeline.get("total_frames") or 0)
    if total <= 0:
        return {"ok": False, "reason": "The timeline has no frames."}
    frame = min(frame, total - 1)

    doc = (doctor or _rr.doctor)()
    if not doc.get("available"):
        return {"ok": False, "reason": doc.get("reason") or "Remotion is not render-ready."}

    out = d / "renders" / "frames" / f"frame_{frame}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Render to a unique temp path in the SAME dir, then os.replace into place —
    # two overlapping renders of the same frame can never serve a torn PNG.
    tmp_out = out.parent / f"frame_{frame}.{secrets.token_hex(4)}.tmp.png"

    try:
        from lib.timeline_render import build_meta
        meta = build_meta(d)
    except Exception:
        meta = {}
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "timeline_props.json"
        pf.write_text(json.dumps({"timeline": timeline, "meta": meta}), encoding="utf-8")
        be = browser if browser is not None else _rr.browser_executable()
        argv = _rr.still_argv(ENTRY, COMPOSITION_ID, str(tmp_out), frame=frame,
                              props=str(pf), extra=["--log=error"], browser=be)
        run = runner or (lambda a: subprocess.run(a, cwd=str(_rr.composer_dir()),
                                                  capture_output=True, text=True, timeout=timeout))
        try:
            proc = run(argv)
        except Exception:
            _safe_unlink(tmp_out)
            return {"ok": False, "reason": "The frame render failed to start."}

    rc = getattr(proc, "returncode", 1)
    if rc != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
        _safe_unlink(tmp_out)
        return {"ok": False, "reason": "The frame render did not complete."}
    os.replace(tmp_out, out)  # atomic publish
    return {
        "ok": True,
        "frame": frame,
        "url": f"/media/{d.name}/renders/frames/frame_{frame}.png",
        "size_bytes": out.stat().st_size,
    }


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass
