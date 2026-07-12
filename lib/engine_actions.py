"""Safe, allowlisted OAuth actions for subscription engines (status/connect/logout).

The Backlot settings page can check status, initiate a connect, or log out an
engine. Every action is constrained so a POST can never turn into arbitrary
command or engine execution:

  * Only the (engine, action) pairs in ``_ENGINE_ACTIONS`` are allowed — the
    engine id and action are looked up, never interpolated into a shell.
  * Only the exact, hard-coded argv for that pair is ever run (no shell=True, no
    user input in the command).
  * Output is SANITIZED: we return booleans / short safe strings derived from the
    vendor status parse — never the raw subprocess stdout/stderr, which can carry
    identity (email/org) or tokens.
  * ``logout`` is destructive and requires explicit ``confirm=True``.
  * ``connect`` for an interactive OAuth CLI is returned as MANUAL guidance (the
    login must complete in a terminal/browser the headless server can't own) —
    honest, not a fake success.

``runner``/``which`` are injectable so tests exercise every path with no real CLI
(and so verification never actually logs anyone out).
"""

from __future__ import annotations

import shutil
from typing import Callable, Optional

from lib.engines import ProbeResult, _default_runner, discover_engines

ACTIONS = ("status", "connect", "logout", "install", "verify")

# Per engine: how each action is handled.
#   ("status", None)        -> run the vendor status probe, return sanitized state
#   ("cmd", argv)           -> run this exact allowlisted command (logout)
#   ("manual", command_str) -> return guidance; DO NOT execute (interactive OAuth)
#   ("unsupported", reason) -> action not available for this engine
_ENGINE_ACTIONS: dict[str, dict[str, tuple]] = {
    "claude": {
        "status": ("status", None),
        "connect": ("manual", "claude auth login"),
        "logout": ("cmd", ["claude", "auth", "logout"]),
    },
    "codex": {
        "status": ("status", None),
        "connect": ("manual", "codex login"),
        "logout": ("cmd", ["codex", "logout"]),
    },
    "gemini": {
        "status": ("status", None),
        "connect": ("manual", "gemini"),
        "logout": ("unsupported", "The Gemini CLI has no non-interactive logout command."),
    },
    "zai": {
        "status": ("status", None),
        "connect": ("manual",
                    "Set ZAI_API_KEY (or point Claude Code at Z.AI via "
                    "ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN)"),
        "logout": ("unsupported", "Z.AI uses an API token/proxy, not an OAuth session to log out of."),
    },
    # Antigravity (Google) is structurally different (install download + interactive
    # browser OAuth); every action routes to run_antigravity_action.
    "antigravity": {
        "status": ("antigravity", None),
        "verify": ("antigravity", None),
        "install": ("antigravity", None),
        "connect": ("antigravity", None),
        "logout": ("antigravity", None),
    },
}


class EngineActionError(ValueError):
    """Raised for a disallowed/invalid engine action (maps to HTTP 400)."""


def supported_actions(engine: str) -> dict[str, str]:
    """UI hint: for one engine, action -> mode ('auto'|'manual'|'interactive'|'unsupported')."""
    if engine == "antigravity":
        # install/status/verify run non-interactively; connect/logout open a
        # Terminal for the interactive browser sign-in / TUI sign-out.
        return {"install": "auto", "status": "auto", "verify": "auto",
                "connect": "interactive", "logout": "interactive"}
    spec = _ENGINE_ACTIONS.get(engine, {})
    out: dict[str, str] = {}
    for action, (kind, _arg) in spec.items():
        out[action] = (
            "manual" if kind == "manual"
            else "unsupported" if kind == "unsupported"
            else "auto"
        )
    return out


def _open_terminal_agy(spawn: Optional[Callable] = None) -> bool:
    """Open a real Terminal running the FIXED `agy` command (no user input). The
    interactive Google sign-in / sign-out happens in that window. Injectable."""
    if spawn is not None:
        return bool(spawn())
    import json as _json
    import platform
    import shlex
    import subprocess
    from lib import antigravity
    if platform.system() != "Darwin":
        return False  # non-macOS: UI shows the manual command instead
    cmd = antigravity.sign_in_command()  # [<agy path>] — a constant, not user input
    inner = " ".join(shlex.quote(c) for c in cmd)
    subprocess.run(
        ["osascript", "-e", f'tell application "Terminal" to do script {_json.dumps(inner)}'],
        check=True, timeout=15, capture_output=True,
    )
    return True


