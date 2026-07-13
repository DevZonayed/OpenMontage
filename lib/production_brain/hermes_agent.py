"""Native Hermes Agent integration — detection, readiness, and session start.

OpenMontage is operated by the **Hermes Agent** through Hermes's own supported
embedding surface: the **ACP stdio adapter** (`hermes-acp` / `python -m
acp_adapter`, JSON-RPC 2.0 over stdio — ``initialize`` → ``session/new`` →
``session/prompt`` / ``session/cancel``). This is the ONLY external system
OpenMontage talks to. Whatever run engine Hermes uses internally is Hermes's
private business and is invisible here — there is no external orchestrator endpoint, token,
project, or job in OpenMontage's runtime or UI.

Design (a narrow OpenMontage-owned adapter so the backend can evolve):

  * :class:`HermesAgentDetector` — safe, read-only detection of the local Hermes
    install plus a bounded, side-effect-free readiness probe (``hermes-acp
    --check`` / ``--version``). No network, no credentials, no state.db access.
  * :class:`NativeHermesAgentClient` — implements the
    :class:`~lib.production_brain.orchestrator.HermesOrchestratorClient` port. A run
    over ACP only lives while the Hermes process owning its session is alive, so the
    default (ephemeral-probe) run starter **fails closed** rather than fabricate an
    "active" run behind an already-exited process (:func:`_default_session_factory`).
    A durable session runner is wired by injecting ``session_factory`` /
    ``canceller`` (also how the offline unit tests drive it); that runner opens a
    genuine Hermes session, keeps the process alive, and returns Hermes's own
    session id (never fabricated). The real ACP wire client (:class:`_AcpStdioClient`)
    spawns the allowlisted local Hermes binary with an argv list (never a shell),
    bounded timeouts, and a minimal environment.
  * :func:`agent_status` — the plain-language connection view the board/Studio
    render. It NEVER contains an endpoint, token, project, or job field.

Fail-closed contract: if Hermes is not installed, not verifiably launchable, or a
session cannot be truthfully created, ``available()`` is False and Start Production
surfaces an honest "Hermes Agent integration not configured" blocker — manual
editing always remains available. Nothing here invents success.

Security (contract D):
  * the launch target is an ALLOWLISTED local path under the Hermes home
    (``~/.hermes/hermes-agent``); it is spawned with an argv list — never a shell
    string — so no user input can be injected as a command;
  * every subprocess call is bounded by a timeout and its handle is always
    reaped/killed;
  * the working directory is bound to the validated OpenMontage repo root, never
    to caller-supplied text;
  * no credential is ever read, stored, logged, or transmitted (the ACP surface is
    loopback stdio and needs none);
  * the durable session handle persisted under ``.backlot/`` is non-secret.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from lib.paths import REPO_ROOT
from lib.production_brain.orchestrator import (
    OrchestratorHandle,
    OrchestratorUnavailable,
    is_canonical_id,
)

# The agent engine label stamped on telemetry for a real native connection. Only
# used when a genuine Hermes ACP session backs the run.
AGENT_ENGINE = "hermes-agent"
AGENT_DISPLAY_NAME = "Hermes Agent"

# Where Hermes is installed locally. Overridable for tests via ``HERMES_HOME``.
_DEFAULT_HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
_HERMES_AGENT_DIRNAME = "hermes-agent"

# Bounded timeouts (seconds) — every Hermes subprocess is capped.
_VERIFY_TIMEOUT = 25.0
_SESSION_TIMEOUT = 45.0

# Non-secret local state (gitignored ``.backlot/``).
_CONFIG_DIRNAME = ".backlot"
_CONFIG_FILENAME = "hermes_agent.json"          # {enabled, version}
_SESSION_FILENAME = "hermes_agent_session.json"  # durable session handles


# --------------------------------------------------------------------------- #
# Detection + readiness (safe, read-only, side-effect-free)
# --------------------------------------------------------------------------- #
def _hermes_home(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else _DEFAULT_HERMES_HOME


def _default_runner(argv: list[str], *, cwd: Optional[str] = None,
                    timeout: float = _VERIFY_TIMEOUT) -> tuple[int, str, str]:
    """Run an allowlisted argv (NO shell), bounded by ``timeout``. Never raises for
    a non-zero exit; only returns a synthetic failure tuple on spawn/timeout."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell, allowlisted binary
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=_minimal_env(), check=False)
    except subprocess.TimeoutExpired:
        return (124, "", "timed out")
    except (OSError, ValueError) as exc:
        return (127, "", f"{exc.__class__.__name__}")
    return (proc.returncode, proc.stdout or "", proc.stderr or "")


