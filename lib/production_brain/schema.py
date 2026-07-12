"""Canonical production-run vocabulary, transition rules, redaction, reducer.

This module is pure (no I/O): the state machine, the secret-redactor, and the
event→state reducer live here so they can be unit-tested in isolation and reused
by both the store and the API layer.
"""

from __future__ import annotations

import re
from typing import Any, Optional

SCHEMA_VERSION = "1.0"

# ---- Stages (the explicit, ordered production stages the brain drives) ------
# research → proposal → script → scene_plan → assets → narration → edit →
# render → review → approval → complete
STAGES: tuple[str, ...] = (
    "research",
    "proposal",
    "script",
    "scene_plan",
    "assets",
    "narration",
    "edit",
    "render",
    "review",
    "approval",
    "complete",
)

STAGE_TITLES: dict[str, str] = {
    "research": "Research",
    "proposal": "Proposal",
    "script": "Script",
    "scene_plan": "Scene planning",
    "assets": "Asset generation",
    "narration": "Narration & music",
    "edit": "Editing",
    "render": "Rendering",
    "review": "Validation & review",
    "approval": "Approval",
    "complete": "Completion",
}

STAGE_STATUSES: frozenset[str] = frozenset(
    {"pending", "active", "blocked", "awaiting_approval", "done", "failed", "skipped"}
)

# ---- Coarse run lifecycle ---------------------------------------------------
RUN_STATES: frozenset[str] = frozenset(
    {
        "not_started",
        "running",
        "awaiting_approval",
        "blocked",
        "cancelling",
        "cancelled",
        "failed",
        "completed",
    }
)
TERMINAL_RUN_STATES: frozenset[str] = frozenset({"cancelled", "failed", "completed"})
ACTIVE_RUN_STATES: frozenset[str] = frozenset(
    {"running", "awaiting_approval", "blocked", "cancelling"}
)

# Allowed coarse-state transitions. Used to reject invalid transitions loudly.
# The brain cancel is atomic (there is no worker to signal, unlike the coarse
# lib.production_run worker), so an active run may go straight to ``cancelled``.
# ``cancelling`` remains a valid intermediate for callers that want it.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "not_started": frozenset({"running"}),
    "running": frozenset({"awaiting_approval", "blocked", "cancelling", "cancelled", "failed", "completed"}),
    "awaiting_approval": frozenset({"running", "blocked", "cancelling", "cancelled", "failed", "completed"}),
    "blocked": frozenset({"running", "awaiting_approval", "cancelling", "cancelled", "failed", "completed"}),
    "cancelling": frozenset({"cancelled", "failed"}),
    "cancelled": frozenset(),
    "failed": frozenset(),
    "completed": frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    if src == dst:
        return True
    return dst in _TRANSITIONS.get(src, frozenset())


# ---- Event vocabulary -------------------------------------------------------
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",
        "stage_entered",
        "stage_progress",
        "tool_call",
        "provider_call",
        "decision",
        "output",
        "approval_requested",
        "approval_granted",
        "approval_rejected",
        "correction",
        "blocker_raised",
        "blocker_cleared",
        "stage_completed",
        "stage_failed",
        "stage_skipped",
        "retry",
        "resume",
        "heartbeat",
        "run_cancel_requested",
        "run_completed",
        "run_failed",
        "run_cancelled",
        "external_handle_updated",
        "note",
    }
)

BLOCKER_KINDS: frozenset[str] = frozenset(
    {"auth", "provider_access", "tool_bug", "quality", "runtime_unavailable",
     "brain_unavailable", "control_unconfirmed", "other"}
)


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #
# Key substrings whose VALUE must never be persisted in telemetry.
_SECRET_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_key",
    "private_key",
    "client_secret",
    "bearer",
    "credential",
    "cookie",
    "session_token",
)
_REDACTED = "[redacted]"
# Standalone value shapes that look like credentials even under an innocent key.
_SECRET_VALUE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}"          # OpenAI-style
    r"|AKIA[0-9A-Z]{12,}"              # AWS access key id
    r"|Bearer\s+[A-Za-z0-9._\-]{12,}"  # bearer header value
    r"|gh[pousr]_[A-Za-z0-9]{20,}"     # GitHub tokens
    r"|xox[baprs]-[A-Za-z0-9-]{10,})"  # Slack tokens
)