def run_antigravity_action(
    action: str, *, confirm: bool = False, timeout: int = 20,
    prober: Optional[Callable] = None, installer: Optional[Callable] = None,
    spawn: Optional[Callable] = None,
) -> dict:
    """Antigravity (Google) actions on the INSPECTED `agy` contract. Sanitized —
    never identity/tokens/raw output."""
    from lib import antigravity

    if action in ("status", "verify"):
        st = (prober or antigravity.probe_status)(timeout=timeout)
        return {"ok": True, "engine": "antigravity", "action": action,
                "state": {"installed": bool(st.get("installed")),
                          "signed_in": bool(st.get("signed_in"))}}

    if action == "install":
        try:
            res = (installer or antigravity.install)()
        except antigravity.AntigravityInstallError as exc:
            raise EngineActionError(str(exc))
        ok = bool(res.get("installed"))
        return {"ok": ok, "engine": "antigravity", "action": "install",
                "installed": ok, "version": res.get("version"),
                "message": "Antigravity CLI installed." if ok else "Install did not complete."}

    if action == "connect":
        if not antigravity.is_installed():
            raise EngineActionError("Antigravity CLI is not installed — install it first.")
        started = _open_terminal_agy(spawn)
        return {"ok": started, "engine": "antigravity", "action": "connect",
                "mode": "interactive", "started": started,
                "message": ("A terminal opened for Google sign-in — complete it in your "
                            "browser, then use Refresh status." if started else
                            "Run `agy` in a terminal to sign in, then Refresh status.")}

    if action == "logout":
        if not antigravity.is_installed():
            raise EngineActionError("Antigravity CLI is not installed.")
        # Interactive-only sign-out (no guessed argv); requires explicit confirm.
        if not confirm:
            raise EngineActionError("logout requires explicit confirmation (confirm=true)")
        started = _open_terminal_agy(spawn)
        return {"ok": started, "engine": "antigravity", "action": "logout",
                "mode": "interactive", "started": started,
                "message": antigravity.sign_out_note()}

    raise EngineActionError(f"unknown antigravity action {action!r}")


def _sanitized_state(engine_status) -> dict:
    """Only non-secret, non-identity fields leave this module."""
    return {
        "installed": engine_status.installed,
        "logged_in": engine_status.logged_in,
        "auth_method": engine_status.auth_method,
        "subscription_backed": engine_status.subscription_backed,
        "subscription_type": engine_status.subscription_type,
    }


def run_engine_action(
    engine: str,
    action: str,
    *,
    confirm: bool = False,
    runner: Optional[Callable[[list, int], ProbeResult]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    timeout: int = 10,
) -> dict:
    """Execute an allowlisted engine action and return a sanitized result."""
    # Injection guards: engine + action must be known, else refuse (no execution).
    if engine not in _ENGINE_ACTIONS:
        raise EngineActionError(f"unknown engine {engine!r}")
    if action not in ACTIONS:
        raise EngineActionError(f"unknown action {action!r}")
    spec = _ENGINE_ACTIONS[engine].get(action)
    if spec is None:
        raise EngineActionError(f"action {action!r} is not available for {engine!r}")

    kind, arg = spec

    if kind == "antigravity":
        return run_antigravity_action(action, confirm=confirm, timeout=timeout)
    where = which or shutil.which
    run = runner or _default_runner

    if kind == "unsupported":
        return {"ok": False, "engine": engine, "action": action,
                "supported": False, "message": arg}

    if kind == "manual":
        # D (review 3): nothing was executed and OAuth was NOT completed, so this
        # is NOT a success. Report ok=False, started=False, and give engine-
        # specific instructions. Z.AI is a token/proxy path, not OAuth — its copy
        # must not imply an OAuth sign-in.
        is_oauth = engine in ("claude", "codex", "gemini")
        if is_oauth:
            message = (f"Sign-in is interactive and completes in your browser — the "
                       f"server can't do it for you. Run `{arg}` in a terminal, then "
                       f"use Refresh status.")
        else:  # zai — token/proxy path (deliberately avoids OAuth wording)
            message = (f"Z.AI uses an API token or a Claude-Code proxy — not an "
                       f"interactive sign-in. Configure a credential: {arg}. Then use Refresh status.")
        return {
            "ok": False, "engine": engine, "action": action, "mode": "manual",
            "started": False, "auth_kind": "oauth" if is_oauth else "api_token",
            "command": arg, "message": message,
        }

    if kind == "status":
        # discover_engines runs the vendor status probe internally and returns a
        # parsed, non-secret EngineStatus.
        [status] = discover_engines(only=[engine], probe_auth=True, runner=runner, which=which)
        return {"ok": True, "engine": engine, "action": "status", "state": _sanitized_state(status)}

    if kind == "cmd":
        # Destructive actions (logout) require explicit confirmation.
        if action == "logout" and not confirm:
            raise EngineActionError("logout requires explicit confirmation (confirm=true)")
        if where(arg[0]) is None:
            return {"ok": False, "engine": engine, "action": action,
                    "installed": False, "message": f"{arg[0]} is not installed."}
        proc = run(list(arg), timeout)
        ok = proc.ran and proc.returncode == 0
        # NEVER return proc.stdout/stderr — they can carry identity. Just a boolean
        # + a generic message.
        return {
            "ok": ok, "engine": engine, "action": action,
            "message": (f"Logged out of {engine}." if ok
                        else f"{action} did not complete (exit {proc.returncode})."),
        }

    raise EngineActionError(f"unhandled action spec for {engine!r}/{action!r}")  # pragma: no cover