def _minimal_env() -> dict:
    """A minimal, sanitized environment for the Hermes subprocess (no secrets)."""
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER",
            "HERMES_HOME", "SystemRoot", "USERPROFILE", "APPDATA", "PATHEXT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


class HermesAgentDetector:
    """Detects the local Hermes install and verifies the ACP surface is launchable.

    Read-only and side-effect-free: detection is filesystem stat only; readiness
    runs Hermes's own ``--check``/``--version`` probes which import the adapter and
    exit without creating a session or touching state.db.
    """

    def __init__(self, *, home: Optional[Path] = None,
                 runner: Optional[Callable[..., tuple[int, str, str]]] = None) -> None:
        self._home = _hermes_home(home)
        self._runner = runner or _default_runner

    @property
    def agent_dir(self) -> Path:
        return self._home / _HERMES_AGENT_DIRNAME

    def _venv_python(self) -> Optional[Path]:
        base = self.agent_dir / "venv"
        for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
            p = base / rel
            if p.exists():
                return p
        return None

    def launch_argv(self) -> Optional[list[str]]:
        """The allowlisted argv that runs the Hermes ACP adapter, or None.

        Prefers the install's own venv interpreter with ``-m acp_adapter``; falls
        back to a ``hermes-acp`` console script on PATH only if it resolves inside
        the Hermes home (never an arbitrary PATH entry)."""
        agent_dir = self.agent_dir
        if not agent_dir.is_dir():
            return None
        py = self._venv_python()
        if py is not None and (agent_dir / "acp_adapter" / "__init__.py").exists():
            return [str(py), "-m", "acp_adapter"]
        # Console-script fallback, but only if it lives under the Hermes home.
        script = shutil.which("hermes-acp")
        if script:
            try:
                resolved = Path(script).resolve()
                home = self._home.resolve()
                # A true path-boundary check — NOT a string prefix, which would let a
                # sibling like ``~/.hermes-evil/hermes-acp`` pass the ``~/.hermes`` prefix.
                if resolved.is_relative_to(home):
                    return [str(resolved)]
            except OSError:
                return None
        return None

    def detect(self) -> dict:
        """Filesystem-only detection. Never spawns a process."""
        argv = self.launch_argv()
        installed = argv is not None
        return {
            "installed": installed,
            "launch": argv,
            "home": str(self._home),
            "reason": "" if installed else (
                f"Hermes is not installed at {self.agent_dir}."),
        }

    def verify(self) -> dict:
        """Bounded, side-effect-free readiness probe via ``hermes-acp --check``.

        Returns ``{installed, ready, version, detail}``. ``ready`` is True only when
        the adapter imports cleanly (``--check`` exits 0). Never raises."""
        det = self.detect()
        if not det["installed"]:
            return {"installed": False, "ready": False, "version": None,
                    "detail": det["reason"]}
        argv = det["launch"]
        code, out, err = self._runner(argv + ["--check"], cwd=None, timeout=_VERIFY_TIMEOUT)
        if code != 0:
            reason = (err or out or "").strip().splitlines()[-1:] or [""]
            return {"installed": True, "ready": False, "version": None,
                    "detail": f"Hermes ACP check failed ({reason[0][:200] or f'exit {code}'})."}
        version = None
        vcode, vout, _ = self._runner(argv + ["--version"], cwd=None, timeout=_VERIFY_TIMEOUT)
        if vcode == 0:
            version = (vout or "").strip().splitlines()[-1:] or [None]
            version = version[0] or None
        return {"installed": True, "ready": True, "version": version,
                "detail": "Hermes Agent is installed and its ACP surface is ready."}


