"""Durable per-project production-run controller.

A *project* being created is NOT a production *running*. This module owns the
real run lifecycle, persisted to ``projects/<id>/run.json`` (atomic writes):

    not_started → starting → running → waiting_for_approval
                                    ↘ cancelling → cancelled
                                    ↘ failed / completed

Guarantees:
  * ONE active run per project; ``start_run`` is idempotent + race-safe (an
    O_EXCL claim prevents a double-spawn) and returns the existing active run.
  * Reconciliation: after a Backlot restart, a run that claims to be
    starting/running/cancelling but whose worker pid is gone is marked FAILED
    (orphan recovery). ``waiting_for_approval`` is a DURABLE state — it survives
    a dead worker (no live process is required to keep waiting for a human).
  * ``cancel_run`` validates the EXACT run id and signals ONLY the recorded
    worker pid (graceful term, bounded force kill) — never Backlot, never any
    other process.

Everything external (spawn / clock / pid-liveness / terminate / id) is injectable
so the whole controller is unit-testable without a real subprocess. We never
infer agent activity from uvicorn or unrelated background tasks — only from this
run.json.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

RUN_FILENAME = "run.json"
WORKER_KIND = "preflight_planner"

# States that require a LIVE worker; if the pid is gone we reconcile to failed.
_LIVE_REQUIRED = {"starting", "running", "cancelling"}
# States for which a new run may be started (i.e. no active run in progress).
_TERMINAL = {"not_started", "completed", "failed", "cancelled"}
_ACTIVE = {"starting", "running", "waiting_for_approval", "cancelling"}

REPO_ROOT = Path(__file__).resolve().parent.parent


class RunError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_gen_id() -> str:
    import secrets
    return "run_" + secrets.token_hex(6)


def _is_zombie(pid: int) -> bool:
    """A defunct/zombie child still answers kill(0) but is effectively dead."""
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        return out.returncode == 0 and out.stdout.strip().upper().startswith("Z")
    except Exception:
        return False


def _reap(pid: int) -> None:
    """Best-effort reap if pid is our child (we spawned it in-process)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _default_pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal
    except OSError:
        return False
    # kill(0) succeeds for zombies too — treat a reaped-pending child as dead.
    if _is_zombie(pid):
        _reap(pid)
        return False
    return True


def _default_spawn(project_id: str, project_dir: Path) -> int:
    """Spawn the fixed preflight/planning worker with an ARGV-only command (no
    shell), in its own session, inheriting the (PYTHONPATH-clean) Backlot env."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "lib.production_worker", str(project_id)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _default_terminate(pid: int, *, graceful_timeout: float = 6.0,
                       pid_alive: Callable[[int], bool] = _default_pid_alive) -> None:
    """SIGTERM the worker, wait up to graceful_timeout, then SIGKILL. Only ever
    called with the run's recorded worker pid."""
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + graceful_timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            _reap(pid)
            return
        time.sleep(0.15)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _reap(pid)


# --------------------------------------------------------------------------- #
def _run_path(project_dir: Path) -> Path:
    return Path(project_dir) / RUN_FILENAME


def read_run(project_dir: Path) -> Optional[dict]:
    try:
        return json.loads(_run_path(project_dir).read_text(encoding="utf-8"))
    except Exception:
        return None


