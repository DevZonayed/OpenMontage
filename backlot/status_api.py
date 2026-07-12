"""Backlot data layer for the CANONICAL production-status view.

One endpoint, one reconciled view model — consumed by BOTH the board and the
Remotion Studio so they can never disagree about where the production is or what
to do next. Mirrors the ``brain_api`` / ``timeline_api`` convention: pure,
synchronous functions that take a ``project_dir`` and never block; the async
routes in ``backlot/server.py`` add the guards.

The heavy source reads (brain state, board state, run.json, timeline) are cheap
filesystem reads. The one potentially-network read — the Hermes/Mochlet health
handshake — is cached with a short TTL so board polling never hammers the local
service.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from lib.production_status import build_status_view

# Short-lived, process-global cache for the (global, not per-project) Hermes
# connection status so the board's frequent /status polling doesn't probe the
# local orchestrator every cycle.
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
    transport: Optional[Callable[..., Any]] = None,
    probe: bool = True,
    use_cache: bool = True,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """The Hermes connection status (cached). Never raises, never returns a token."""
    clock = now or time.time
    if use_cache and transport is None:
        ts = clock()
        if _conn_cache["value"] is not None and (ts - _conn_cache["at"]) < _CONN_TTL_SECONDS:
            return _conn_cache["value"]
    from lib.production_brain.connection import connection_status

    value = _safe(lambda: connection_status(transport=transport, probe=probe),
                  {"status": "unknown", "available": False,
                   "headline": "Hermes connection status is unavailable.",
                   "detail": "", "actions": [], "token_configured": False})
    if use_cache and transport is None:
        _conn_cache["at"] = clock()
        _conn_cache["value"] = value
    return value


def build_status_payload(
    project_dir: Path,
    *,
    demo: bool = False,
    stale: bool = False,
    connection_transport: Optional[Callable[..., Any]] = None,
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
    connection = connection_view(transport=connection_transport, probe=probe_connection)

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
# Guided connection control (invalidates the cache)
# --------------------------------------------------------------------------- #
def hermes_connection(*, transport: Optional[Callable[..., Any]] = None) -> dict:
    return connection_view(transport=transport, use_cache=transport is None)


def hermes_connect(body: dict, *, transport: Optional[Callable[..., Any]] = None) -> dict:
    """Guided connect. Body: {url?, token?, project_id?, kind?}. Never echoes the token.

    Returns a connection-status dict; ``status == "needs_project"`` carries the
    discovered ``projects`` for the UI to choose from (then re-POST with project_id).
    """
    from lib.production_brain.connection import ConnectionError, connect

    url = body.get("url")
    token = body.get("token")
    project_id = body.get("project_id")
    kind = body.get("kind")
    for name, val in (("url", url), ("token", token), ("project_id", project_id), ("kind", kind)):
        if val is not None and not isinstance(val, str):
            raise StatusApiError(f"{name} must be a string", status=400)
    try:
        result = connect(url=url or None, token=token or None,
                         project_id=project_id or None, kind=kind or None,
                         transport=transport)
    except ConnectionError as exc:
        raise StatusApiError(str(exc), status=exc.status) from exc
    _invalidate_conn_cache()
    return result


def hermes_disconnect(body: dict) -> dict:
    from lib.production_brain.connection import disconnect

    result = disconnect(wipe_token=bool(body.get("wipe_token")))
    _invalidate_conn_cache()
    return result


def _invalidate_conn_cache() -> None:
    _conn_cache["at"] = 0.0
    _conn_cache["value"] = None