# --------------------------------------------------------------------------- #
# Real ACP stdio transport (spawns the allowlisted local Hermes binary)
# --------------------------------------------------------------------------- #
class _AcpStdioClient:
    """A minimal, hardened newline-delimited JSON-RPC client over ACP stdio.

    Speaks exactly what the Hermes ACP adapter expects: ``initialize`` →
    ``session/new`` → (optional) ``session/prompt``. Incoming agent→client
    requests (permission prompts, ``session/update`` notifications) are answered
    conservatively or ignored so the handshake can complete. The process is always
    terminated in :meth:`close`."""

    def __init__(self, argv: list[str], *, cwd: str, timeout: float = _SESSION_TIMEOUT) -> None:
        self._argv = argv
        self._cwd = cwd
        self._timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0

    def __enter__(self) -> "_AcpStdioClient":
        self._proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, allowlisted
            self._argv, cwd=self._cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=_minimal_env())
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _send(self, method: str, params: dict, *, is_request: bool = True,
              _id: Optional[int] = None) -> None:
        assert self._proc and self._proc.stdin
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if is_request:
            msg["id"] = _id
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        """Send a request and pump incoming lines until its response arrives."""
        assert self._proc and self._proc.stdout
        self._next_id += 1
        rid = self._next_id
        self._send(method, params, is_request=True, _id=rid)
        import time
        deadline = time.monotonic() + self._timeout
        while True:
            if time.monotonic() > deadline:
                raise OrchestratorUnavailable(
                    f"Hermes Agent did not respond to {method} within {self._timeout:.0f}s.")
            line = self._proc.stdout.readline()
            if line == "":
                raise OrchestratorUnavailable(
                    "Hermes Agent closed the ACP connection unexpectedly.")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A response to our request.
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    err = msg["error"] or {}
                    raise OrchestratorUnavailable(
                        f"Hermes Agent rejected {method} ({str(err.get('message'))[:160]}).")
                return msg.get("result") or {}
            # An agent→client REQUEST (has method + id) — answer minimally so the
            # agent isn't blocked. Deny permission prompts (we cannot prompt here);
            # ack everything else with an empty result.
            if "method" in msg and "id" in msg:
                inbound = str(msg.get("method") or "")
                result: dict[str, Any] = {}
                if inbound.endswith("request_permission"):
                    result = {"outcome": {"outcome": "cancelled"}}
                self._send_response(msg["id"], result)
            # Notifications (session/update, etc.) are informational — ignore.

    def _send_response(self, _id: Any, result: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _id, "result": result}) + "\n")
        self._proc.stdin.flush()

    def initialize(self) -> dict:
        return self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "openmontage", "version": "1.0.0"},
        })

    def new_session(self, cwd: str) -> str:
        res = self._request("session/new", {"cwd": cwd, "mcpServers": []})
        sid = res.get("sessionId") or res.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise OrchestratorUnavailable("Hermes Agent returned no session id.")
        return sid

    def prompt(self, session_id: str, text: str) -> dict:
        """Deliver the production instruction as a REAL request and return Hermes's
        authoritative ``PromptResponse`` (``{stopReason: ...}``).

        This blocks (bounded by ``timeout``) until Hermes finishes the turn — so a
        caller must only use it from a runner that keeps the ACP process alive for
        the run's duration. A refusal / error / timeout raises
        :class:`OrchestratorUnavailable` (fail closed), never a silent success."""
        res = self._request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        })
        stop = res.get("stopReason") or res.get("stop_reason")
        if stop == "refusal":
            raise OrchestratorUnavailable("Hermes Agent refused the production prompt.")
        return res