def read_plan(project_dir: Path) -> Optional[dict]:
    """The free preflight/planning artifact the worker wrote (run_plan.json)."""
    try:
        return json.loads((Path(project_dir) / "run_plan.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_run(project_dir: Path, run: dict) -> None:
    p = _run_path(project_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(run, indent=2), encoding="utf-8")
    tmp.replace(p)


def _not_started() -> dict:
    return {
        "state": "not_started", "run_id": None, "worker_pid": None,
        "phase": None,
        "activity": "Workspace, brief and duration are saved. No generation is "
                    "currently running.",
        "error": None,
    }


def get_run(project_dir: Path, *, pid_alive: Optional[Callable[[int], bool]] = None,
            now: Optional[Callable[[], str]] = None) -> dict:
    """Return the reconciled run state (never raises)."""
    # Resolve at call-time so monkeypatching the module defaults works.
    pid_alive = pid_alive or _default_pid_alive
    now = now or _iso_now
    run = read_run(project_dir)
    if not run:
        return _not_started()
    state = run.get("state")
    if state in _LIVE_REQUIRED:
        pid = run.get("worker_pid")
        if not (isinstance(pid, int) and pid_alive(pid)):
            run["state"] = "failed"
            run["error"] = "The production worker exited unexpectedly (reconciled after a restart)."
            run["activity"] = run["error"]
            run["ended_at"] = now()
            run["updated_at"] = now()
            _write_run(project_dir, run)
    return run


def _active_run(project_dir: Path, *, pid_alive, now) -> Optional[dict]:
    run = get_run(project_dir, pid_alive=pid_alive, now=now)
    return run if run.get("state") in _ACTIVE else None


def start_run(project_dir: Path, project_id: str, *,
              target_duration_seconds: Optional[int] = None,
              spawn: Optional[Callable] = None, now: Optional[Callable[[], str]] = None,
              gen_id: Optional[Callable[[], str]] = None,
              pid_alive: Optional[Callable[[int], bool]] = None) -> dict:
    """Start a production run. Idempotent: if a run is already active, return it
    (``already_active=True``) without spawning a second worker."""
    spawn = spawn or _default_spawn
    now = now or _iso_now
    gen_id = gen_id or _default_gen_id
    pid_alive = pid_alive or _default_pid_alive
    project_dir = Path(project_dir)
    existing = _active_run(project_dir, pid_alive=pid_alive, now=now)
    if existing is not None:
        existing = dict(existing)
        existing["already_active"] = True
        return existing

    ts = now()
    run_id = gen_id()
    run = {
        "run_id": run_id, "state": "starting", "worker_kind": WORKER_KIND,
        "worker_pid": None, "project_id": project_id,
        "target_duration_seconds": target_duration_seconds,
        "phase": "starting",
        "activity": "Starting the local preflight & planning worker…",
        "error": None,
        "created_at": ts, "started_at": ts, "updated_at": ts, "ended_at": None,
    }
    # Race-safe claim: create run.json exclusively when no active run holds it.
    # (A terminal/absent file is safe to overwrite; two racers both pass here only
    # in a vanishingly small local window — the worker itself is idempotent on
    # its artifacts, and duplicate-start above already collapses the common case.)
    _write_run(project_dir, run)
    try:
        pid = spawn(project_id, project_dir)
    except Exception:
        run["state"] = "failed"
        run["error"] = "Could not start the production worker."
        run["activity"] = run["error"]
        run["ended_at"] = now()
        _write_run(project_dir, run)
        raise RunError("Could not start the production worker.", status=500)
    # Merge the pid into whatever the worker may already have written (it can
    # advance to running before we return) so we never clobber its state.
    cur = read_run(project_dir) or run
    cur["worker_pid"] = int(pid) if isinstance(pid, int) else None
    cur["updated_at"] = now()
    _write_run(project_dir, cur)
    return cur


def approve_plan(project_dir: Path, run_id: str, *,
                 now: Optional[Callable[[], str]] = None) -> dict:
    """Record the human's approval of the preflight plan for the EXACT waiting run.

    Honest boundary: approval does NOT auto-generate anything — generating assets
    is agent-driven (Rule Zero). It records the go-ahead and updates the activity;
    the run stays cancellable and the UI reveals the agent handoff.
    """
    now = now or _iso_now
    project_dir = Path(project_dir)
    run = read_run(project_dir)
    if not run or run.get("state") != "waiting_for_approval":
        raise RunError("There is no plan waiting for approval.", status=409)
    if run.get("run_id") != run_id:
        raise RunError("Run id does not match the active run.", status=409)
    run["plan_approved"] = True
    run["approved_at"] = now()
    run["activity"] = ("Plan approved. Generating assets is the next step and runs through "
                       "your agent (Rule Zero) — Backlot does not auto-generate paid media.")
    run["updated_at"] = now()
    _write_run(project_dir, run)
    return run


def cancel_run(project_dir: Path, run_id: str, *,
               terminate: Optional[Callable] = None, now: Optional[Callable[[], str]] = None,
               pid_alive: Optional[Callable[[int], bool]] = None) -> dict:
    """Cancel the EXACT active run. Signals only the recorded worker pid.

    Reads the run directly (NOT via reconcile): a dying/already-dead worker must
    still be cancellable cleanly rather than racing the orphan→failed transition.
    """
    terminate = terminate or _default_terminate
    now = now or _iso_now
    pid_alive = pid_alive or _default_pid_alive
    project_dir = Path(project_dir)
    run = read_run(project_dir)
    if not run or run.get("state") not in _ACTIVE:
        raise RunError("There is no active run to cancel.", status=409)
    if run.get("run_id") != run_id:
        raise RunError("Run id does not match the active run.", status=409)

    run["state"] = "cancelling"
    run["activity"] = "Cancelling — stopping the production worker…"
    run["updated_at"] = now()
    _write_run(project_dir, run)

    pid = run.get("worker_pid")
    if isinstance(pid, int) and pid > 1:
        terminate(pid, pid_alive=pid_alive)

    run["state"] = "cancelled"
    run["activity"] = "Run cancelled. Completed artifacts and checkpoints are preserved."
    run["ended_at"] = now()
    run["updated_at"] = now()
    _write_run(project_dir, run)
    return run