def _key_is_secret(key: str) -> bool:
    k = key.lower()
    return any(part in k for part in _SECRET_KEY_PARTS)


def _redact_value(value: Any, *, flag: list) -> Any:
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _key_is_secret(str(k)):
                flag.append(True)
                out[k] = _REDACTED
            else:
                out[k] = _redact_value(v, flag=flag)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, flag=flag) for v in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        flag.append(True)
        return _SECRET_VALUE_RE.sub(_REDACTED, value)
    return value


def redact_data(data: Any) -> tuple[Any, bool]:
    """Return (redacted_copy, was_redacted). Never raises."""
    flag: list = []
    try:
        out = _redact_value(data, flag=flag)
    except Exception:
        return {}, True
    return out, bool(flag)


def redact_event(event: dict) -> dict:
    """Redact secrets from an event in place-safe fashion, stamping `redacted`."""
    ev = dict(event)
    data = ev.get("data")
    if data is not None:
        red, was = redact_data(data)
        ev["data"] = red
        if was:
            ev["redacted"] = True
    # Never let a raw secret ride along in message/tool/provider fields.
    for k in ("message", "tool", "provider"):
        v = ev.get(k)
        if isinstance(v, str) and _SECRET_VALUE_RE.search(v):
            ev[k] = _SECRET_VALUE_RE.sub(_REDACTED, v)
            ev["redacted"] = True
    return ev


# --------------------------------------------------------------------------- #
# Materialized-state construction + reducer
# --------------------------------------------------------------------------- #
def default_stages() -> list[dict]:
    return [
        {
            "id": s,
            "title": STAGE_TITLES[s],
            "status": "pending",
            "progress": 0.0,
            "started_at": None,
            "ended_at": None,
            "elapsed_seconds": None,
            "tool": None,
            "provider": None,
            "job_id": None,
            "latest_event_seq": None,
            "latest_activity": None,
            "outputs": [],
            "error": None,
        }
        for s in STAGES
    ]


def empty_state(project_id: str) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "kind": "production_run_state",
        "run_id": None,
        "project_id": project_id,
        "state": "not_started",
        "terminal": False,
        "brain": {},
        "requested_duration_seconds": None,
        "actual_duration_seconds": None,
        "current_stage": None,
        "stages": default_stages(),
        "approvals": [],
        "blockers": [],
        "outputs": [],
        "error": None,
        "activity": "No production run has started for this project.",
        "counts": {"events": 0, "tool_calls": 0, "decisions": 0, "outputs": 0},
        "cursor": 0,
        "created_at": None,
        "started_at": None,
        "updated_at": None,
        "ended_at": None,
    }


def _stage(state: dict, stage_id: Optional[str]) -> Optional[dict]:
    if not stage_id:
        return None
    for s in state["stages"]:
        if s["id"] == stage_id:
            return s
    return None


def _parse_iso(ts: Optional[str]):
    if not ts:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _elapsed(started: Optional[str], ended: Optional[str]) -> Optional[float]:
    a, b = _parse_iso(started), _parse_iso(ended)
    if a is None or b is None:
        return None
    try:
        return max(0.0, (b - a).total_seconds())
    except Exception:
        return None


class InvalidTransition(ValueError):
    """A coarse-state transition the machine forbids."""