def _default_session_factory(cwd: str, instruction: str, *, argv: list[str]) -> dict:
    """The default (ephemeral-probe) run starter — deliberately **fail-closed**.

    A run started over ACP only stays alive while the Hermes process that owns its
    session is alive. This default transport is an ephemeral probe: it cannot keep
    the Hermes process running for the length of a production, so it must NOT return
    a session id that callers would persist as an *active* run while Hermes has
    already exited (that would fabricate success). Native in-app run *execution*
    therefore fails closed here — the agent is still genuinely detected, verified,
    and connected (see :class:`HermesAgentDetector` / :func:`agent_status`), and the
    real ACP session/prompt/cancel machinery in :class:`_AcpStdioClient` is exercised
    by the gated live smoke and by tests through an injected ``session_factory``.

    A future durable session runner (one that supervises a long-lived ACP process
    and streams ``session/update``) is wired by injecting ``session_factory`` into
    :class:`NativeHermesAgentClient`; it should call ``_AcpStdioClient.initialize`` →
    ``new_session`` → ``prompt`` (a real request) and keep the process alive.
    """
    raise OrchestratorUnavailable(
        "Native Hermes Agent run execution requires a durable ACP session runner, "
        "which is not enabled in this build. The Hermes Agent is detected and ready "
        "— you can drive this production from the Hermes Agent directly, and manual "
        "timeline editing / rendering remain available here. No run was opened.")


# --------------------------------------------------------------------------- #
# Native orchestration client (implements the HermesOrchestratorClient port)
# --------------------------------------------------------------------------- #
class NativeHermesAgentClient:
    """Opens real Hermes ACP sessions to back OpenMontage production runs.

    ``available()`` is True only when the local Hermes Agent is detected, verified
    ready, and enabled for this workspace. ``create_job`` opens a genuine session
    (Hermes returns the id) scoped to the OpenMontage repo; the returned handle's
    ``session_id`` is Hermes's own id, never fabricated. ``cancel_job`` cancels the
    session. Retry/resume are not native ACP concepts and fail closed honestly.
    """

    kind = "live"
    engine = AGENT_ENGINE

    def __init__(self, *, detector: Optional[HermesAgentDetector] = None,
                 enabled: bool = True, project_cwd: Optional[str] = None,
                 base_dir: Optional[Path] = None,
                 session_factory: Optional[Callable[..., dict]] = None,
                 canceller: Optional[Callable[[str], None]] = None) -> None:
        self._detector = detector or HermesAgentDetector()
        self._enabled = enabled
        self._cwd = str(project_cwd or REPO_ROOT)
        self._base_dir = base_dir
        self._session_factory = session_factory
        self._canceller = canceller
        self._ready_cache: Optional[dict] = None

    def _readiness(self) -> dict:
        if self._ready_cache is None:
            self._ready_cache = self._detector.verify()
        return self._ready_cache

    def available(self) -> bool:
        try:
            return bool(self._enabled) and bool(self._readiness().get("ready"))
        except Exception:
            return False

    def create_job(self, *, project_id: str, run_id: str,
                   requested_duration_seconds: Optional[int], idempotency_key: str) -> OrchestratorHandle:
        if not self.available():
            raise OrchestratorUnavailable(
                "The Hermes Agent integration is not configured on this machine. "
                "Detect and connect the local Hermes Agent in Studio to start production.")
        # Idempotency: a persisted handle for this exact key means the session was
        # already created — never open a second one.
        store = _session_store(self._base_dir)
        existing = store.get(idempotency_key)
        if existing and is_canonical_id(existing.get("session_id")):
            return _handle_from(existing)

        det = self._detector.detect()
        argv = det.get("launch")
        if not argv:
            raise OrchestratorUnavailable("Hermes Agent launch target is unavailable.")
        instruction = _instruction(project_id=project_id, run_id=run_id,
                                    requested_duration_seconds=requested_duration_seconds)
        factory = self._session_factory or (
            lambda cwd, instr: _default_session_factory(cwd, instr, argv=argv))
        result = factory(self._cwd, instruction)
        sid = (result or {}).get("sessionId") or (result or {}).get("session_id")
        if not is_canonical_id(sid):
            raise OrchestratorUnavailable(
                "Hermes Agent returned a non-canonical session id; refusing to open a run.")
        handle = OrchestratorHandle(
            session_id=sid, job_id=sid, engine=AGENT_ENGINE,
            detail="native Hermes Agent session")
        store.put(idempotency_key, {"session_id": sid, "job_id": sid,
                                    "project_id": project_id, "run_id": run_id})
        return handle

    def cancel_job(self, *, job_id: str) -> None:
        if not is_canonical_id(job_id):
            raise OrchestratorUnavailable("refusing to cancel a non-canonical session id")
        canceller = self._canceller or self._default_cancel
        canceller(job_id)

    def _default_cancel(self, session_id: str) -> None:
        # ``session/cancel`` only means anything to the Hermes process that owns the
        # LIVE session. A fresh ephemeral process has no handle on that run, and the
        # notification is unacknowledged — so we cannot truthfully confirm the cancel
        # from here. Fail closed (raise) so callers report the cancellation as
        # UNCONFIRMED / retryable rather than marking the run terminally cancelled.
        # A durable runner injects a ``canceller`` that cancels the live session and
        # confirms it.
        raise OrchestratorUnavailable(
            "Hermes Agent cancellation could not be confirmed: there is no durable "
            "Hermes session runner to deliver and acknowledge session/cancel against "
            "the live run. Cancel the run from the Hermes Agent directly.")

    def control_job(self, *, job_id: str, action: str, idempotency_key: str) -> None:
        if action == "cancel":
            self.cancel_job(job_id=job_id)
            return
        raise OrchestratorUnavailable(
            f"The native Hermes Agent does not support '{action}'. Steer the run from "
            "the Hermes Agent itself.")


