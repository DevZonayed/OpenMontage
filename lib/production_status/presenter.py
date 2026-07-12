"""Pure reconciliation of every production-state source into ONE view model.

OpenMontage has three independent state systems that used to be shown to the user
as separate, often-contradictory truths:

  * the **production brain** (event-sourced, 11 canonical stages) — ``brain``;
  * the **coarse run controller** (``run.json`` — a local preflight/planner worker
    lifecycle) — ``run``;
  * the **checkpoint pipeline** (per-stage ``checkpoint_*.json`` files, whose stage
    names come from a pipeline manifest and can be the legacy 8/9-stage vocabulary)
    — reached here through the already-derived ``board`` state.

This module folds all of them into a single, plain-language *command-center* view
model. It is PURE (no I/O): callers load the source dicts and pass them in, which
makes every reconciliation rule unit-testable in isolation.

Design guarantees enforced here (and covered by tests):

  * ONE canonical 11-stage vocabulary; legacy stage names are mapped, never shown
    raw alongside the canonical ones.
  * Exactly ONE ``primary_action``; everything else is ``secondary_actions``.
  * ``stop_available`` is True only when a REAL run is active/cancelling — never
    when a plan is merely approved or queued.
  * ``render.renderable`` is True only when the timeline actually has layers.
  * Contradictory sources produce an explicit ``overall_state == "reconciling"``
    plus a ``diagnostics`` entry — never two conflicting badges at once.
  * ``mode == "demo"`` (or ``"fixture"``) is only ever set from an explicit
    request / a genuinely fake-driven run — never as an automatic fallback.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.production_brain import schema as S

# --------------------------------------------------------------------------- #
# Canonical vocabulary
# --------------------------------------------------------------------------- #
# The single source of truth is the brain's stage machine. We re-export it so the
# board and studio share one ordered, human-labelled 11-stage rail.
CANONICAL_STAGES: tuple[str, ...] = S.STAGES
CANONICAL_STAGE_COUNT: int = len(CANONICAL_STAGES)
CANONICAL_STAGE_TITLES: dict[str, str] = dict(S.STAGE_TITLES)
_STAGE_INDEX: dict[str, int] = {name: i for i, name in enumerate(CANONICAL_STAGES)}

# Legacy / pipeline-manifest stage names → canonical stage. The checkpoint board
# uses these (research…publish, plus ``idea``); the brain uses the 11 above. A
# name already canonical maps to itself.
LEGACY_STAGE_MAP: dict[str, str] = {
    "research": "research",
    "idea": "proposal",
    "proposal": "proposal",
    "script": "script",
    "scene_plan": "scene_plan",
    "storyboard": "scene_plan",
    "assets": "assets",
    "asset": "assets",
    "narration": "narration",
    "audio": "narration",
    "voiceover": "narration",
    "edit": "edit",
    "editing": "edit",
    "compose": "render",
    "composition": "render",
    "render": "render",
    "review": "review",
    "qa": "review",
    "approval": "approval",
    "publish": "approval",
    "delivery": "approval",
    "complete": "complete",
    "completed": "complete",
    "done": "complete",
}

OVERALL_STATES: frozenset[str] = frozenset(
    {
        "not_started",
        "planning",
        "awaiting_plan_approval",
        "ready_to_produce",
        "producing",
        "awaiting_approval",
        "blocked",
        "cancelling",
        "cancelled",
        "failed",
        "completed",
        "reconciling",
    }
)

# Overall states in which a real run is genuinely in flight (so Stop is offered).
_ACTIVE_OVERALL: frozenset[str] = frozenset(
    {"producing", "awaiting_approval", "blocked", "cancelling", "planning"}
)

# Coarse run.json states.
_RUN_ACTIVE = frozenset({"starting", "running", "cancelling"})
_RUN_WAITING = "waiting_for_approval"

# Owners.
OWNER_HERMES = "hermes"
OWNER_USER = "user"
OWNER_SYSTEM = "system"


def canonical_stage(name: Optional[str]) -> Optional[str]:
    """Map any stage name (canonical, legacy, or pipeline-specific) → canonical."""
    if not name or not isinstance(name, str):
        return None
    key = name.strip().lower()
    if key in _STAGE_INDEX:
        return key
    return LEGACY_STAGE_MAP.get(key)


def canonical_stage_index(name: Optional[str]) -> Optional[int]:
    c = canonical_stage(name)
    return _STAGE_INDEX.get(c) if c else None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_active(run: Optional[dict]) -> bool:
    return bool(run) and run.get("state") in _RUN_ACTIVE


def _brain_has_run(brain: Optional[dict]) -> bool:
    return bool(brain) and bool(brain.get("run_id")) and brain.get("state") != "not_started"


def _checkpoint_progress(board: Optional[dict]) -> dict[str, Any]:
    """Fold the checkpoint rail into canonical progress.

    Returns ``{completed: set[str], current: Optional[str], statuses: dict,
    any: bool, failed: Optional[str], awaiting: Optional[str]}`` where keys are
    canonical stage ids.
    """
    completed: set[str] = set()
    statuses: dict[str, str] = {}
    failed: Optional[str] = None
    awaiting: Optional[str] = None
    current: Optional[str] = None
    stages = (board or {}).get("stages") or []
    seen_any = False
    for entry in stages:
        if not isinstance(entry, dict):
            continue
        canon = canonical_stage(entry.get("name"))
        if not canon:
            continue
        status = entry.get("status") or "pending"
        if status in ("completed", "in_progress", "awaiting_human", "failed"):
            seen_any = True
        # A later checkpoint for the same canonical stage wins the more-advanced status.
        prev = statuses.get(canon)
        rank = {"pending": 0, "unknown": 0, "in_progress": 2, "awaiting_human": 3,
                "failed": 3, "completed": 4}
        if prev is None or rank.get(status, 1) >= rank.get(prev, 1):
            statuses[canon] = status
    for canon, status in statuses.items():
        if status == "completed":
            completed.add(canon)
        elif status == "failed" and failed is None:
            failed = canon
        elif status == "awaiting_human" and awaiting is None:
            awaiting = canon
    # Current = first canonical stage that is not completed but has some activity,
    # else the stage right after the furthest completed one.
    for canon in ("in_progress", "awaiting_human"):
        for name, status in statuses.items():
            if status == ("in_progress" if canon == "in_progress" else "awaiting_human"):
                current = name
                break
        if current:
            break
    if current is None and completed:
        furthest = max(completed, key=lambda c: _STAGE_INDEX[c])
        nxt = _STAGE_INDEX[furthest] + 1
        if nxt < CANONICAL_STAGE_COUNT:
            current = CANONICAL_STAGES[nxt]
    return {
        "completed": completed,
        "current": current,
        "statuses": statuses,
        "any": seen_any,
        "failed": failed,
        "awaiting": awaiting,
    }


def _fmt_stage(name: Optional[str]) -> str:
    c = canonical_stage(name)
    return CANONICAL_STAGE_TITLES.get(c, (name or "production").replace("_", " ").title()) if c else (
        (name or "production").replace("_", " ").title())


def _brain_mode(brain: dict) -> str:
    """live for a real external job, fixture for the fake driver."""
    orchestration = ((brain.get("brain") or {}).get("orchestration")) or "external_job"
    return "live" if orchestration == "external_job" else "fixture"


def _latest_event(brain: dict, board: Optional[dict]) -> Optional[dict]:
    activity = brain.get("activity")
    updated = brain.get("updated_at")
    if _brain_has_run(brain) and activity:
        return {"label": activity, "ts": updated, "seq": brain.get("cursor")}
    events = (board or {}).get("events") or []
    if events:
        last = events[-1]
        label = last.get("message") or last.get("event") or last.get("type")
        return {"label": label, "ts": last.get("ts") or last.get("timestamp"), "seq": last.get("seq")}
    if activity:
        return {"label": activity, "ts": updated, "seq": brain.get("cursor")}
    return None


def _timeline_layers(timeline: Optional[dict]) -> int:
    if not timeline:
        return 0
    n = timeline.get("layer_count")
    if isinstance(n, int):
        return n
    layers = timeline.get("layers")
    if isinstance(layers, list):
        return len(layers)
    tracks = timeline.get("tracks")
    if isinstance(tracks, list):
        total = 0
        for t in tracks:
            ls = (t or {}).get("layers") if isinstance(t, dict) else None
            if isinstance(ls, list):
                total += len(ls)
        return total
    return 0


# --------------------------------------------------------------------------- #
# Stepper
# --------------------------------------------------------------------------- #
def _build_stepper(
    *,
    brain: dict,
    checkpoints: dict,
    authoritative: str,
    current_stage: Optional[str],
) -> list[dict]:
    """One entry per canonical stage with a display status.

    ``authoritative`` is ``"brain"`` or ``"checkpoints"``.
    Display statuses: completed | current | blocked | awaiting | failed |
    skipped | upcoming.
    """
    current_idx = _STAGE_INDEX.get(canonical_stage(current_stage)) if current_stage else None
    brain_by_id = {s.get("id"): s for s in (brain.get("stages") or []) if isinstance(s, dict)}
    ck_status = checkpoints.get("statuses", {})
    ck_completed = checkpoints.get("completed", set())

    rail: list[dict] = []
    for idx, sid in enumerate(CANONICAL_STAGES):
        title = CANONICAL_STAGE_TITLES[sid]
        status = "upcoming"
        detail = None
        progress = 0.0
        if authoritative == "brain":
            bs = brain_by_id.get(sid) or {}
            bstatus = bs.get("status") or "pending"
            progress = float(bs.get("progress") or 0.0)
            detail = bs.get("latest_activity")
            status = {
                "done": "completed",
                "active": "current",
                "blocked": "blocked",
                "awaiting_approval": "awaiting",
                "failed": "failed",
                "skipped": "skipped",
                "pending": "upcoming",
            }.get(bstatus, "upcoming")
            if status == "upcoming" and current_idx is not None and idx < current_idx:
                # brain hasn't touched it but we've moved past — treat as done.
                status = "completed"
        else:  # checkpoints authoritative
            cstatus = ck_status.get(sid)
            if sid in ck_completed:
                status = "completed"
                progress = 1.0
            elif cstatus == "in_progress":
                status = "current"
            elif cstatus == "awaiting_human":
                status = "awaiting"
            elif cstatus == "failed":
                status = "failed"
            elif current_idx is not None and idx == current_idx:
                status = "current"
            elif current_idx is not None and idx < current_idx:
                status = "completed"
                progress = 1.0
        # The single "current" marker always wins for the reconciled current stage.
        if current_idx is not None and idx == current_idx and status in ("upcoming", "current"):
            status = "current"
        rail.append({
            "id": sid,
            "index": idx,
            "label": title,
            "status": status,
            "progress": round(progress, 3),
            "detail": detail,
        })
    return rail


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_status_view(
    *,
    brain: Optional[dict] = None,
    board: Optional[dict] = None,
    run: Optional[dict] = None,
    inbox: Optional[dict] = None,
    timeline: Optional[dict] = None,
    connection: Optional[dict] = None,
    demo: bool = False,
    stale: bool = False,
) -> dict:
    """Reconcile every source into the canonical command-center view model.

    All inputs are optional and defensively defaulted, so a brand-new or partly
    written project degrades to a clear ``not_started`` view rather than raising.
    """
    brain = brain or S.empty_state((board or {}).get("project_id") or "")
    board = board or {}
    connection = connection or {"status": "unknown", "available": False}
    checkpoints = _checkpoint_progress(board)
    brain_run = _brain_has_run(brain)

    diagnostics: list[dict] = []
    secondary: list[dict] = []

    # --- pick the authoritative source + overall_state + current stage -------
    if brain_run:
        authoritative = "brain"
        mode = "demo" if demo else _brain_mode(brain)
        bstate = brain.get("state")
        overall = {
            "running": "producing",
            "awaiting_approval": "awaiting_approval",
            "blocked": "blocked",
            "cancelling": "cancelling",
            "cancelled": "cancelled",
            "failed": "failed",
            "completed": "completed",
        }.get(bstate, "producing")
        current_stage = canonical_stage(brain.get("current_stage")) or (
            None if overall in ("completed", "cancelled", "failed") else CANONICAL_STAGES[0])
        # Conflict: brain says finished, but the coarse worker still claims active.
        if overall in ("completed",) and _run_active(run):
            overall = "reconciling"
            diagnostics.append({
                "kind": "source_conflict",
                "message": "The production brain reports the run finished, but a local "
                           "worker still reports it running. Showing a reconciliation "
                           "state until they agree.",
                "sources": {"brain": bstate, "run": run.get("state")},
            })
    else:
        authoritative = "checkpoints"
        run_state = (run or {}).get("state") or "not_started"
        plan_approved = bool((run or {}).get("plan_approved"))
        mode = "demo" if demo else ("local" if (checkpoints["any"] or run_state != "not_started") else "idle")
        if run_state == _RUN_WAITING and plan_approved:
            overall = "ready_to_produce"
            # The approved plan already covers research→scene_plan (planning); the
            # next thing Hermes *produces* is asset generation, so the command
            # center points at the first incomplete PRODUCTION stage (assets
            # onward), not an intermediate planning checkpoint.
            current_stage = (_first_incomplete_production(checkpoints)
                             or checkpoints["current"] or PRODUCTION_START)
        elif run_state == _RUN_WAITING:
            overall = "awaiting_plan_approval"
            current_stage = "proposal"
        elif run_state == "cancelling":
            # Checked before _RUN_ACTIVE (which also contains "cancelling") so a
            # coarse cancellation is never mislabeled as "planning".
            overall = "cancelling"
            current_stage = checkpoints["current"]
        elif run_state in _RUN_ACTIVE:
            overall = "planning"
            current_stage = checkpoints["current"] or "research"
        elif run_state == "cancelled":
            overall = "cancelled"
            current_stage = None
        elif run_state == "failed":
            overall = "failed"
            current_stage = checkpoints["failed"] or checkpoints["current"]
        elif run_state == "completed":
            overall = "completed"
            current_stage = None
        elif checkpoints["failed"]:
            overall = "blocked"
            current_stage = checkpoints["failed"]
        elif checkpoints["awaiting"]:
            overall = "awaiting_plan_approval"
            current_stage = checkpoints["awaiting"]
        elif checkpoints["any"]:
            overall = "ready_to_produce" if checkpoints["completed"] else "planning"
            current_stage = checkpoints["current"] or _first_incomplete(checkpoints)
        else:
            overall = "not_started"
            current_stage = None

    stage_index = _STAGE_INDEX.get(current_stage) if current_stage else None
    stepper = _build_stepper(
        brain=brain, checkpoints=checkpoints, authoritative=authoritative,
        current_stage=current_stage)
    completed_count = sum(1 for s in stepper if s["status"] == "completed")
    progress = round(completed_count / CANONICAL_STAGE_COUNT, 3)

    # --- identity / telemetry (only ever from a real brain run) --------------
    brain_id = brain.get("brain") or {}
    identity = {
        "agent": brain_id.get("agent_id") if brain_run else None,
        "job": brain_id.get("job_id") if brain_run else None,
        "session": brain_id.get("session_id") if brain_run else None,
        "engine": brain_id.get("engine") if brain_run else None,
        "tool": None,
        "provider": None,
    }
    if brain_run and stage_index is not None:
        cur = next((s for s in (brain.get("stages") or []) if s.get("id") == current_stage), None)
        if cur:
            identity["tool"] = cur.get("tool")
            identity["provider"] = cur.get("provider")

    # --- elapsed on the current stage ----------------------------------------
    elapsed = None
    if brain_run and current_stage:
        cur = next((s for s in (brain.get("stages") or []) if s.get("id") == current_stage), None)
        if cur:
            elapsed = cur.get("elapsed_seconds")

    latest_event = _latest_event(brain, board)

    # --- headline / active task / owner / why_waiting / actions --------------
    narrative = _narrative(
        overall=overall, current_stage=current_stage, stage_index=stage_index,
        brain=brain, brain_run=brain_run, board=board, run=run, inbox=inbox,
        connection=connection, mode=mode)
    secondary.extend(narrative["secondary"])
    if narrative.get("diagnostic"):
        diagnostics.append(narrative["diagnostic"])

    # --- stop availability (never on a merely-approved / idle run) -----------
    stop_available = overall in _ACTIVE_OVERALL and (brain_run or _run_active(run) or overall == "cancelling")

    # --- render block (never renderable without layers) ----------------------
    render = _render_block(
        overall=overall, brain=brain, brain_run=brain_run, run=run,
        timeline=timeline, current_stage=current_stage)

    # --- staleness (client passed last-known-live-with-network-error) --------
    if stale:
        diagnostics.append({
            "kind": "stale",
            "message": "Showing the last known state — reconnecting to live updates…",
        })

    return {
        "version": "1.0",
        "kind": "production_status_view",
        "project_id": brain.get("project_id") or board.get("project_id"),
        "mode": mode,
        "authoritative_source": authoritative,
        "overall_state": overall,
        "current_stage": current_stage,
        "current_stage_label": _fmt_stage(current_stage) if current_stage else None,
        "stage_index": stage_index,
        "stage_number": (stage_index + 1) if stage_index is not None else None,
        "stage_count": CANONICAL_STAGE_COUNT,
        "headline": narrative["headline"],
        "active_task": narrative["active_task"],
        "owner": narrative["owner"],
        "why_waiting": narrative["why_waiting"],
        "primary_action": narrative["primary"],
        "secondary_actions": secondary,
        "latest_event": latest_event,
        "elapsed_seconds": elapsed,
        "progress": progress,
        "completed_stages": completed_count,
        "stages": stepper,
        "identity": identity,
        "run_id": brain.get("run_id") if brain_run else (run or {}).get("run_id"),
        "stop_available": stop_available,
        "render": render,
        "connection": connection,
        "diagnostics": diagnostics,
        "sources": {
            "brain_state": brain.get("state"),
            "brain_run_id": brain.get("run_id"),
            "run_state": (run or {}).get("state"),
            "plan_approved": bool((run or {}).get("plan_approved")),
            "has_checkpoints": checkpoints["any"],
        },
        "stale": bool(stale),
        "is_demo": mode == "demo",
        "is_live": mode == "live",
        "is_fixture": mode == "fixture",
    }


def _first_incomplete(checkpoints: dict) -> Optional[str]:
    for sid in CANONICAL_STAGES:
        if sid not in checkpoints["completed"]:
            return sid
    return None


# Production (asset-generating) work begins at ``assets``; research→scene_plan are
# the planning stages an approved plan already covers.
PRODUCTION_START = "assets"
_PRODUCTION_START_INDEX = _STAGE_INDEX[PRODUCTION_START]


def _first_incomplete_production(checkpoints: dict) -> Optional[str]:
    """First canonical stage at/after ``assets`` that isn't completed."""
    for idx in range(_PRODUCTION_START_INDEX, CANONICAL_STAGE_COUNT):
        sid = CANONICAL_STAGES[idx]
        if sid not in checkpoints["completed"]:
            return sid
    return None


