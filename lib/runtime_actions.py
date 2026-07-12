"""Composition-runtime maintenance actions (fixed, allowlisted).

Backlot exposes a few *operational* runtime actions — verify / install / repair
the Remotion runtime — so an operator can make it render-ready from Settings.

Hard rules:
  * Allowlisted RUNTIMES and ACTIONS only. No arbitrary package name, command,
    flag, cwd, or path ever comes from the caller.
  * Install/repair run a FIXED argv (``npm ci`` in the fixed remotion-composer
    dir); repair additionally runs a fixed Remotion "browser ensure" step so a
    long/paid render never has to discover a browser mid-flight.
  * Every result is sanitized: booleans + short generic strings + a fresh doctor
    report. Raw npm/CLI stderr and absolute paths never leak to the caller.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from lib import remotion_runtime as _rr

RUNTIMES = ("remotion",)
ACTIONS = ("verify", "install", "repair")


class RuntimeActionError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _doctor(**kw) -> dict:
    return _rr.doctor(**kw)


def install_command() -> tuple[list[str], Path]:
    """Fixed installer argv + cwd. Deterministic, lockfile-based, no caller input."""
    return (["npm", "ci", "--no-audit", "--no-fund"], _rr.composer_dir())


def browser_ensure_command() -> tuple[list[str], Path]:
    """Fixed 'download the Remotion browser' argv via the pinned local CLI."""
    return ([str(_rr.cli_bin_path()), "browser", "ensure"], _rr.composer_dir())


def _run_fixed(cmd_cwd: tuple[list[str], Path], timeout: int) -> tuple[int, str, str]:
    argv, cwd = cmd_cwd
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception:
        return -1, "", ""


def run_runtime_action(runtime: str, action: str, *,
                       installer: Optional[Callable[[], tuple]] = None,
                       browser_ensurer: Optional[Callable[[], tuple]] = None,
                       timeout: int = 600) -> dict:
    """Execute a fixed maintenance action; return a sanitized result + doctor."""
    if runtime not in RUNTIMES:
        raise RuntimeActionError("unsupported runtime for maintenance actions")
    if action not in ACTIONS:
        raise RuntimeActionError("unknown runtime action")

    if action == "verify":
        doc = _doctor()
        return {"ok": bool(doc["available"]), "action": "verify",
                "runtime": runtime, "doctor": doc}

    install = installer or (lambda: _run_fixed(install_command(), timeout))
    rc, _, _ = install()
    if action == "repair":
        ensure = browser_ensurer or (lambda: _run_fixed(browser_ensure_command(), timeout))
        # Best-effort browser ensure — offline it may fail, but the doctor still
        # detects a system/cached browser. We never surface raw output.
        ensure()

    doc = _doctor()
    ok = rc == 0 and bool(doc["available"])
    message = ("Remotion is render-ready." if doc["available"]
               else (doc["reason"] or "Install did not complete — see the runtime status."))
    return {"ok": ok, "action": action, "runtime": runtime,
            "message": message, "doctor": doc}
