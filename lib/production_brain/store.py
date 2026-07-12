"""Durable, single-writer production-run store (append-only event log + view).

Design contract:
  * The append-only ``run_events.jsonl`` is AUTHORITATIVE. ``state.json`` is a
    materialized cache that can always be rebuilt from the log, which is exactly
    how crash/restart recovery works — a torn or stale cache is discarded and
    recomputed from the durable log on the next read.
  * Exactly one writer touches these two files: this store. (Coarse run.json
    liveness stays owned by lib.production_run — different file, different
    concern — so there are no duplicate writers.)
  * Every append is atomic and monotonically sequenced, serialized within the
    process by a lock and across processes by an advisory file lock, so seq
    numbers never collide and lines never tear.
  * Secrets never reach disk: every event is redacted before it is written.
  * ``start`` is idempotent (one active run per project); ``cancel`` validates
    the EXACT run id; terminal states are sticky and truthful.

Everything time/id related is injectable so the store is fully unit-testable
without wall-clock or randomness.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from lib.production_brain import schema as S

BRAIN_DIRNAME = "brain"
EVENTS_FILENAME = "run_events.jsonl"
STATE_FILENAME = "state.json"
LOCK_FILENAME = ".brain.lock"

_proc_lock = threading.Lock()


class BrainStoreError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_gen_id() -> str:
    import secrets

    return "run_" + secrets.token_hex(6)


class ProductionBrainStore:
    """Per-project production-run telemetry store (single writer)."""

    def __init__(
        self,
        project_dir: Path | str,
        *,
        now: Callable[[], str] = _iso_now,
        gen_id: Callable[[], str] = _default_gen_id,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.brain_dir = self.project_dir / BRAIN_DIRNAME
        self.events_path = self.brain_dir / EVENTS_FILENAME
        self.state_path = self.brain_dir / STATE_FILENAME
        self._lock_path = self.brain_dir / LOCK_FILENAME
        self._now = now
        self._gen_id = gen_id

    # ---- paths / io helpers ------------------------------------------------
    def _ensure_dir(self) -> None:
        self.brain_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        """Serialize writers within and across processes.

        Within a process: a global lock. Across processes: an advisory flock on
        a dedicated lock file (best-effort; degrades to the process lock where
        flock is unavailable, e.g. Windows)."""
        self._ensure_dir()
        with _proc_lock:
            fd = None
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception:
                    pass
                yield
            finally:
                if fd is not None:
                    try:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                    os.close(fd)

    def _atomic_write_json(self, path: Path, obj: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    # ---- reads (never write) ----------------------------------------------
    def read_events_raw(self) -> list[dict]:
        """All events, oldest first. Tolerates a torn trailing line."""
        if not self.events_path.exists():
            return []
        events: list[dict] = []
        try:
            with open(self.events_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        events.sort(key=lambda e: int(e.get("seq") or 0))
        return events

    def read_events(self, after: int = 0, limit: Optional[int] = None) -> list[dict]:
        """Cursor read: events with ``seq > after`` (oldest first)."""
        evs = [e for e in self.read_events_raw() if int(e.get("seq") or 0) > int(after or 0)]
        if limit is not None:
            return evs[: max(0, int(limit))]
        return evs

    def _max_seq(self, events: Optional[list[dict]] = None) -> int:
        evs = events if events is not None else self.read_events_raw()
        return max((int(e.get("seq") or 0) for e in evs), default=0)

    def read_state(self) -> dict:
        """Return the materialized state — rebuilding from the log if the cache
        is missing, unreadable, or stale (the crux of crash recovery)."""
        events = self.read_events_raw()
        max_seq = self._max_seq(events)
        cached = None
        if self.state_path.exists():
            try:
                cached = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                cached = None
        if (
            isinstance(cached, dict)
            and cached.get("kind") == "production_run_state"
            and int(cached.get("cursor") or 0) == max_seq
        ):
            return cached
        # Stale/absent cache → rebuild from the authoritative log.
        return S.materialize(self.project_dir.name, events)

    def has_active_run(self) -> bool:
        st = self.read_state()
        return st.get("state") in S.ACTIVE_RUN_STATES

    # ---- write path --------------------------------------------------------
    def _append(self, event_fields: dict, *, strict: bool = False) -> dict:
        """Append one event atomically (acquires the writer lock)."""
        with self._locked():
            return self._append_locked(event_fields, strict=strict)

    def _append_locked(self, event_fields: dict, *, strict: bool = False) -> dict:
        """Append one event. CALLER MUST HOLD ``self._locked()`` — this exists so
        a compound critical section (check-active-then-append) can seq-assign and
        write without releasing the lock in between (the idempotent-start +
        no-impossible-event guarantees depend on this atomicity)."""
        events = self.read_events_raw()
        seq = self._max_seq(events) + 1
        # Recompute prior state consistently with the log FIRST, so control/telemetry
        # events can be structurally stamped with the run's persisted external
        # handle (session_id/job_id from state["brain"]). This makes restart /
        # cancel / retry / resume correlation machine-verifiable, not message text.
        prior = self._consistent_state(events, self._max_seq(events))
        fields = dict(event_fields)
        _brain = prior.get("brain") or {}
        # run_started carries its own identity; everything else inherits the run's
        # persisted external handle. Stamp when the caller didn't supply one
        # (absent OR None — event() passes explicit None keys).
        if fields.get("type") != "run_started":
            if not fields.get("session_id"):
                fields["session_id"] = _brain.get("session_id")
            if not fields.get("job_id"):
                fields["job_id"] = _brain.get("job_id")
        ev = {
            "v": S.SCHEMA_VERSION,
            "seq": seq,
            "ts": self._now(),
        }
        ev.update({k: v for k, v in fields.items() if v is not None})
        ev = S.redact_event(ev)
        # A strict fold raises InvalidTransition BEFORE anything is persisted, so
        # an impossible event never reaches the durable log.
        new_state = S.reduce_event(prior, ev, strict=strict)
        # Append the durable event first, then refresh the cache.
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, default=str) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        self._atomic_write_json(self.state_path, new_state)
        return ev

    def _guarded_append(self, event_fields: dict, *, run_id: Optional[str] = None,
                        require_active: bool = True, strict: bool = True) -> dict:
        """Validate the run under the SAME lock hold as the append, then append.

        This closes the check-then-act race: two callers can no longer both pass
        an active-run / pending-approval check and then each append. ``strict``
        also makes an impossible coarse-state transition raise instead of being
        silently persisted."""
        with self._locked():
            st = self.read_state()
            if require_active:
                if st.get("state") not in S.ACTIVE_RUN_STATES:
                    raise BrainStoreError("There is no active run for this event.", status=409)
                if run_id is not None and st.get("run_id") != run_id:
                    raise BrainStoreError("Run id does not match the active run.", status=409)
            fields = dict(event_fields)
            fields.setdefault("run_id", st.get("run_id"))
            fields.setdefault("project_id", self.project_dir.name)
            try:
                return self._append_locked(fields, strict=strict)
            except S.InvalidTransition as exc:
                raise BrainStoreError(f"invalid transition for this run ({exc})", status=409)

    def _consistent_state(self, events: list[dict], max_seq: int) -> dict:
        cached = None
        if self.state_path.exists():
            try:
                cached = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                cached = None
        if (
            isinstance(cached, dict)
            and cached.get("kind") == "production_run_state"
            and int(cached.get("cursor") or 0) == max_seq
        ):
            return cached
        return S.materialize(self.project_dir.name, events)

    # ---- lifecycle ---------------------------------------------------------
    def start(
        self,
        *,
        run_id: Optional[str] = None,
        brain: Optional[dict] = None,
        requested_duration_seconds: Optional[int] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> dict:
        """Begin a run. Idempotent: if one is already active, return it with
        ``already_active=True`` and DO NOT append a second run_started.

        The active-run check and the ``run_started`` append happen under a SINGLE
        lock hold, so two concurrent starts cannot both pass the check and each
        append — exactly one wins; the other returns ``already_active``."""
        # Duration validation is pure — do it outside the lock.
        rid = run_id or self._gen_id()
        data: dict = {"brain": brain or {}}
        if requested_duration_seconds is not None:
            # Preserve the canonical 1..300s target contract. ``actual`` duration
            # is recorded separately (and may differ) once the deliverable renders.
            from lib import duration as _dur

            try:
                requested_duration_seconds = _dur.validate_target_seconds(requested_duration_seconds)
            except _dur.DurationError as exc:
                raise BrainStoreError(str(exc), status=400)
            data["requested_duration_seconds"] = int(requested_duration_seconds)
        with self._locked():
            st = self.read_state()
            if st.get("state") in S.ACTIVE_RUN_STATES:
                out = dict(st)
                out["already_active"] = True
                return out
            self._append_locked(
                {
                    "type": "run_started",
                    "run_id": rid,
                    "project_id": self.project_dir.name,
                    "agent_id": agent_id or (brain or {}).get("agent_id"),
                    "session_id": session_id or (brain or {}).get("session_id"),
                    "job_id": job_id or (brain or {}).get("job_id"),
                    "message": message or "Production run started.",
                    "data": data,
                },
                strict=True,
            )
        return self.read_state()

    def start_provisioned(
        self,
        *,
        provision: "Callable[[str], tuple]",
        run_id: Optional[str] = None,
        requested_duration_seconds: Optional[int] = None,
        brain: Optional[dict] = None,
        message: Optional[str] = None,
    ) -> dict:
        """Reserve the run, THEN provision an external job — atomically.

        The active-run check, the ``provision`` call, and the ``run_started``
        append all happen under a SINGLE per-project lock hold. So of two
        concurrent starts, only the WINNER ever calls ``provision`` (creating
        exactly one external job); the loser blocks on the lock, then sees the
        active run and returns ``already_active`` WITHOUT provisioning — no orphan
        external job. ``provision(run_id) -> (session_id, job_id, brain_extra, msg)``
        may raise to fail closed (then no run is opened)."""
        # Duration validation is pure — do it before taking the lock.
        if requested_duration_seconds is not None:
            from lib import duration as _dur

            try:
                requested_duration_seconds = _dur.validate_target_seconds(requested_duration_seconds)
            except _dur.DurationError as exc:
                raise BrainStoreError(str(exc), status=400)
        with self._locked():
            st = self.read_state()
            if st.get("state") in S.ACTIVE_RUN_STATES:
                out = dict(st)
                out["already_active"] = True
                return out
            rid = run_id or self._gen_id()
            # WINNER-ONLY: reached only when no active run exists, so the external
            # job is created exactly once.
            session_id, job_id, brain_extra, provisioned_msg = provision(rid)
            merged_brain = {**(brain or {}), **(brain_extra or {})}
            data: dict = {"brain": merged_brain}
            if requested_duration_seconds is not None:
                data["requested_duration_seconds"] = int(requested_duration_seconds)
            self._append_locked(
                {
                    "type": "run_started",
                    "run_id": rid,
                    "project_id": self.project_dir.name,
                    "agent_id": merged_brain.get("agent_id"),
                    "session_id": session_id or merged_brain.get("session_id"),
                    "job_id": job_id or merged_brain.get("job_id"),
                    "message": message or provisioned_msg or "Production run started.",
                    "data": data,
                },
                strict=True,
            )
        return self.read_state()

    # ---- generic event helpers (used by the adapter/brain) -----------------
    def event(
        self,
        etype: str,
        *,
        stage: Optional[str] = None,
        tool: Optional[str] = None,
        provider: Optional[str] = None,
        job_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message: Optional[str] = None,
        level: str = "info",
        data: Optional[dict] = None,
        run_id: Optional[str] = None,
    ) -> dict:
        """Emit an event for the CURRENT active run.

        Refuses (409) when there is no active run, or when ``run_id`` is given and
        does not match the active run — so the authoritative log can never contain
        an event before a run starts or after it is terminal. Uses a strict fold,
        so an impossible coarse-state transition raises instead of being persisted.
        """
        if etype not in S.EVENT_TYPES:
            raise BrainStoreError(f"unknown event type: {etype}")
        return self._guarded_append(
            {
                "type": etype,
                "stage": stage,
                "tool": tool,
                "provider": provider,
                "job_id": job_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "message": message,
                "level": level,
                "data": data,
            },
            run_id=run_id,
            require_active=True,
            strict=True,
        )

    # thin, self-documenting wrappers -------------------------------------------------
    def enter_stage(self, stage: str, *, message: Optional[str] = None, **kw) -> dict:
        return self.event("stage_entered", stage=stage, message=message, **kw)

    def stage_progress(self, stage: str, progress: float, *, message: Optional[str] = None, **kw) -> dict:
        return self.event("stage_progress", stage=stage, message=message,
                          data={"progress": progress}, **kw)

    def tool_call(self, stage: str, tool: str, *, provider: Optional[str] = None,
                  job_id: Optional[str] = None, message: Optional[str] = None, data: Optional[dict] = None, **kw) -> dict:
        return self.event("tool_call", stage=stage, tool=tool, provider=provider,
                          job_id=job_id, message=message, data=data, **kw)

    def provider_call(self, stage: str, provider: str, *, tool: Optional[str] = None,
                      job_id: Optional[str] = None, message: Optional[str] = None, data: Optional[dict] = None, **kw) -> dict:
        return self.event("provider_call", stage=stage, provider=provider, tool=tool,
                          job_id=job_id, message=message, data=data, **kw)

    def decision(self, stage: str, *, message: Optional[str] = None, data: Optional[dict] = None, **kw) -> dict:
        return self.event("decision", stage=stage, message=message, data=data, **kw)

    def record_correction(self, stage: str, *, decision_ref: str, message: Optional[str] = None,
                          data: Optional[dict] = None, **kw) -> dict:
        """Append a DISTINCT authoritative user-correction event for this run+stage.

        This is the ONLY event that backs correction-sourced learning — an
        approval or a generic decision does not qualify as correction evidence."""
        payload = {"decision_ref": decision_ref}
        if data:
            payload.update(data)
        return self.event("correction", stage=stage, message=message, data=payload, **kw)

    def output(self, stage: str, *, kind: str, path: Optional[str] = None, label: Optional[str] = None,
               actual_duration_seconds: Optional[float] = None, message: Optional[str] = None, **kw) -> dict:
        data = {"kind": kind, "path": path, "label": label}
        if actual_duration_seconds is not None:
            data["actual_duration_seconds"] = actual_duration_seconds
        return self.event("output", stage=stage, message=message, data=data, **kw)

    def request_approval(self, stage: str, *, prompt: Optional[str] = None,
                         approval_id: Optional[str] = None, message: Optional[str] = None, **kw) -> dict:
        return self.event("approval_requested", stage=stage, message=message,
                          data={"prompt": prompt, "approval_id": approval_id}, **kw)

    def complete_stage(self, stage: str, *, message: Optional[str] = None, **kw) -> dict:
        return self.event("stage_completed", stage=stage, message=message, **kw)

    def fail_stage(self, stage: str, *, error: str, message: Optional[str] = None, **kw) -> dict:
        return self.event("stage_failed", stage=stage, message=message, data={"error": error}, **kw)

    def raise_blocker(self, stage: str, *, kind: str, message: str,
                      options: Optional[list] = None, blocker_id: Optional[str] = None, **kw) -> dict:
        return self.event("blocker_raised", stage=stage, message=message,
                          level="error", data={"kind": kind, "message": message,
                                               "options": options or [], "blocker_id": blocker_id}, **kw)

    def clear_blocker(self, stage: str, *, blocker_id: Optional[str] = None, message: Optional[str] = None, **kw) -> dict:
        return self.event("blocker_cleared", stage=stage, message=message,
                          data={"blocker_id": blocker_id}, **kw)

    def heartbeat(self, *, message: Optional[str] = None, **kw) -> dict:
        return self.event("heartbeat", message=message, **kw)

    # ---- approvals / control (validate the EXACT run id) -------------------
    def grant_approval(self, run_id: str, *, approval_id: Optional[str] = None,
                       stage: Optional[str] = None, by: Optional[str] = None,
                       note: Optional[str] = None) -> dict:
        return self._decide_approval(run_id, granted=True, approval_id=approval_id,
                                    stage=stage, by=by, note=note)

    def reject_approval(self, run_id: str, *, approval_id: Optional[str] = None,
                        stage: Optional[str] = None, by: Optional[str] = None,
                        note: Optional[str] = None) -> dict:
        return self._decide_approval(run_id, granted=False, approval_id=approval_id,
                                    stage=stage, by=by, note=note)

    def _decide_approval(self, run_id: str, *, granted: bool, approval_id: Optional[str],
                         stage: Optional[str], by: Optional[str], note: Optional[str]) -> dict:
        # Resolve the pending approval + append the decision under ONE lock hold,
        # so a concurrent grant/reject/cancel can't race the resolution.
        verb = "grant" if granted else "reject"
        with self._locked():
            st = self.read_state()
            if st.get("state") not in S.ACTIVE_RUN_STATES:
                raise BrainStoreError("There is no active run.", status=409)
            if st.get("run_id") != run_id:
                raise BrainStoreError("Run id does not match the active run.", status=409)
            target = self._resolve_pending_approval(st, approval_id, stage)
            if target is None:
                raise BrainStoreError(f"There is no pending approval to {verb}.", status=409)
            self._append_locked({
                "type": "approval_granted" if granted else "approval_rejected",
                "run_id": run_id, "project_id": self.project_dir.name,
                "stage": target["stage"], "by": by,
                "message": (f"Approved: {S.STAGE_TITLES.get(target['stage'], target['stage'])}."
                            if granted else
                            f"Rejected: {S.STAGE_TITLES.get(target['stage'], target['stage'])}."),
                "data": {"approval_id": target["approval_id"], "by": by, "note": note},
            }, strict=True)
        return self.read_state()

    @staticmethod
    def _resolve_pending_approval(st: dict, approval_id: Optional[str], stage: Optional[str]) -> Optional[dict]:
        pend = [a for a in st.get("approvals", []) if a.get("status") == "pending"]
        if approval_id:
            for a in pend:
                if a["approval_id"] == approval_id:
                    return a
            return None
        if stage:
            for a in pend:
                if a["stage"] == stage:
                    return a
            return None
        return pend[0] if pend else None

    def retry_stage(self, stage: str, *, run_id: Optional[str] = None,
                    message: Optional[str] = None) -> dict:
        if stage not in S.STAGES:
            raise BrainStoreError(f"unknown stage: {stage}")
        self._guarded_append({
            "type": "retry", "stage": stage,
            "message": message or f"Retrying {S.STAGE_TITLES.get(stage, stage)}.",
        }, run_id=run_id, require_active=True, strict=True)
        return self.read_state()

    def resume(self, *, message: Optional[str] = None) -> dict:
        """Reconcile + continue a run after a restart. Recomputes the state from
        the durable log (crash recovery) and records a resume marker if active."""
        with self._locked():
            st = self.read_state()
            if st.get("state") in S.ACTIVE_RUN_STATES:
                self._append_locked({
                    "type": "resume", "run_id": st.get("run_id"),
                    "project_id": self.project_dir.name,
                    "message": message or "Run resumed after restart.",
                }, strict=True)
        return self.read_state()

    def complete_run(self, run_id: str, *, actual_duration_seconds: Optional[float] = None,
                     message: Optional[str] = None) -> dict:
        data = {}
        if actual_duration_seconds is not None:
            data["actual_duration_seconds"] = actual_duration_seconds
        self._guarded_append({
            "type": "run_completed", "run_id": run_id, "project_id": self.project_dir.name,
            "message": message or "Production run completed.", "data": data,
        }, run_id=run_id, require_active=True, strict=True)
        return self.read_state()

    def fail_run(self, run_id: str, *, error: str, message: Optional[str] = None) -> dict:
        self._guarded_append({
            "type": "run_failed", "run_id": run_id, "project_id": self.project_dir.name,
            "level": "error", "message": message or "Production run failed.",
            "data": {"error": error},
        }, run_id=run_id, require_active=True, strict=True)
        return self.read_state()

    def cancel(self, run_id: str, *, message: Optional[str] = None) -> dict:
        """Cancel the EXACT active run. Project-scoped; touches nothing else."""
        self._guarded_append({
            "type": "run_cancelled", "run_id": run_id, "project_id": self.project_dir.name,
            "message": message or "Production run cancelled. Completed work is preserved.",
        }, run_id=run_id, require_active=True, strict=True)
        return self.read_state()

    # ---- read payload (enriched with live elapsed) ------------------------
    def payload(self, *, now: Optional[Callable[[], str]] = None) -> dict:
        """Materialized state enriched with live elapsed-time on the active stage."""
        st = self.read_state()
        clock = now or self._now
        nowts = clock()
        for s in st.get("stages", []):
            if s.get("status") in ("active", "awaiting_approval", "blocked") and s.get("started_at") and not s.get("ended_at"):
                s["elapsed_seconds"] = S._elapsed(s["started_at"], nowts)
        return st