class _UnavailableAgentClient:
    """Fail-closed client used when Hermes Agent is not ready. ``available()``
    False so the adapter refuses to open a run (honest blocker, no external fallback)."""

    kind = "live"
    engine = AGENT_ENGINE

    def available(self) -> bool:
        return False

    def create_job(self, **_: Any) -> OrchestratorHandle:
        raise OrchestratorUnavailable(
            "The Hermes Agent integration is not configured on this machine.")

    def cancel_job(self, **_: Any) -> None:
        raise OrchestratorUnavailable("The Hermes Agent integration is not configured.")

    def control_job(self, **_: Any) -> None:
        raise OrchestratorUnavailable("The Hermes Agent integration is not configured.")


# --------------------------------------------------------------------------- #
# Instruction + durable session handle
# --------------------------------------------------------------------------- #
def _instruction(*, project_id: str, run_id: str,
                 requested_duration_seconds: Optional[int]) -> str:
    dur = (f" The requested duration is {int(requested_duration_seconds)} seconds."
           if requested_duration_seconds else "")
    return (
        f"OM-RUN:{run_id} Operate this OpenMontage workspace and produce the video "
        f"project '{project_id}'.{dur} Follow AGENT_GUIDE.md: pick the pipeline, run "
        "preflight, and drive the production stages with checkpoints and approvals. "
        "Write all artifacts and assets under projects/" + project_id + "/.")


def _config_dir(base_dir: Optional[Path]) -> Path:
    return Path(base_dir or REPO_ROOT) / _CONFIG_DIRNAME


