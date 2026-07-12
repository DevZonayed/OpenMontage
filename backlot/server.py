"""Backlot server — FastAPI app: board state API, SSE change feed, media.

The watcher observes ``projects/`` with watchfiles; on any change it bumps a
per-project version and wakes SSE subscribers, who tell the browser to
refetch state.

Board reads are read-only. The only writes the server performs are through the
narrow, CSRF-guarded ``POST /api/projects`` endpoint, which initializes a NEW
project workspace + intake artifact via ``lib.project_intake.create_project``
(canonical ``init_project``). Production itself remains agent-driven; the server
never edits an existing project's pipeline artifacts.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backlot.state import PROJECTS_DIR, REPO_ROOT, list_projects, load_board_state, summarize_project

UI_DIR = Path(__file__).resolve().parent / "ui"
THUMB_CACHE_DIR = REPO_ROOT / ".backlot" / "thumbs"
THUMB_WIDTHS = (320, 640, 960)

# Provider/engine preferences file (module-level so tests can redirect it).
PREFS_PATH = REPO_ROOT / "providers.yaml"

# Process-scoped CSRF token: minted once per server process, handed to the
# same-origin page via GET /api/csrf, and required (in a custom header) on EVERY
# state-changing request. A cross-site page can't read it (same-origin policy)
# and the custom header forces a CORS preflight we never permit — so a
# cross-site POST cannot forge a mutation. Regenerated per process (per restart).
_CSRF_TOKEN = secrets.token_urlsafe(32)
_CSRF_HEADER = "x-openmontage-csrf"
_MAX_MUTATION_BYTES = 16 * 1024  # generous for a key + small JSON; blocks abuse

# ---------------------------------------------------------------------------
# In-process per-client rate limiting for expensive/sensitive mutations.
#
# Dependency-free sliding-window limiter. Applies ONLY to costly or
# security-sensitive POSTs (Z.AI credential lifecycle, engine OAuth actions,
# project creation) — NEVER to safe GETs or the SSE feed. Keyed by the DIRECT
# socket peer (``request.client.host``); we deliberately do NOT trust
# ``X-Forwarded-For`` / ``X-Real-IP`` by default (Backlot binds loopback, and
# honoring arbitrary proxy headers would let a caller forge unlimited keys).
# Buckets are memory-bounded and stale-evicted. Checked AFTER the CSRF/origin
# guard, so an unauthenticated flood 403s without consuming a victim's budget.
# ---------------------------------------------------------------------------

# (limit, window_seconds) per bucket. Generous enough that real UI flows (a few
# clicks) never trip; tight enough to stop an automated abuse loop.
_RATE_LIMITS = {
    "credential": (20, 10.0),
    "action": (20, 10.0),
    "projects": (12, 10.0),
    "runtime": (6, 60.0),  # runtime install/repair/verify are expensive + rare
    "timeline": (30, 10.0),  # editor saves — frequent but bounded
    "run": (10, 60.0),       # start/cancel a production run — spawns a worker
    "render": (12, 60.0),    # single-frame stills — a subprocess each, but scrub-and-render is interactive
    "inbox": (40, 10.0),     # agent-queue READ polled ~every 2s by the board — own bucket, never drains 'projects'
    "brain": (60, 10.0),     # production-brain control (approve/cancel/retry/resume) — bursty on a live run
    "preferences": (30, 10.0),  # learned-style read/update/reset — user-driven, infrequent
    "hermes": (12, 60.0),        # guided Hermes/Mochlet connect — user-driven, rare
}
_RATE_MAX_KEYS = 4096  # hard cap on tracked buckets (memory bound)


def _rate_now() -> float:
    """Monotonic clock indirection so tests can pin/advance time deterministically."""
    return time.monotonic()


class _SlidingWindowRateLimiter:
    """Tiny fixed-memory sliding-window limiter. Single-threaded by design: the
    check runs on the event-loop thread before any ``to_thread`` offload."""

    def __init__(self) -> None:
        self._buckets: dict[tuple, list[float]] = {}

    def hit(self, key: tuple, limit: int, window: float, now: float) -> tuple[bool, float]:
        stamps = self._buckets.get(key)
        if stamps is None:
            stamps = []
            self._buckets[key] = stamps
        cutoff = now - window
        drop = 0
        for t in stamps:  # list is time-ordered; drop those outside the window
            if t > cutoff:
                break
            drop += 1
        if drop:
            del stamps[:drop]
        if len(stamps) >= limit:
            return False, max(0.0, window - (now - stamps[0]))
        stamps.append(now)
        return True, 0.0

    def evict_stale(self, now: float, max_window: float) -> None:
        if not self._buckets:
            return
        dead = [k for k, v in self._buckets.items() if not v or now - v[-1] > max_window]
        for k in dead:
            self._buckets.pop(k, None)
        if len(self._buckets) > _RATE_MAX_KEYS:  # hard memory cap
            ordered = sorted(self._buckets, key=lambda k: self._buckets[k][-1])
            for k in ordered[: len(self._buckets) - _RATE_MAX_KEYS]:
                self._buckets.pop(k, None)

    def clear(self) -> None:
        self._buckets.clear()


_rate_limiter = _SlidingWindowRateLimiter()
_RATE_MAX_WINDOW = max(w for _, w in _RATE_LIMITS.values())


def reset_rate_limits() -> None:
    """Test hook: clear all rate-limit state between cases."""
    _rate_limiter.clear()


def _client_key(request: Request) -> str:
    # DIRECT peer only — do NOT trust X-Forwarded-For / X-Real-IP by default.
    return request.client.host if request.client else "unknown"


def _enforce_rate(request: Request, bucket: str) -> None:
    """Raise a sanitized 429 (+Retry-After) if this client exceeded ``bucket``."""
    limit, window = _RATE_LIMITS[bucket]
    now = _rate_now()
    allowed, retry_after = _rate_limiter.hit((_client_key(request), bucket), limit, window, now)
    _rate_limiter.evict_stale(now, _RATE_MAX_WINDOW)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="too many requests; please slow down",
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )


# Paths inside a project whose changes are pure noise for the board.
# ``brain`` holds the production-brain telemetry (state.json + run_events.jsonl):
# it changes at high frequency on a live run and is surfaced by its own polling
# endpoints (/api/project/{id}/brain[/events]) — exactly like run.json — so it
# must not trigger a full board SSE re-render (which would flicker the UI).
_IGNORE_PARTS = {"node_modules", ".git", "__pycache__", ".cache", "brain"}
# Run/timeline state files change frequently (worker heartbeat, editor saves) and
# are surfaced by their own polling endpoints — don't trigger a full board SSE
# re-render (which would flicker the UI and collapse expanded sections).
_IGNORE_FILES = {"run.json", "run.json.tmp", "run_plan.json",
                 "timeline.json", "timeline.json.tmp"}

SSE_HEARTBEAT_SECONDS = 15


class ChangeHub:
    """Fan-out of project-change notifications to SSE subscribers.

    Subscriptions are filtered: a board subscribed to one project only ever
    receives that project's ids, so unrelated-project bursts can't flood its
    queue and starve out the one notification it actually needs.
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, Optional[str]] = {}

    def subscribe(self, project_id: Optional[str] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers[q] = project_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def publish(self, project_id: str) -> None:
        for q, only in list(self._subscribers.items()):
            if only is not None and only != project_id:
                continue
            try:
                q.put_nowait(project_id)
            except asyncio.QueueFull:
                # Queue holds only THIS subscriber's relevant ids, so a full
                # queue already guarantees a pending wake-up → safe to drop.
                pass


hub = ChangeHub()

# Library summaries are expensive to derive (full state parse per project);
# cache per project and invalidate from the watcher.
_summary_cache: dict[str, dict] = {}


def _invalidate_summary(project_id: str) -> None:
    _summary_cache.pop(project_id, None)


def _cached_summaries() -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        cached = _summary_cache.get(entry.name)
        if cached is None:
            try:
                cached = summarize_project(entry)
            except Exception:
                cached = {
                    "project_id": entry.name, "title": entry.name,
                    "pipeline_type": "unknown", "has_pipeline_state": False,
                    "poster": None, "live": False, "last_activity": 0,
                    "active_stage": None, "awaiting_human": False,
                    "stage_states": [], "completed_count": 0,
                    "render_count": 0, "scene_count": 0, "error": "unreadable",
                }
            _summary_cache[entry.name] = cached
        summaries.append(cached)
    summaries.sort(key=lambda s: (not s["live"], -(s["last_activity"] or 0)))
    return summaries


# Watch-loop hot path: pure string comparison, no per-path filesystem calls
# (change batches can be thousands of paths during a render).
import os as _os

_PROJECTS_ROOT_STR = _os.path.normcase(str(PROJECTS_DIR.resolve()))


def _project_of_change(path_str: str) -> Optional[str]:
    """Map a changed filesystem path to a project id (None = irrelevant)."""
    norm = _os.path.normcase(_os.path.normpath(path_str))
    if not norm.startswith(_PROJECTS_ROOT_STR):
        return None
    rel = norm[len(_PROJECTS_ROOT_STR):].lstrip("\\/")
    if not rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if _IGNORE_PARTS.intersection(parts):
        return None
    if parts[-1] in _IGNORE_FILES:
        return None
    return parts[0]


async def _watch_projects() -> None:
    """Background task: watch projects/ and publish debounced changes."""
    try:
        from watchfiles import awatch
    except ImportError:
        return  # watcher unavailable → board still works via manual refresh
    if not PROJECTS_DIR.is_dir():
        return
    async for changes in awatch(PROJECTS_DIR, recursive=True, step=400):
        touched: set[str] = set()
        for _change, path_str in changes:
            pid = _project_of_change(path_str)
            if pid:
                touched.add(pid)
        for pid in touched:
            _invalidate_summary(pid)
            hub.publish(pid)


def create_app(*, render_base_url: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Backlot", docs_url=None, redoc_url=None)

    # Trusted loopback base the CLI render resolves project media against. It is
    # provided EXPLICITLY by the server runtime — `cmd_serve` passes the ACTUAL
    # bound port (http://127.0.0.1:<port>) — or from operator config
    # (BACKLOT_RENDER_BASE_URL / BACKLOT_PORT). NEVER derived from a request Host
    # header. Fail CLOSED (None) when nothing trusted is configured: the render
    # endpoints then refuse to render rather than guess a (possibly wrong) port.
    try:
        from lib.render_meta import resolve_render_base_url
        app.state.render_base_url = resolve_render_base_url(
            base_url=render_base_url, require_explicit=True)
    except Exception:
        app.state.render_base_url = None

    @app.middleware("http")
    async def _no_cache_ui(request: Request, call_next):
        # The UI is a live local tool — never let the browser serve a stale
        # board.js/settings.js after a restart (revalidate every load).
        resp = await call_next(request)
        p = request.url.path
        if p.startswith("/ui/") or p in ("/", "/settings") or p.startswith("/p/"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.watch_task = asyncio.create_task(_watch_projects())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "watch_task", None)
        if task:
            task.cancel()

    # ---- API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": "backlot"}

    @app.get("/api/projects")
    async def projects() -> list:
        return await asyncio.to_thread(_cached_summaries)

    @app.get("/api/project/{project_id}/state")
    async def project_state(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(load_board_state, project_dir)

    # ---- Providers / engines settings ---------------------------------
    # Read: subscription engines + auth state, composition runtimes +
    # diagnostics, media capabilities, and current preferences. Write:
    # validated preferences only (no secrets — enforced in providers_api).

    @app.get("/api/csrf")
    async def csrf() -> dict:
        """Hand the same-origin page its process-scoped CSRF token. A cross-site
        page can send this GET but cannot READ the response (no permissive CORS),
        so it can't obtain the token to forge a mutation."""
        return {"csrf": _CSRF_TOKEN}

    @app.get("/api/providers")
    async def providers_get(probe: int = 1) -> dict:
        from backlot.providers_api import build_providers_payload
        return await asyncio.to_thread(
            build_providers_payload, probe_auth=bool(probe), prefs_path=PREFS_PATH
        )

    @app.post("/api/providers")
    async def providers_post(request: Request) -> dict:
        from backlot.providers_api import PreferencesSaveError, save_preferences
        body = await _guarded_json_body(request)
        try:
            return await asyncio.to_thread(save_preferences, body, prefs_path=PREFS_PATH)
        except PreferencesSaveError as exc:
            raise HTTPException(status_code=422 if exc.is_secret else 400, detail=str(exc))

    @app.post("/api/providers/action")
    async def providers_action(request: Request) -> dict:
        """Allowlisted engine OAuth actions (status/connect/logout). No secrets
        or raw command output ever leave lib.engine_actions."""
        from lib.engine_actions import EngineActionError, run_engine_action
        body = await _guarded_json_body(request)
        _enforce_rate(request, "action")
        engine = body.get("engine")
        action = body.get("action")
        confirm = bool(body.get("confirm", False))
        try:
            return await asyncio.to_thread(
                run_engine_action, engine, action, confirm=confirm
            )
        except EngineActionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/providers/credential")
    async def providers_credential(request: Request) -> dict:
        """Z.AI API-key lifecycle: store / verify / remove / launch. The key is
        stored ONLY in the OS keychain and is never returned, logged, or echoed."""
        from backlot.providers_api import CredentialError, handle_credential
        body = await _guarded_json_body(request)
        _enforce_rate(request, "credential")
        try:
            return await asyncio.to_thread(handle_credential, body)
        except CredentialError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))

    @app.post("/api/providers/runtime")
    async def providers_runtime(request: Request) -> dict:
        """Fixed, allowlisted composition-runtime maintenance (Remotion
        verify/install/repair). No arbitrary package/command/path from the
        caller; output is sanitized (booleans + a doctor report)."""
        from lib.runtime_actions import RuntimeActionError, run_runtime_action
        body = await _guarded_json_body(request)
        _enforce_rate(request, "runtime")
        runtime = body.get("runtime")
        action = body.get("action")
        try:
            return await asyncio.to_thread(run_runtime_action, runtime, action)
        except RuntimeActionError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    # ---- New project (workspace + intake only; production stays agent-driven) --

    @app.get("/api/pipelines")
    async def pipelines() -> list:
        from lib.project_intake import list_pipelines_meta
        return await asyncio.to_thread(list_pipelines_meta)

    @app.post("/api/projects")
    async def create_project_route(request: Request) -> dict:
        from lib.project_intake import ProjectIntakeError, create_project
        body = await _guarded_json_body(request)
        _enforce_rate(request, "projects")
        try:
            return await asyncio.to_thread(
                create_project, body.get("title"), body.get("brief") or "",
                body.get("pipeline"), project_id=body.get("project_id"), base=PROJECTS_DIR,
                target_duration_seconds=body.get("target_duration_seconds"),
            )
        except ProjectIntakeError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))

    @app.get("/api/project/{project_id}/intake")
    async def project_intake_route(project_id: str) -> dict:
        from lib.project_intake import read_intake
        project_dir = _safe_project_dir(project_id)
        return (await asyncio.to_thread(read_intake, project_dir)) or {}

    @app.get("/api/project/{project_id}/timeline")
    async def project_timeline_get(project_id: str) -> dict:
        """Canonical timeline payload the editor + Remotion render both consume."""
        from backlot.timeline_api import build_timeline_payload
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(build_timeline_payload, project_dir)

    @app.post("/api/project/{project_id}/frame")
    async def project_frame_still(project_id: str, request: Request) -> dict:
        """Render ONE real frame of the canonical timeline (pinned Remotion CLI,
        free/local) — turns the editor's schematic monitor into actual pixels."""
        from lib.frame_render import FrameRenderError, render_still
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "render")
        frame = body.get("frame", 0)
        try:
            frame = int(frame)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="frame must be an integer")
        base = getattr(request.app.state, "render_base_url", None)
        if not base:
            raise HTTPException(
                status_code=503,
                detail="Render base URL is not configured; project media cannot be "
                       "resolved. Start via `backlot serve` or set BACKLOT_PORT / "
                       "BACKLOT_RENDER_BASE_URL.")
        try:
            res = await asyncio.to_thread(render_still, project_dir, frame, base_url=base)
        except FrameRenderError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason") or "Frame render failed.")
        return res

    @app.post("/api/project/{project_id}/timeline/render")
    async def project_timeline_render(project_id: str, request: Request) -> dict:
        """Render the WHOLE canonical timeline to a real (capped) MP4 via the pinned
        Remotion CLI — the editor's complete, playable render (free/local)."""
        from lib.timeline_render import render_timeline_preview
        project_dir = _safe_project_dir(project_id)
        await _guarded_json_body(request)   # CSRF + origin + size guard
        _enforce_rate(request, "render")
        base = getattr(request.app.state, "render_base_url", None)
        if not base:
            raise HTTPException(
                status_code=503,
                detail="Render base URL is not configured; project media cannot be "
                       "resolved. Start via `backlot serve` or set BACKLOT_PORT / "
                       "BACKLOT_RENDER_BASE_URL.")
        res = await asyncio.to_thread(render_timeline_preview, project_dir, base_url=base)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason") or "Timeline render failed.")
        return res

    @app.get("/api/project/{project_id}/agent-inbox")
    async def project_agent_inbox(project_id: str, request: Request) -> dict:
        """Read-only: everything currently queued for the agent (or awaiting the
        user) — queued layer regenerations, a pending duration re-plan, and the
        run approval state. Honest visibility, no generation (Rule Zero)."""
        from lib.agent_inbox import pending_agent_work
        project_dir = _safe_project_dir(project_id)
        _enforce_rate(request, "inbox")
        return await asyncio.to_thread(pending_agent_work, project_dir)

    @app.post("/api/project/{project_id}/timeline/revision")
    async def project_layer_revision(project_id: str, request: Request) -> dict:
        """Queue an honest AI-regeneration request for ONE layer (agent-driven).
        Does NOT generate anything — appends a versioned request + marks queued."""
        from lib.revision_requests import RevisionError, queue_revision
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "timeline")
        layer_id = body.get("layer_id")
        prompt = body.get("prompt")
        if not isinstance(layer_id, str) or not layer_id:
            raise HTTPException(status_code=400, detail="layer_id is required")
        try:
            return await asyncio.to_thread(
                queue_revision, project_dir, layer_id, prompt, constraints=body.get("constraints"))
        except RevisionError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.post("/api/project/{project_id}/duration")
    async def project_duration_set(project_id: str, request: Request) -> dict:
        """Change the canonical target duration. Edit-safe: if a timeline has layers
        or the plan is approved, requires an explicit strategy and returns the
        impact (409) until one is chosen."""
        from lib.duration import DurationError
        from lib.duration_edit import DurationEditConflict, change_target_duration
        from lib.project_intake import ProjectIntakeError
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "timeline")
        try:
            return await asyncio.to_thread(
                change_target_duration, project_dir, body.get("duration"),
                strategy=body.get("strategy"))
        except DurationEditConflict as exc:
            raise HTTPException(status_code=exc.status,
                                detail={"error": "strategy_required", "impact": exc.impact})
        except DurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ProjectIntakeError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.post("/api/project/{project_id}/timeline")
    async def project_timeline_save(project_id: str, request: Request) -> dict:
        """Save an edited timeline (validated, path-confined, optimistic-ETag)."""
        from backlot.timeline_api import save_timeline_payload
        from lib.timeline import TimelineError
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "timeline")
        try:
            return await asyncio.to_thread(save_timeline_payload, project_dir, body)
        except TimelineError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    # ---- Production run lifecycle (real bounded free preflight/planning worker) --

    @app.get("/api/project/{project_id}/run")
    async def project_run_get(project_id: str) -> dict:
        """Reconciled production-run state + the free preflight plan (run_plan.json)."""
        from lib.production_run import get_run, read_plan
        project_dir = _safe_project_dir(project_id)

        def _payload():
            run = get_run(project_dir)
            run["plan"] = read_plan(project_dir)
            return run
        return await asyncio.to_thread(_payload)

    @app.post("/api/project/{project_id}/run/approve")
    async def project_run_approve(project_id: str, request: Request) -> dict:
        """Record human approval of the preflight plan (does NOT auto-generate)."""
        from lib.production_run import RunError, approve_plan
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "run")
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HTTPException(status_code=400, detail="run_id is required")
        try:
            return await asyncio.to_thread(approve_plan, project_dir, run_id)
        except RunError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.post("/api/project/{project_id}/run")
    async def project_run_start(project_id: str, request: Request) -> dict:
        """Start a real production run (idempotent — returns the active run if one
        is already in progress). Spawns a fixed argv-only worker."""
        from lib.production_run import RunError, start_run
        from lib.project_intake import read_intake
        project_dir = _safe_project_dir(project_id)
        await _guarded_json_body(request)
        _enforce_rate(request, "run")
        intake = await asyncio.to_thread(read_intake, project_dir)
        target = (intake or {}).get("target_duration_seconds")
        try:
            return await asyncio.to_thread(
                start_run, project_dir, project_id, target_duration_seconds=target)
        except RunError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.post("/api/project/{project_id}/run/preview")
    async def project_run_preview(project_id: str, request: Request) -> dict:
        """Render a FREE Remotion preview animatic of the plan (no paid media)."""
        from lib.preview_render import generate_and_record
        project_dir = _safe_project_dir(project_id)
        await _guarded_json_body(request)
        _enforce_rate(request, "run")
        res = await asyncio.to_thread(generate_and_record, project_dir)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason") or "Preview render failed.")
        return res

    @app.post("/api/project/{project_id}/run/cancel")
    async def project_run_cancel(project_id: str, request: Request) -> dict:
        """Cancel the EXACT active run (by run_id) — stops only that worker."""
        from lib.production_run import RunError, cancel_run
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "run")
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HTTPException(status_code=400, detail="run_id is required")
        try:
            return await asyncio.to_thread(cancel_run, project_dir, run_id)
        except RunError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    # ---- Production brain (canonical run state + append-only event history) ----
    # The Hermes brain's observable telemetry: which agent/job/tool/provider is
    # doing which task, current stage, progress, elapsed time, latest event,
    # outputs, approvals, blockers, errors. Reads are non-blocking snapshots
    # (the board polls, like it polls /run); control is CSRF + rate guarded.

    @app.get("/api/project/{project_id}/brain")
    async def project_brain_state(project_id: str) -> dict:
        from backlot.brain_api import build_run_payload
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(build_run_payload, project_dir)

    @app.get("/api/project/{project_id}/brain/events")
    async def project_brain_events(project_id: str, after: int = 0, limit: int = 200) -> dict:
        """Cursor page of the append-only event history (non-blocking snapshot)."""
        from backlot.brain_api import read_events_payload
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_events_payload, project_dir, after=after, limit=limit)

    @app.get("/api/project/{project_id}/brain/assets")
    async def project_brain_assets(project_id: str) -> dict:
        from backlot.brain_api import assets_payload
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(assets_payload, project_dir)

    def _brain_control(handler_name: str):
        async def _route(project_id: str, request: Request) -> dict:
            import backlot.brain_api as brain_api
            from backlot.brain_api import BrainApiError
            project_dir = _safe_project_dir(project_id)
            body = await _guarded_json_body(request)
            _enforce_rate(request, "brain")
            handler = getattr(brain_api, handler_name)
            try:
                return await asyncio.to_thread(handler, project_dir, body)
            except BrainApiError as exc:
                raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))
        _route.__name__ = f"project_brain_{handler_name}"
        return _route

    app.post("/api/project/{project_id}/brain/start")(_brain_control("start_run"))
    app.post("/api/project/{project_id}/brain/approve")(_brain_control("grant_approval"))
    app.post("/api/project/{project_id}/brain/reject")(_brain_control("reject_approval"))
    app.post("/api/project/{project_id}/brain/cancel")(_brain_control("cancel_run"))
    app.post("/api/project/{project_id}/brain/retry")(_brain_control("retry_stage"))
    app.post("/api/project/{project_id}/brain/resume")(_brain_control("resume_run"))

    # ---- Canonical production status (ONE view model: board + studio share it) --
    # Reconciles brain + coarse run + checkpoints + timeline + Hermes connection
    # into a single command-center view so the two surfaces never disagree.

    @app.get("/api/project/{project_id}/status")
    async def project_status(project_id: str, demo: int = 0, stale: int = 0) -> dict:
        from backlot.status_api import build_status_payload
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(
            build_status_payload, project_dir, demo=bool(demo), stale=bool(stale))

    # ---- Hermes / Mochlet connection (guided, secure) ------------------------

    @app.get("/api/hermes/connection")
    async def hermes_connection_get() -> dict:
        from backlot.status_api import hermes_connection
        return await asyncio.to_thread(hermes_connection)

    @app.post("/api/hermes/connect")
    async def hermes_connect_post(request: Request) -> dict:
        from backlot.status_api import StatusApiError, hermes_connect
        body = await _guarded_json_body(request)
        _enforce_rate(request, "hermes")
        try:
            return await asyncio.to_thread(hermes_connect, body)
        except StatusApiError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.post("/api/hermes/disconnect")
    async def hermes_disconnect_post(request: Request) -> dict:
        from backlot.status_api import hermes_disconnect
        body = await _guarded_json_body(request)
        _enforce_rate(request, "hermes")
        return await asyncio.to_thread(hermes_disconnect, body)

    # ---- Learned style preferences (visible, auditable, reversible) ----------

    @app.get("/api/project/{project_id}/preferences")
    async def project_preferences_get(project_id: str, scope: str = "all",
                                      category: Optional[str] = None) -> dict:
        from backlot.brain_api import read_preferences
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_preferences, project_dir, scope=scope, category=category)

    @app.post("/api/project/{project_id}/preferences")
    async def project_preferences_post(project_id: str, request: Request) -> dict:
        from backlot.brain_api import BrainApiError, update_preference
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "preferences")
        try:
            return await asyncio.to_thread(update_preference, body, project_dir=project_dir)
        except BrainApiError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.post("/api/project/{project_id}/preferences/reset")
    async def project_preferences_reset(project_id: str, request: Request) -> dict:
        from backlot.brain_api import reset_preferences
        project_dir = _safe_project_dir(project_id)
        body = await _guarded_json_body(request)
        _enforce_rate(request, "preferences")
        return await asyncio.to_thread(reset_preferences, body, project_dir=project_dir)

    @app.get("/api/preferences")
    async def preferences_get(scope: str = "global", category: Optional[str] = None) -> dict:
        """Global learned-style preferences (cross-project defaults)."""
        from backlot.brain_api import read_preferences
        return await asyncio.to_thread(read_preferences, None,
                                      scope="global" if scope != "project" else "global",
                                      category=category)

    @app.post("/api/preferences")
    async def preferences_post(request: Request) -> dict:
        from backlot.brain_api import BrainApiError, update_preference
        body = await _guarded_json_body(request)
        _enforce_rate(request, "preferences")
        try:
            return await asyncio.to_thread(update_preference, body, project_dir=None)
        except BrainApiError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.get("/api/project/{project_id}/events")
    async def project_events(project_id: str, request: Request) -> StreamingResponse:
        _safe_project_dir(project_id)  # 404 early for unknown projects

        async def stream():
            q = hub.subscribe(project_id)
            try:
                yield _sse({"type": "hello", "project_id": project_id})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    # Coalesce bursts: drain anything else queued.
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": project_id})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/library/events")
    async def library_events(request: Request) -> StreamingResponse:
        async def stream():
            q = hub.subscribe()
            try:
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        changed = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": changed})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # ---- Thumbnails (downscaled, cached on disk) ------------------------

    @app.get("/thumb/{project_id}/{file_path:path}")
    async def thumb(project_id: str, file_path: str, w: int = 640) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        width = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
        cached = await asyncio.to_thread(_thumbnail_for, target, width)
        if cached is None:
            # Never fall back to raw video bytes for an <img> consumer (F-03);
            # non-thumbable images are safe to serve as-is.
            if target.suffix.lower() in {".mp4", ".webm", ".mov"}:
                raise HTTPException(status_code=404, detail="no poster frame available")
            return FileResponse(target)
        return FileResponse(cached, media_type="image/jpeg")

    # ---- Media (range requests handled by FileResponse) ---------------

    @app.get("/media/{project_id}/{file_path:path}")
    async def media(project_id: str, file_path: str) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(target)

    # ---- UI ------------------------------------------------------------

    @app.get("/p/{project_id}/editor")
    async def editor_page(project_id: str) -> FileResponse:
        return FileResponse(UI_DIR / "editor.html")

    @app.get("/p/{project_id}")
    async def board_page(project_id: str) -> FileResponse:
        return FileResponse(UI_DIR / "board.html")

    @app.get("/p/{project_path:path}")
    async def board_page_path(project_path: str) -> FileResponse:
        return FileResponse(UI_DIR / "board.html")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(UI_DIR / "settings.html")

    @app.get("/favicon.ico")
    async def favicon():
        from fastapi.responses import Response
        return Response(status_code=204)  # no icon — silence the console 404

    @app.get("/")
    async def library_page() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    return app