def reduce_event(state: dict, ev: dict, *, strict: bool = False) -> dict:
    """Fold one event into the materialized state. Pure — returns a NEW dict.

    When ``strict`` is True an illegal coarse-state transition raises
    :class:`InvalidTransition`; otherwise it is clamped (never silently corrupts
    a terminal run — terminal states are sticky).
    """
    etype = ev.get("type")
    ts = ev.get("ts")
    seq = ev.get("seq")
    stage_id = ev.get("stage")
    data = ev.get("data") or {}

    # ``run_started`` begins a fresh run *episode*. A log may hold several runs
    # (run_1 cancelled → run_2 started); replaying it must reset to the new run
    # rather than stay frozen on the prior terminal one. Handled before the
    # terminal guard so a completed run can be legitimately superseded.
    if etype == "run_started":
        fresh = empty_state(ev.get("project_id") or state.get("project_id"))
        fresh["cursor"] = int(seq or 0)
        fresh["counts"]["events"] = 1
        fresh["run_id"] = ev.get("run_id")
        fresh["created_at"] = ts
        fresh["started_at"] = ts
        fresh["updated_at"] = ts
        fresh["brain"] = data.get("brain") or {}
        rd = data.get("requested_duration_seconds")
        if rd is not None:
            fresh["requested_duration_seconds"] = rd
        fresh["state"] = "running"
        fresh["terminal"] = False
        fresh["current_stage"] = STAGES[0]
        fresh["activity"] = ev.get("message") or "Production run started."
        return fresh

    st = _deep_copy(state)
    st["cursor"] = max(int(st.get("cursor") or 0), int(seq or 0))
    st["updated_at"] = ts or st.get("updated_at")
    counts = st.setdefault("counts", {"events": 0, "tool_calls": 0, "decisions": 0, "outputs": 0})
    counts["events"] = int(counts.get("events") or 0) + 1

    # Terminal runs are frozen: only a heartbeat/note may touch updated_at.
    if st.get("terminal") and etype not in ("heartbeat", "note"):
        if strict:
            raise InvalidTransition(f"run is terminal ({st['state']}); cannot apply {etype}")
        return st

    def _set_state(dst: str) -> None:
        src = st.get("state") or "not_started"
        if not can_transition(src, dst):
            if strict:
                raise InvalidTransition(f"{src} → {dst} is not allowed")
            return
        st["state"] = dst
        if dst in TERMINAL_RUN_STATES:
            st["terminal"] = True
            st["ended_at"] = ts or st.get("ended_at")

    def _touch_stage(s: dict) -> None:
        s["latest_event_seq"] = seq
        if ev.get("message"):
            s["latest_activity"] = ev["message"]

    if etype == "stage_entered":
        s = _stage(st, stage_id)
        if s is not None:
            if s["status"] in ("pending", "blocked", "awaiting_approval", "failed"):
                s["status"] = "active"
            if not s.get("started_at"):
                s["started_at"] = ts
            _touch_stage(s)
            st["current_stage"] = stage_id
        _set_state("running")
        st["activity"] = ev.get("message") or f"Entered {STAGE_TITLES.get(stage_id, stage_id)}."

    elif etype == "stage_progress":
        s = _stage(st, stage_id)
        if s is not None:
            p = data.get("progress")
            if isinstance(p, (int, float)):
                s["progress"] = max(0.0, min(1.0, float(p)))
            if s["status"] == "pending":
                s["status"] = "active"
            _touch_stage(s)
        if ev.get("message"):
            st["activity"] = ev["message"]

    elif etype in ("tool_call", "provider_call"):
        s = _stage(st, stage_id)
        if s is not None:
            if ev.get("tool"):
                s["tool"] = ev["tool"]
            if ev.get("provider"):
                s["provider"] = ev["provider"]
            if ev.get("job_id"):
                s["job_id"] = ev["job_id"]
            _touch_stage(s)
        counts["tool_calls"] = int(counts.get("tool_calls") or 0) + 1
        if ev.get("message"):
            st["activity"] = ev["message"]

    elif etype in ("decision", "correction"):
        # A ``correction`` is a DISTINCT authoritative user-correction action —
        # it is not the same as an approval and is the only evidence that backs
        # correction-sourced learning (see lib.production_brain.evidence).
        counts["decisions"] = int(counts.get("decisions") or 0) + 1
        s = _stage(st, stage_id)
        if s is not None:
            _touch_stage(s)
        if ev.get("message"):
            st["activity"] = ev["message"]

    elif etype == "output":
        out = {
            "kind": data.get("kind") or "artifact",
            "path": data.get("path"),
            "label": data.get("label"),
            "stage": stage_id,
            "seq": seq,
        }
        st.setdefault("outputs", []).append(out)
        counts["outputs"] = int(counts.get("outputs") or 0) + 1
        s = _stage(st, stage_id)
        if s is not None:
            s.setdefault("outputs", []).append({k: out[k] for k in ("kind", "path", "label")})
            _touch_stage(s)
        # A rendered deliverable can carry the real duration.
        ad = data.get("actual_duration_seconds")
        if ad is not None:
            st["actual_duration_seconds"] = ad
        if ev.get("message"):
            st["activity"] = ev["message"]

    elif etype == "approval_requested":
        aid = data.get("approval_id") or f"appr-{seq}"
        st.setdefault("approvals", []).append({
            "approval_id": aid,
            "stage": stage_id,
            "status": "pending",
            "requested_at": ts,
            "decided_at": None,
            "by": None,
            "note": None,
            "prompt": data.get("prompt") or ev.get("message"),
        })
        s = _stage(st, stage_id)
        if s is not None:
            s["status"] = "awaiting_approval"
            _touch_stage(s)
        _set_state("awaiting_approval")
        st["activity"] = ev.get("message") or f"Awaiting approval for {STAGE_TITLES.get(stage_id, stage_id)}."

    elif etype in ("approval_granted", "approval_rejected"):
        aid = data.get("approval_id")
        granted = etype == "approval_granted"
        for a in st.get("approvals", []):
            if (aid and a["approval_id"] == aid) or (not aid and a["stage"] == stage_id and a["status"] == "pending"):
                a["status"] = "approved" if granted else "rejected"
                a["decided_at"] = ts
                a["by"] = data.get("by")
                a["note"] = data.get("note")
                break
        s = _stage(st, stage_id)
        if s is not None and s["status"] == "awaiting_approval":
            s["status"] = "active" if granted else "failed"
            _touch_stage(s)
        # Return to running unless another gate/blocker is outstanding.
        if not _has_pending_gate(st):
            _set_state("running")
        st["activity"] = ev.get("message") or (
            "Approval granted." if granted else "Approval rejected."
        )

    elif etype == "blocker_raised":
        bid = data.get("blocker_id") or f"blk-{seq}"
        kind = data.get("kind") if data.get("kind") in BLOCKER_KINDS else "other"
        st.setdefault("blockers", []).append({
            "blocker_id": bid,
            "stage": stage_id,
            "kind": kind,
            "message": data.get("message") or ev.get("message") or "Blocked.",
            "options": list(data.get("options") or []),
            "created_at": ts,
            "resolved": False,
            "resolved_at": None,
        })
        s = _stage(st, stage_id)
        if s is not None:
            s["status"] = "blocked"
            _touch_stage(s)
        _set_state("blocked")
        st["activity"] = ev.get("message") or data.get("message") or "Run blocked."

    elif etype == "blocker_cleared":
        bid = data.get("blocker_id")
        for b in st.get("blockers", []):
            if (bid and b["blocker_id"] == bid) or (not bid and b["stage"] == stage_id and not b["resolved"]):
                b["resolved"] = True
                b["resolved_at"] = ts
                if not bid:
                    break
        s = _stage(st, stage_id)
        if s is not None and s["status"] == "blocked":
            s["status"] = "active"
            _touch_stage(s)
        if not _has_pending_gate(st):
            _set_state("running")
        st["activity"] = ev.get("message") or "Blocker cleared."

    elif etype == "stage_completed":
        s = _stage(st, stage_id)
        if s is not None:
            s["status"] = "done"
            s["progress"] = 1.0
            s["ended_at"] = ts
            s["elapsed_seconds"] = _elapsed(s.get("started_at"), ts)
            _touch_stage(s)
        st["activity"] = ev.get("message") or f"Completed {STAGE_TITLES.get(stage_id, stage_id)}."

    elif etype == "stage_failed":
        s = _stage(st, stage_id)
        if s is not None:
            s["status"] = "failed"
            s["ended_at"] = ts
            s["error"] = data.get("error") or ev.get("message")
            _touch_stage(s)
        st["activity"] = ev.get("message") or f"{STAGE_TITLES.get(stage_id, stage_id)} failed."

    elif etype == "stage_skipped":
        s = _stage(st, stage_id)
        if s is not None:
            s["status"] = "skipped"
            s["ended_at"] = ts
            _touch_stage(s)

    elif etype == "retry":
        s = _stage(st, stage_id)
        if s is not None:
            s["status"] = "active"
            s["error"] = None
            s["ended_at"] = None
            if not s.get("started_at"):
                s["started_at"] = ts
            _touch_stage(s)
            st["current_stage"] = stage_id
        # Clear this stage's active blockers.
        for b in st.get("blockers", []):
            if b["stage"] == stage_id and not b["resolved"]:
                b["resolved"] = True
                b["resolved_at"] = ts
        if not _has_pending_gate(st):
            _set_state("running")
        st["activity"] = ev.get("message") or f"Retrying {STAGE_TITLES.get(stage_id, stage_id)}."

    elif etype == "resume":
        if not _has_pending_gate(st):
            _set_state("running")
        st["activity"] = ev.get("message") or "Run resumed."

    elif etype == "run_completed":
        for s in st["stages"]:
            if s["status"] in ("pending", "active"):
                # Do not fabricate work; leave pending untouched, close active.
                if s["status"] == "active":
                    s["status"] = "done"
                    s["progress"] = 1.0
                    s["ended_at"] = ts
        ad = data.get("actual_duration_seconds")
        if ad is not None:
            st["actual_duration_seconds"] = ad
        _set_state("completed")
        st["current_stage"] = None
        st["activity"] = ev.get("message") or "Production run completed."

    elif etype == "run_failed":
        st["error"] = data.get("error") or ev.get("message")
        _set_state("failed")
        st["activity"] = ev.get("message") or "Production run failed."

    elif etype == "run_cancel_requested":
        # Cancellation was requested but the EXTERNAL orchestrator has not
        # acknowledged it. This is a NON-terminal, retryable state — never report
        # a terminal ``cancelled`` until the external job is confirmed cancelled.
        _set_state("cancelling")
        bid = data.get("blocker_id") or f"blk-{seq}"
        bstage = stage_id or st.get("current_stage") or STAGES[0]
        if not any(b.get("blocker_id") == bid for b in st.get("blockers", [])):
            st.setdefault("blockers", []).append({
                "blocker_id": bid,
                "stage": bstage,
                "kind": "control_unconfirmed",
                "message": data.get("message") or ev.get("message")
                or "External cancellation is unconfirmed — retry.",
                "options": ["Retry cancellation"],
                "created_at": ts,
                "resolved": False,
                "resolved_at": None,
            })
        st["activity"] = ev.get("message") or (
            "Cancellation requested — awaiting external orchestrator acknowledgment.")

    elif etype == "run_cancelled":
        for s in st["stages"]:
            if s["status"] in ("active", "awaiting_approval", "blocked"):
                s["status"] = "skipped"
                s["ended_at"] = ts
        # A confirmed cancel resolves any outstanding cancellation blocker.
        for b in st.get("blockers", []):
            if not b.get("resolved"):
                b["resolved"] = True
                b["resolved_at"] = ts
        _set_state("cancelled")
        st["current_stage"] = None
        st["activity"] = ev.get("message") or "Production run cancelled. Completed work is preserved."

    elif etype == "external_handle_updated":
        # Mochlet implements retry/resume as a SUCCESSOR job; record the new
        # canonical handle so the UI shows the job that is actually running (we
        # never pretend the old job resumed). Lineage is preserved in the log.
        brain = st.setdefault("brain", {})
        new_job = data.get("job_id")
        new_sess = data.get("session_id")
        # Only a DIFFERENT job is a successor (resume); a same-id control (retry)
        # updates activity without inventing lineage.
        if new_job and new_job != brain.get("job_id"):
            brain["predecessor_job_id"] = brain.get("job_id")
            brain["job_id"] = new_job
        if new_sess:
            brain["session_id"] = new_sess
        if ev.get("message"):
            st["activity"] = ev["message"]

    elif etype in ("heartbeat", "note"):
        if ev.get("message"):
            st["activity"] = ev["message"]

    return st


def _has_pending_gate(st: dict) -> bool:
    if any(a.get("status") == "pending" for a in st.get("approvals", [])):
        return True
    if any((not b.get("resolved")) for b in st.get("blockers", [])):
        return True
    return False


def materialize(project_id: str, events: list[dict], *, strict: bool = False) -> dict:
    """Rebuild the full materialized state from the authoritative event log."""
    st = empty_state(project_id)
    for ev in events:
        st = reduce_event(st, ev, strict=strict)
    return st


def _deep_copy(obj: dict) -> dict:
    import copy

    return copy.deepcopy(obj)