class _SessionStore:
    """Durable, non-secret map of idempotency-key → session handle (restart-safe)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def get(self, key: str) -> Optional[dict]:
        val = self._read().get(key)
        return val if isinstance(val, dict) else None

    def put(self, key: str, value: dict) -> None:
        data = self._read()
        data[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._path)


def _session_store(base_dir: Optional[Path]) -> _SessionStore:
    return _SessionStore(_config_dir(base_dir) / _SESSION_FILENAME)


def _handle_from(d: dict) -> OrchestratorHandle:
    return OrchestratorHandle(session_id=d["session_id"], job_id=d.get("job_id") or d["session_id"],
                              engine=AGENT_ENGINE, detail="native Hermes Agent session")


# --------------------------------------------------------------------------- #
# Enable/disable config (no credentials — just a per-workspace opt-in)
# --------------------------------------------------------------------------- #
def _config_path(base_dir: Optional[Path]) -> Path:
    return _config_dir(base_dir) / _CONFIG_FILENAME


def _read_config(base_dir: Optional[Path]) -> dict:
    try:
        data = json.loads(_config_path(base_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_config(base_dir: Optional[Path], payload: dict) -> None:
    d = _config_dir(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = _config_path(base_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(_config_path(base_dir))


def is_enabled(base_dir: Optional[Path] = None) -> bool:
    return bool(_read_config(base_dir).get("enabled"))


# --------------------------------------------------------------------------- #
# Public status + connect/disconnect + live client build
# --------------------------------------------------------------------------- #
def agent_status(*, detector: Optional[HermesAgentDetector] = None,
                 base_dir: Optional[Path] = None, probe: bool = True) -> dict:
    """Plain-language, actionable Hermes Agent connection view for the UI.

    NEVER contains an endpoint, token, project, or job field. ``status`` ∈
    {not_installed, detected, ready, connected}."""
    det = detector or HermesAgentDetector()
    enabled = is_enabled(base_dir)

    def _view(status, available, headline, detail, actions, **extra):
        return {"kind": "hermes_agent", "status": status, "available": bool(available),
                "server_name": AGENT_DISPLAY_NAME, "headline": headline,
                "detail": detail, "actions": actions, "enabled": enabled, **extra}

    detected = det.detect()
    if not detected["installed"]:
        return _view("not_installed", False,
                     "Hermes Agent is not installed",
                     "OpenMontage is operated natively by the Hermes Agent. Install "
                     "Hermes locally to enable agent-driven production. You can keep "
                     "editing the timeline manually meanwhile.",
                     [{"id": "retry_detect", "label": "Re-check for Hermes"}],
                     installed=False, ready=False, version=None)

    if not probe:
        return _view("detected", False, "Hermes Agent detected",
                     "The local Hermes Agent is installed.",
                     [{"id": "connect_agent", "label": "Connect Hermes Agent"}],
                     installed=True, ready=False, version=None)

    v = det.verify()
    if not v["ready"]:
        return _view("detected", False, "Hermes Agent found, finishing setup",
                     v.get("detail") or "Hermes is installed but its agent surface isn't "
                     "ready yet. Complete Hermes setup, then re-check.",
                     [{"id": "retry_detect", "label": "Re-check readiness"}],
                     installed=True, ready=False, version=v.get("version"))

    if not enabled:
        return _view("ready", False, "Hermes Agent is ready — connect to enable production",
                     "The local Hermes Agent is installed and ready. Connect it to this "
                     "workspace to let it drive production.",
                     [{"id": "connect_agent", "label": "Connect Hermes Agent"}],
                     installed=True, ready=True, version=v.get("version"))

    return _view("connected", True, f"{AGENT_DISPLAY_NAME} connected",
                 "Hermes Agent is connected and can drive production for this workspace.",
                 [{"id": "disconnect_agent", "label": "Disconnect"}],
                 installed=True, ready=True, version=v.get("version"))


def connect(*, detector: Optional[HermesAgentDetector] = None,
            base_dir: Optional[Path] = None) -> dict:
    """Enable the native Hermes Agent for this workspace (no credentials).

    Verifies the agent is genuinely launchable first; a failed verify does NOT
    enable (fail closed). Returns the resulting :func:`agent_status` view."""
    det = detector or HermesAgentDetector()
    v = det.verify()
    if not v["ready"]:
        return agent_status(detector=det, base_dir=base_dir, probe=True)
    _write_config(base_dir, {"enabled": True, "version": v.get("version")})
    return agent_status(detector=det, base_dir=base_dir, probe=True)


def disconnect(*, base_dir: Optional[Path] = None) -> dict:
    try:
        _config_path(base_dir).unlink()
    except OSError:
        pass
    return agent_status(base_dir=base_dir, probe=False)


def build_live_client(*, base_dir: Optional[Path] = None,
                      detector: Optional[HermesAgentDetector] = None,
                      session_factory: Optional[Callable[..., dict]] = None,
                      canceller: Optional[Callable[[str], None]] = None):
    """Build the orchestration client the brain adapter uses.

    A ready+enabled Hermes Agent → :class:`NativeHermesAgentClient` (real ACP
    sessions). Otherwise a fail-closed :class:`_UnavailableAgentClient` so Start
    Production surfaces an honest blocker — never a non-native fallback."""
    det = detector or HermesAgentDetector()
    enabled = is_enabled(base_dir)
    if enabled and det.verify().get("ready"):
        return NativeHermesAgentClient(
            detector=det, enabled=True, base_dir=base_dir,
            session_factory=session_factory, canceller=canceller)
    return _UnavailableAgentClient()
