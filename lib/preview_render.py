"""Free preview-animatic render via the PINNED local Remotion CLI.

Renders the ``PreviewAnimatic`` composition — a short, self-contained motion
summary of an approved plan (title, target duration, frame budget, providers,
sections) — to ``<project>/renders/preview.mp4``. Zero paid media, zero network:
it uses the render-ready Remotion runtime from Slice A (pinned CLI + resolved
browser), a bounded subprocess timeout, and atomic-ish output. It is honestly a
PREVIEW of the plan, never the final agent-generated film.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from lib import duration as _dur
from lib import production_run as _pr
from lib import remotion_runtime as _rr
from lib import timeline as _tl

COMPOSITION_ID = "PreviewAnimatic"
PREVIEW_FILENAME = "preview.mp4"


def build_props(intake: dict, plan: dict, timeline: dict) -> dict:
    """Pure: assemble the animatic props from the project's intake + plan + timeline."""
    intake = intake or {}
    plan = plan or {}
    pr = plan.get("provider_readiness", {}) or {}
    secs = _dur.infer_target_seconds(intake)
    runtimes = [k for k, v in (pr.get("composition_runtimes") or {}).items() if v]
    sections = [str(L.get("type")) for L in (timeline or {}).get("layers", []) if L.get("type")][:8]
    return {
        "title": (intake.get("title") or intake.get("project_id") or "Untitled project")[:80],
        "pipeline": intake.get("pipeline_type") or "animation",
        "targetFormatted": _dur.format_mmss(secs),
        "totalFrames": _dur.frames_for(secs),
        "wordBudget": _dur.word_budget(secs),
        "sections": sections,
        "providersConfigured": int(pr.get("capabilities_configured") or 0),
        "providersTotal": int(pr.get("capabilities_total") or 0),
        "runtimes": runtimes,
    }


def _measured_seconds(path: Path) -> Optional[float]:
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", str(path)],
                           capture_output=True, text=True, timeout=30)
        return round(float(p.stdout.strip()), 2) if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None


def render_preview(project_dir: Path, *, props: Optional[dict] = None,
                   runner: Optional[Callable] = None, browser: Optional[str] = None,
                   timeout: int = 180, doctor: Optional[Callable] = None) -> dict:
    """Render the free preview animatic. Returns a sanitized result dict."""
    d = Path(project_dir)
    out = d / "renders" / PREVIEW_FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = (doctor or _rr.doctor)()
    if not doc.get("available"):
        return {"ok": False, "reason": doc.get("reason") or "Remotion is not render-ready."}

    if props is None:
        from lib.project_intake import read_intake
        intake = read_intake(d) or {}
        plan = _pr.read_plan(d) or {}
        tl, _ = _tl.read_timeline(d)
        props = build_props(intake, plan, tl or {})

    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "preview_props.json"
        pf.write_text(json.dumps(props), encoding="utf-8")
        be = browser if browser is not None else _rr.browser_executable()
        argv = _rr.render_argv("src/index.tsx", COMPOSITION_ID, str(out),
                               props=str(pf), extra=["--scale=0.5", "--log=error"], browser=be)
        run = runner or (lambda a: subprocess.run(a, cwd=str(_rr.composer_dir()),
                                                  capture_output=True, text=True, timeout=timeout))
        try:
            proc = run(argv)
        except Exception:
            return {"ok": False, "reason": "The preview render failed to start."}

    rc = getattr(proc, "returncode", 1)
    if rc != 0 or not out.is_file() or out.stat().st_size == 0:
        return {"ok": False, "reason": "The preview render did not complete."}
    return {
        "ok": True,
        "preview_url": f"/media/{d.name}/renders/{PREVIEW_FILENAME}",
        "size_bytes": out.stat().st_size,
        "measured_seconds": _measured_seconds(out),
    }


def generate_and_record(project_dir: Path) -> dict:
    """Render the preview and record the outcome on the run (log + preview field)."""
    res = render_preview(Path(project_dir))
    run = _pr.read_run(project_dir)
    if isinstance(run, dict):
        now = _pr._iso_now()
        entries = run.get("log") or []
        if res.get("ok"):
            entries.append({"ts": now, "phase": "preview",
                            "message": f"Rendered a free preview animatic ({res.get('measured_seconds')}s)."})
            run["preview"] = {"url": res["preview_url"], "measured_seconds": res.get("measured_seconds"), "at": now}
        else:
            entries.append({"ts": now, "phase": "preview",
                            "message": f"Preview render failed: {res.get('reason')}"})
        run["log"] = entries[-80:]
        run["updated_at"] = now
        _pr._write_run(project_dir, run)
    return res
