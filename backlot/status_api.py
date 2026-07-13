"""Backlot data layer for the CANONICAL production-status view.

One endpoint, one reconciled view model — consumed by BOTH the board (as a
read-only overview) and the Remotion Studio (as the action center) so they can
never disagree about where the production is or what to do next. Mirrors the
``brain_api`` / ``timeline_api`` convention: pure, synchronous functions that take
a ``project_dir`` and never block; the async routes in ``backlot/server.py`` add
the guards.

The heavy source reads (brain state, board state, run.json, timeline) are cheap
filesystem reads. The one potentially-slow read — the native Hermes Agent
readiness probe (a bounded local subprocess) — is cached with a short TTL so the
board's frequent /status polling never re-probes the agent every cycle.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from lib.production_status import build_status_view

# Short-lived, process-global cache for the (global, not per-project) Hermes Agent
# connection status so the board's frequent /status polling doesn't re-probe the
# local agent every cycle.
_CONN_TTL_SECONDS = 5.0
_conn_cache: dict[str, Any] = {"at": 0.0, "value": None}


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
    """Just enough of the timeline to gate the render button (layers + rendering)."""
    def _load():
        from lib.timeline import load_or_build_timeline

        tl, _tag = load_or_build_timeline(project_dir)
        return tl
    return _safe(_load, None)


def connection_view(
    *,
    detector: Optional[Any] = None,
    probe: bool = True,
    use_cache: bool = True,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """The native Hermes Agent connection status (cached). Never raises, and never
    contains an endpoint, token, project, or job."""
    clock = now or time.time
    if use_cache and detector is None:
        ts = clock()
        if _conn_cache["value"] is not None and (ts - _conn_cache["at"]) < _CONN_TTL_SECONDS:
            return _conn_cache["value"]
    from lib.production_brain.hermes_agent import agent_status

    value = _safe(lambda: agent_status(detector=detector, probe=probe),
                  {"kind": "hermes_agent", "status": "unknown", "available": False,
                   "server_name": "Hermes Agent",
                   "headline": "Hermes Agent status is unavailable.",
                   "detail": "", "actions": [], "installed": False, "ready": False})
    if use_cache and detector is None:
        _conn_cache["at"] = clock()
        _conn_cache["value"] = value
    return value


def build_status_payload(
    project_dir: Path,
    *,
    demo: bool = False,
    stale: bool = False,
    connection_detector: Optional[Any] = None,
    probe_connection: bool = True,
) -> dict:
    """Assemble the canonical status view for one project."""
    from backlot.brain_api import build_run_payload
    from backlot.state import load_board_state

    brain = _safe(lambda: build_run_payload(project_dir), None)
    board = _safe(lambda: load_board_state(project_dir), None)
    run = _safe(lambda: _load_run(project_dir), None)
    inbox = _safe(lambda: _load_inbox(project_dir), None)
    timeline = _load_timeline_lite(project_dir)
    connection = connection_view(detector=connection_detector, probe=probe_connection)

    return build_status_view(
        brain=brain, board=board, run=run, inbox=inbox, timeline=timeline,
        connection=connection, demo=bool(demo), stale=bool(stale))


def _load_run(project_dir: Path) -> dict:
    from lib.production_run import get_run, read_plan

    run = get_run(project_dir)
    try:
        run["plan"] = read_plan(project_dir)
    except Exception:
        pass
    return run


def _load_inbox(project_dir: Path) -> dict:
    from lib.agent_inbox import pending_agent_work

    return pending_agent_work(project_dir)


# --------------------------------------------------------------------------- #
# Native Hermes Agent connection control (invalidates the cache)
# --------------------------------------------------------------------------- #
def agent_connection(*, detector: Optional[Any] = None) -> dict:
    return connection_view(detector=detector, use_cache=detector is None)


def agent_connect(body: dict, *, detector: Optional[Any] = None) -> dict:
    """Connect (enable) the native Hermes Agent for this workspace.

    No credentials, endpoint, token, or project — the agent is auto-detected and
    verified locally. A failed verify does NOT enable (fail closed)."""
    from lib.production_brain.hermes_agent import connect

    result = connect(detector=detector)
    _invalidate_conn_cache()
    return result


def agent_disconnect(body: dict) -> dict:
    from lib.production_brain.hermes_agent import disconnect

    result = disconnect()
    _invalidate_conn_cache()
    return result


def _invalidate_conn_cache() -> None:
    _conn_cache["at"] = 0.0
    _conn_cache["value"] = None
