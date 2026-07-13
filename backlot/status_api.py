"""Backlot data layer for the read-only project OVERVIEW.

OpenMontage is a manual-first editor: there is no autonomous production worker.
This assembles the durable on-disk artifacts — the checkpoint milestone rail, the
timeline (layers + duration), the intake's requested duration, and rendered
outputs — into ONE overview the Board renders. Pure, synchronous, never blocks;
the async route in ``backlot/server.py`` adds the guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from lib.production_status import build_status_view


class StatusApiError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _load_timeline_lite(project_dir: Path) -> Optional[dict]:
    def _load():
        from lib.timeline import load_or_build_timeline

        tl, _tag = load_or_build_timeline(project_dir)
        return tl
    return _safe(_load, None)


def _load_project(project_dir: Path) -> dict:
    data = _safe(lambda: __import__("json").loads(
        (project_dir / "project.json").read_text(encoding="utf-8")), {})
    return data if isinstance(data, dict) else {}


def _requested_duration(project_dir: Path) -> Optional[float]:
    def _load():
        from lib.project_intake import read_intake

        intake = read_intake(project_dir) or {}
        val = intake.get("target_duration_seconds")
        return float(val) if val is not None else None
    return _safe(_load, None)


def _outputs(project_dir: Path) -> dict:
    """Rendered deliverables + a coarse asset count, scanned from disk."""
    renders: list[dict] = []
    rdir = project_dir / "renders"
    if rdir.is_dir():
        for p in sorted(rdir.glob("*.mp4")):
            renders.append({"path": f"renders/{p.name}", "label": p.stem.replace("_", " ").title()})
    asset_count = 0
    for sub in ("assets/images", "assets/video", "assets/audio", "assets/music"):
        d = project_dir / sub
        if d.is_dir():
            asset_count += sum(1 for _ in d.glob("*") if _.is_file())
    return {"renders": renders, "asset_count": asset_count}


def build_status_payload(
    project_dir: Path,
    *,
    demo: bool = False,
    stale: bool = False,
    **_ignored: Any,
) -> dict:
    """Assemble the read-only overview for one project."""
    from backlot.state import load_board_state

    project = _load_project(project_dir)
    board = _safe(lambda: load_board_state(project_dir), None)
    timeline = _load_timeline_lite(project_dir)
    requested = _requested_duration(project_dir)
    outputs = _outputs(project_dir)

    return build_status_view(
        project=project, board=board, timeline=timeline,
        requested_duration_seconds=requested, outputs=outputs,
        demo=bool(demo), stale=bool(stale))