async def _guarded_json_body(request: Request, *, max_bytes: int = _MAX_MUTATION_BYTES) -> dict:
    """Validate a state-changing JSON request and return its parsed body.

    Enforces (all with generic, sanitized errors):
      * CSRF: the process-scoped token in the ``X-OpenMontage-CSRF`` header.
      * Same-origin: if an Origin/Referer is present its host must match Host.
      * Content-Type: application/json.
      * Size bound: Content-Length (and the read body) within ``max_bytes``.
    """
    # --- CSRF token (constant-time compare) ---
    token = request.headers.get(_CSRF_HEADER, "")
    if not token or not secrets.compare_digest(token, _CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")

    # --- Same-origin (Origin/Referer host must equal Host when present) ---
    host = (request.headers.get("host") or "").split(",")[0].strip().lower()
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        from urllib.parse import urlparse
        oh = (urlparse(origin).netloc or "").lower()
        if host and oh and oh != host:
            raise HTTPException(status_code=403, detail="cross-origin request rejected")

    # --- Content-Type ---
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="content-type must be application/json")

    # --- Size bound ---
    clen = request.headers.get("content-length")
    if clen is not None:
        try:
            if int(clen) > max_bytes:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content-length")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return body


def _safe_project_dir(project_id: str) -> Path:
    # ':' rejects Windows drive-relative ids like "C:" (PROJECTS_DIR / "C:"
    # collapses back to PROJECTS_DIR itself).
    if any(c in project_id for c in "/\\:") or project_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid project id")
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return project_dir


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _thumbnail_for(source: Path, width: int) -> Optional[Path]:
    """Downscale an image (or extract a video poster frame) to a cached JPEG."""
    suffix = source.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    is_video = suffix in {".mp4", ".webm", ".mov"}
    if not (is_image or is_video):
        return None
    try:
        import hashlib
        stat = source.stat()
        key = hashlib.sha1(
            f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()
        ).hexdigest()[:20]
        cached = THUMB_CACHE_DIR / f"{key}.jpg"
        if cached.is_file():
            return cached
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Unique temp per request — concurrent misses for the same source
        # must not write (and replace from) the same temp file.
        import uuid
        tmp = THUMB_CACHE_DIR / f"{key}.{uuid.uuid4().hex[:8]}.tmp.jpg"
        if is_video:
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5",
                 "-i", str(source), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", str(tmp)],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0 or not tmp.is_file():
                return None
        else:
            from PIL import Image
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((width, width * 3))
                img.save(tmp, "JPEG", quality=82)
        tmp.replace(cached)
        return cached
    except Exception:
        return None


app = create_app()