# --------------------------------------------------------------------------- #
# Narrative (headline / owner / single primary action)
# --------------------------------------------------------------------------- #
def _narrative(
    *, overall, current_stage, stage_index, brain, brain_run, board, run, inbox,
    connection, mode,
) -> dict:
    stage_label = _fmt_stage(current_stage) if current_stage else None
    activity = brain.get("activity") if brain_run else None
    conn_ok = bool(connection.get("available"))
    secondary: list[dict] = []
    diagnostic = None

    def action(aid, label, owner, *, kind="control", advances=True, **extra):
        return {"id": aid, "label": label, "owner": owner, "kind": kind,
                "advances_production": advances, **extra}

    preview_secondary = action(
        "preview", "Preview approved plan locally", OWNER_USER,
        kind="preview", advances=False,
        hint="A local animatic — it does not advance the real production.")

    if overall == "not_started":
        if conn_ok:
            primary = action("start", "Start production with Hermes", OWNER_HERMES, kind="start")
            headline = "Ready to start production"
            active_task = "No production run yet."
            owner = OWNER_HERMES
            why = "Nothing has started. Hermes will run research → proposal when you begin."
        else:
            primary = action("connect_hermes", "Connect Hermes", OWNER_USER, kind="connect")
            headline = "Connect Hermes to begin"
            active_task = "Hermes isn't connected yet."
            owner = OWNER_USER
            why = connection.get("headline") or "Hermes production isn't connected on this machine yet."
        return {"headline": headline, "active_task": active_task, "owner": owner,
                "why_waiting": why, "primary": primary, "secondary": secondary,
                "diagnostic": diagnostic}

    if overall == "awaiting_plan_approval":
        # Distinguish the coarse-run PLAN gate (an approvable run.json waiting for
        # go-ahead) from a mid-pipeline checkpoint gate captured as ``awaiting_human``
        # (the agent paused in chat — there is no run to POST-approve, so a
        # clickable approve would 400 on a null run_id).
        coarse_plan_gate = (run or {}).get("state") == _RUN_WAITING
        if coarse_plan_gate:
            primary = action("approve_plan", "Review & approve the plan", OWNER_USER, kind="approve")
            secondary.append(action("request_changes", "Request changes", OWNER_USER,
                                    kind="reject", advances=False))
            return {
                "headline": "Your plan is ready to review",
                "active_task": f"Awaiting your approval on the {stage_label or 'plan'}.",
                "owner": OWNER_USER,
                "why_waiting": "Hermes has proposed a plan and is waiting for your go-ahead "
                               "before producing assets.",
                "primary": primary, "secondary": secondary, "diagnostic": diagnostic}
        # Mid-pipeline review gate — passive, no broken POST.
        primary = action("review_in_chat", f"Review {stage_label or 'this stage'} to continue",
                         OWNER_USER, kind="status", advances=False)
        return {
            "headline": f"Waiting for your review — {stage_label or 'stage'}",
            "active_task": "The agent paused at this gate for your review.",
            "owner": OWNER_USER,
            "why_waiting": "Reply in chat to approve this stage or request changes.",
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "ready_to_produce":
        if conn_ok:
            primary = action("continue_hermes", "Continue production with Hermes",
                             OWNER_HERMES, kind="start")
            why = ("The plan is approved. Hermes hasn't begun producing "
                   f"{stage_label.lower() if stage_label else 'the next stage'} yet.")
        else:
            primary = action("connect_hermes", "Connect Hermes to continue", OWNER_USER,
                             kind="connect")
            why = (connection.get("headline")
                   or "The plan is approved, but Hermes isn't connected to continue production.")
        secondary.append(preview_secondary)
        return {
            "headline": f"Waiting for Hermes to begin {stage_label.lower() if stage_label else 'production'}",
            "active_task": "Plan approved — production has not started yet.",
            "owner": OWNER_HERMES if conn_ok else OWNER_USER,
            "why_waiting": why,
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "producing":
        primary = action("monitor", f"Hermes is producing {stage_label.lower() if stage_label else 'your video'}",
                         OWNER_HERMES, kind="status", advances=False)
        return {
            "headline": f"Hermes is working on {stage_label or 'production'}",
            "active_task": activity or f"Producing {stage_label or 'the video'}.",
            "owner": OWNER_HERMES,
            "why_waiting": None,
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "awaiting_approval":
        pending = next((a for a in (brain.get("approvals") or [])
                        if a.get("status") == "pending"), None)
        prompt = (pending or {}).get("prompt") or f"Approve {stage_label or 'this stage'}?"
        primary = action("approve", f"Approve {stage_label or 'stage'}", OWNER_USER,
                         kind="approve", approval_id=(pending or {}).get("approval_id"),
                         stage=(pending or {}).get("stage"))
        secondary.append(action("reject", "Reject", OWNER_USER, kind="reject", advances=False,
                                approval_id=(pending or {}).get("approval_id"),
                                stage=(pending or {}).get("stage")))
        return {
            "headline": f"Hermes needs your approval — {stage_label or 'stage'}",
            "active_task": prompt,
            "owner": OWNER_USER,
            "why_waiting": "Hermes paused here for your review before continuing.",
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "blocked":
        blocker = next((b for b in (brain.get("blockers") or []) if not b.get("resolved")), None)
        msg = (blocker or {}).get("message") or "Production is blocked."
        kind = (blocker or {}).get("kind")
        # Route the single primary action from the blocker kind.
        if kind == "brain_unavailable" or not bool(connection.get("available")):
            primary = action("connect_hermes", "Connect Hermes", OWNER_USER, kind="connect")
            owner = OWNER_USER
        elif kind == "control_unconfirmed":
            primary = action("retry_control", "Retry", OWNER_USER, kind="retry",
                             stage=(blocker or {}).get("stage"))
            owner = OWNER_USER
        else:
            primary = action("retry_stage", f"Retry {stage_label or 'stage'}", OWNER_USER,
                             kind="retry", stage=(blocker or {}).get("stage"))
            owner = OWNER_USER
        return {
            "headline": f"Production is blocked — {stage_label or 'stage'}",
            "active_task": msg,
            "owner": owner,
            "why_waiting": msg,
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "cancelling":
        primary = action("cancelling", "Cancelling production…", OWNER_SYSTEM,
                         kind="status", advances=False)
        return {
            "headline": "Cancelling production",
            "active_task": activity or "Waiting for the run to stop.",
            "owner": OWNER_SYSTEM, "why_waiting": "A cancellation is in progress.",
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "cancelled":
        primary = action("restart", "Start a new production", OWNER_HERMES, kind="start")
        return {
            "headline": "Production cancelled",
            "active_task": "The run was cancelled. Completed work is preserved.",
            "owner": OWNER_USER, "why_waiting": None,
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "failed":
        primary = action("retry_stage", f"Retry {stage_label or 'production'}", OWNER_USER,
                         kind="retry", stage=current_stage)
        err = brain.get("error") if brain_run else (run or {}).get("error")
        return {
            "headline": f"Production failed — {stage_label or 'run'}",
            "active_task": err or "The run failed.",
            "owner": OWNER_USER, "why_waiting": err,
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "completed":
        primary = action("view_deliverable", "View the finished video", OWNER_USER,
                         kind="deliverable", advances=False)
        return {
            "headline": "Production complete",
            "active_task": "Your video is ready.",
            "owner": OWNER_USER, "why_waiting": None,
            "primary": primary, "secondary": secondary, "diagnostic": diagnostic}

    if overall == "reconciling":
        primary = action("refresh", "Refresh status", OWNER_SYSTEM, kind="status", advances=False)
        return {
            "headline": "Reconciling production state",
            "active_task": "Two sources disagree about the run — resolving.",
            "owner": OWNER_SYSTEM,
            "why_waiting": "The board is waiting for the state sources to agree.",
            "primary": primary, "secondary": secondary,
            "diagnostic": None}

    # planning (coarse worker doing preflight before the first artifact)
    primary = action("planning", "Hermes is preparing your production", OWNER_HERMES,
                     kind="status", advances=False)
    return {
        "headline": "Hermes is getting started",
        "active_task": (run or {}).get("activity") or activity or "Running preflight and planning.",
        "owner": OWNER_HERMES, "why_waiting": None,
        "primary": primary, "secondary": secondary, "diagnostic": diagnostic}


# --------------------------------------------------------------------------- #
# Render block
# --------------------------------------------------------------------------- #
def _render_block(*, overall, brain, brain_run, run, timeline, current_stage) -> dict:
    layers = _timeline_layers(timeline)
    renderable = layers > 0
    # Is a real render/preview actually in flight right now?
    active = False
    if brain_run:
        cur = next((s for s in (brain.get("stages") or [])
                    if s.get("id") == "render"), None)
        if cur and cur.get("status") == "active":
            active = True
    if run and (run.get("preview") or {}).get("state") in ("rendering", "running"):
        active = True
    if timeline and timeline.get("rendering"):
        active = True

    if renderable:
        reason = None
    elif overall in ("not_started", "planning"):
        reason = "Hermes hasn't built the timeline yet — nothing to render."
    elif overall in ("awaiting_plan_approval", "ready_to_produce"):
        reason = "The plan is approved but no assets exist yet. Render unlocks once Hermes builds the timeline."
    else:
        reason = "No renderable layers on the timeline yet."
    return {
        "renderable": renderable,
        "active": active,
        "reason": reason,
        "layer_count": layers,
    }
