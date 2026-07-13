"""Native Hermes Agent integration — detection, readiness, status, session start.

Replaces the deleted Mochlet/MCP connection suite. Everything here is offline and
hermetic: no real process is ever spawned (an injected ``runner`` and
``session_factory``/``canceller`` stand in for the transport), and the real
``~/.hermes`` is never touched — ``tmp_path`` is used as both ``home`` and
``base_dir``.

Contract under test (``lib.production_brain.hermes_agent``):
  * ``HermesAgentDetector.detect()`` — filesystem-only install detection.
  * ``.verify()`` — bounded, side-effect-free readiness probe via ``--check`` /
    ``--version``.
  * ``agent_status()`` — the plain-language connection view (NEVER an endpoint,
    token, project, or job).
  * ``connect()`` / ``disconnect()`` / ``is_enabled()`` — per-workspace opt-in
    that fails closed on a not-ready verify.
  * ``NativeHermesAgentClient`` — the orchestration port backed by a real ACP
    session id (never fabricated), idempotent, canonical-id enforced.
  * ``build_live_client()`` — ready+enabled → native client, else fail-closed.
"""

from __future__ import annotations

import pytest

from lib.paths import REPO_ROOT
from lib.production_brain import hermes_agent as HA
from lib.production_brain.hermes_agent import (
    HermesAgentDetector,
    NativeHermesAgentClient,
    _UnavailableAgentClient,
    agent_status,
    build_live_client,
    connect,
    disconnect,
    is_enabled,
)
from lib.production_brain.orchestrator import (
    OrchestratorHandle,
    OrchestratorUnavailable,
)

# A canonical (allowlist-safe) session id Hermes would return.
CANON_SID = "hermes-sess-abc123"


# --------------------------------------------------------------------------- #
# Fakes: a filesystem install layout + injectable runner / session factory
# --------------------------------------------------------------------------- #
def _install_hermes(home):
    """Build a fake ``<home>/hermes-agent`` install so detect() reports installed:
    a venv python + the ``acp_adapter`` package. Returns the agent dir."""
    agent = home / "hermes-agent"
    bin0 = agent / "venv" / "bin"
    bin0.mkdir(parents=True, exist_ok=True)
    (bin0 / "python").write_text("#!/bin/sh\n")
    pkg = agent / "acp_adapter"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    return agent


class _Runner:
    """Records every argv it is asked to run and replays scripted responses keyed
    by the trailing flag (``--check`` / ``--version``). NEVER spawns a process."""

    def __init__(self, *, check=(0, "", ""), version=(0, "1.2.3", "")):
        self._check = check
        self._version = version
        self.calls = []

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        if argv and argv[-1] == "--check":
            return self._check
        if argv and argv[-1] == "--version":
            return self._version
        return (0, "", "")


def _ready_detector(home):
    """A detector over a fake install whose runner reports ready + version 1.2.3."""
    _install_hermes(home)
    return HermesAgentDetector(home=home, runner=_Runner())


class _RecordingFactory:
    """A session_factory replacement — records (cwd, instruction) and returns a
    scripted session id. NEVER opens a real ACP transport."""

    def __init__(self, session_id=CANON_SID):
        self._sid = session_id
        self.calls = []

    def __call__(self, cwd, instruction):
        self.calls.append({"cwd": cwd, "instruction": instruction})
        return {"sessionId": self._sid}


# --------------------------------------------------------------------------- #
# detect()
# --------------------------------------------------------------------------- #
def test_detect_not_installed_on_empty_home(tmp_path):
    det = HermesAgentDetector(home=tmp_path, runner=_Runner())
    d = det.detect()
    assert d["installed"] is False
    assert d["launch"] is None
    assert d["home"] == str(tmp_path)
    assert d["reason"]  # an actionable reason string


def test_detect_installed_with_full_layout(tmp_path):
    agent = _install_hermes(tmp_path)
    det = HermesAgentDetector(home=tmp_path, runner=_Runner())
    d = det.detect()
    assert d["installed"] is True
    # launch is an ARGV LIST (never a shell string), pointing at the venv python.
    assert isinstance(d["launch"], list)
    assert d["launch"][-2:] == ["-m", "acp_adapter"]
    assert d["launch"][0] == str(agent / "venv" / "bin" / "python")
    assert det.launch_argv() == d["launch"]


def test_detect_needs_both_venv_and_adapter(tmp_path):
    # venv python present but acp_adapter package missing → not installed.
    agent = tmp_path / "hermes-agent"
    bin0 = agent / "venv" / "bin"
    bin0.mkdir(parents=True)
    (bin0 / "python").write_text("")
    det = HermesAgentDetector(home=tmp_path, runner=_Runner())
    assert det.detect()["installed"] is False
    assert det.launch_argv() is None


# --------------------------------------------------------------------------- #
# verify()
# --------------------------------------------------------------------------- #
def test_verify_ready_when_check_and_version_ok(tmp_path):
    _install_hermes(tmp_path)
    runner = _Runner(check=(0, "", ""), version=(0, "1.2.3", ""))
    v = HermesAgentDetector(home=tmp_path, runner=runner).verify()
    assert v["installed"] is True and v["ready"] is True
    assert v["version"] == "1.2.3"
    # It probed --check then --version, never anything else.
    assert [c["argv"][-1] for c in runner.calls] == ["--check", "--version"]


def test_verify_not_ready_when_check_fails(tmp_path):
    _install_hermes(tmp_path)
    runner = _Runner(check=(1, "", "adapter import error"))
    v = HermesAgentDetector(home=tmp_path, runner=runner).verify()
    assert v["ready"] is False and v["version"] is None
    assert v["installed"] is True
    # --version is never probed once --check has failed.
    assert [c["argv"][-1] for c in runner.calls] == ["--check"]


def test_verify_not_installed_short_circuits(tmp_path):
    runner = _Runner()
    v = HermesAgentDetector(home=tmp_path, runner=runner).verify()
    assert v == {"installed": False, "ready": False, "version": None,
                 "detail": v["detail"]}
    assert runner.calls == []  # never probed


# --------------------------------------------------------------------------- #
# agent_status() — the UI connection view (no endpoint/token/project ever)
# --------------------------------------------------------------------------- #
_FORBIDDEN_KEYS = ("endpoint", "token", "project", "projects", "job", "url")


def _assert_no_secrets(view):
    for k in _FORBIDDEN_KEYS:
        assert k not in view, f"status view leaked forbidden key {k!r}"
    # And no forbidden substring anywhere in the serialized view.
    blob = repr(view).lower()
    for needle in ("endpoint", "token", "mochlet"):
        assert needle not in blob


def test_status_not_installed(tmp_path):
    det = HermesAgentDetector(home=tmp_path, runner=_Runner())
    v = agent_status(detector=det, base_dir=tmp_path)
    assert v["kind"] == "hermes_agent"
    assert v["status"] == "not_installed"
    assert v["available"] is False
    assert v["installed"] is False and v["ready"] is False
    assert v["server_name"] == "Hermes Agent"
    assert any(a["id"] == "retry_detect" for a in v["actions"])
    _assert_no_secrets(v)


def test_status_detected_not_ready(tmp_path):
    _install_hermes(tmp_path)
    det = HermesAgentDetector(home=tmp_path, runner=_Runner(check=(1, "", "boom")))
    v = agent_status(detector=det, base_dir=tmp_path)
    assert v["status"] == "detected"
    assert v["available"] is False
    assert v["installed"] is True and v["ready"] is False
    _assert_no_secrets(v)


def test_status_ready_but_not_enabled(tmp_path):
    det = _ready_detector(tmp_path)
    v = agent_status(detector=det, base_dir=tmp_path)
    assert v["status"] == "ready"
    assert v["available"] is False  # ready but not connected/enabled
    assert v["installed"] is True and v["ready"] is True
    assert v["enabled"] is False
    assert v["version"] == "1.2.3"
    assert any(a["id"] == "connect_agent" for a in v["actions"])
    _assert_no_secrets(v)


def test_status_connected_when_ready_and_enabled(tmp_path):
    det = _ready_detector(tmp_path)
    connect(detector=det, base_dir=tmp_path)
    v = agent_status(detector=det, base_dir=tmp_path)
    assert v["status"] == "connected"
    assert v["available"] is True
    assert v["enabled"] is True
    assert any(a["id"] == "disconnect_agent" for a in v["actions"])
    _assert_no_secrets(v)


# --------------------------------------------------------------------------- #
# connect() / disconnect() / is_enabled() — fail-closed enablement
# --------------------------------------------------------------------------- #
def test_connect_enables_only_when_ready(tmp_path):
    det = _ready_detector(tmp_path)
    assert is_enabled(base_dir=tmp_path) is False
    v = connect(detector=det, base_dir=tmp_path)
    assert v["status"] == "connected" and v["available"] is True
    assert is_enabled(base_dir=tmp_path) is True


def test_connect_does_not_enable_when_not_ready(tmp_path):
    _install_hermes(tmp_path)
    det = HermesAgentDetector(home=tmp_path, runner=_Runner(check=(1, "", "not ready")))
    v = connect(detector=det, base_dir=tmp_path)
    # Fail closed: a not-ready verify NEVER enables the workspace.
    assert v["available"] is False
    assert is_enabled(base_dir=tmp_path) is False


def test_disconnect_forgets_enablement(tmp_path):
    det = _ready_detector(tmp_path)
    connect(detector=det, base_dir=tmp_path)
    assert is_enabled(base_dir=tmp_path) is True
    disconnect(base_dir=tmp_path)
    assert is_enabled(base_dir=tmp_path) is False


# --------------------------------------------------------------------------- #
# NativeHermesAgentClient — the orchestration port
# --------------------------------------------------------------------------- #
def _native(tmp_path, *, factory=None, canceller=None, enabled=True, detector=None):
    det = detector or _ready_detector(tmp_path)
    return NativeHermesAgentClient(
        detector=det, enabled=enabled, base_dir=tmp_path,
        session_factory=factory, canceller=canceller)


def test_client_available_gating(tmp_path):
    # ready + enabled → available
    assert _native(tmp_path, enabled=True).available() is True
    # ready but not enabled → unavailable
    assert _native(tmp_path, enabled=False).available() is False
    # enabled but NOT ready → unavailable
    _install_hermes(tmp_path)
    not_ready = HermesAgentDetector(home=tmp_path, runner=_Runner(check=(1, "", "x")))
    assert _native(tmp_path, detector=not_ready, enabled=True).available() is False


def test_client_kind_and_engine():
    assert NativeHermesAgentClient.kind == "live"
    assert NativeHermesAgentClient.engine == "hermes-agent"


def test_create_job_returns_factory_id_as_canonical_handle(tmp_path):
    factory = _RecordingFactory(CANON_SID)
    client = _native(tmp_path, factory=factory)
    h = client.create_job(project_id="demo", run_id="run_1",
                          requested_duration_seconds=90, idempotency_key="demo:run_1")
    assert isinstance(h, OrchestratorHandle) and h.is_valid()
    assert h.session_id == CANON_SID and h.job_id == CANON_SID
    assert h.engine == "hermes-agent"
    assert len(factory.calls) == 1


def test_create_job_uses_repo_root_cwd_not_caller_input(tmp_path):
    # SECURITY: the working directory is bound to REPO_ROOT (default project_cwd),
    # NOT to any caller-supplied text like project_id / run_id.
    factory = _RecordingFactory()
    client = _native(tmp_path, factory=factory)
    client.create_job(project_id="../../etc/passwd", run_id="; rm -rf /",
                      requested_duration_seconds=None, idempotency_key="k")
    call = factory.calls[0]
    assert call["cwd"] == str(REPO_ROOT)
    # The instruction is passed as text/argv payload — never composed into cwd.
    assert "../../etc/passwd" not in call["cwd"]


def test_create_job_is_idempotent_on_key(tmp_path):
    factory = _RecordingFactory(CANON_SID)
    client = _native(tmp_path, factory=factory)
    a = client.create_job(project_id="demo", run_id="run_1",
                          requested_duration_seconds=60, idempotency_key="demo:run_1")
    b = client.create_job(project_id="demo", run_id="run_1",
                          requested_duration_seconds=60, idempotency_key="demo:run_1")
    # The persisted handle is returned; the factory is NOT called a second time.
    assert a.session_id == b.session_id == CANON_SID
    assert len(factory.calls) == 1


def test_create_job_rejects_non_canonical_session_id(tmp_path):
    factory = _RecordingFactory(session_id="../escape")
    client = _native(tmp_path, factory=factory)
    with pytest.raises(OrchestratorUnavailable):
        client.create_job(project_id="demo", run_id="run_1",
                          requested_duration_seconds=60, idempotency_key="k")


def test_create_job_unavailable_client_raises(tmp_path):
    factory = _RecordingFactory()
    client = _native(tmp_path, factory=factory, enabled=False)  # not enabled → unavailable
    with pytest.raises(OrchestratorUnavailable):
        client.create_job(project_id="demo", run_id="run_1",
                          requested_duration_seconds=60, idempotency_key="k")
    assert factory.calls == []  # never reached the transport


def test_cancel_job_calls_canceller_with_id(tmp_path):
    seen = []
    client = _native(tmp_path, canceller=lambda jid: seen.append(jid))
    client.cancel_job(job_id=CANON_SID)
    assert seen == [CANON_SID]


def test_cancel_job_rejects_non_canonical_id(tmp_path):
    seen = []
    client = _native(tmp_path, canceller=lambda jid: seen.append(jid))
    with pytest.raises(OrchestratorUnavailable):
        client.cancel_job(job_id="../evil")
    assert seen == []  # canceller never called with an unsafe id


def test_control_job_cancel_cancels(tmp_path):
    seen = []
    client = _native(tmp_path, canceller=lambda jid: seen.append(jid))
    client.control_job(job_id=CANON_SID, action="cancel", idempotency_key="k")
    assert seen == [CANON_SID]


def test_control_job_non_cancel_raises(tmp_path):
    client = _native(tmp_path, canceller=lambda jid: None)
    for action in ("retry", "resume"):
        with pytest.raises(OrchestratorUnavailable):
            client.control_job(job_id=CANON_SID, action=action, idempotency_key="k")


# --------------------------------------------------------------------------- #
# SECURITY: no shell, no credentials, argv-only launch
# --------------------------------------------------------------------------- #
def test_launch_is_argv_list_never_shell(tmp_path):
    agent = _install_hermes(tmp_path)
    det = HermesAgentDetector(home=tmp_path, runner=_Runner())
    argv = det.launch_argv()
    # A LIST (execve-style), so no user text can be interpreted by a shell.
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)
    assert argv[0] == str(agent / "venv" / "bin" / "python")


def test_no_credential_or_keyring_account_for_the_agent(tmp_path):
    # The native agent surface is loopback stdio and reads NO credential. There is
    # no keyring account, no token env var, nothing to exfiltrate. Assert the
    # module exposes no token/account/keyring symbol.
    names = [n.lower() for n in dir(HA)]
    for bad in ("token", "keyring", "account", "secret", "bearer"):
        assert not any(bad in n for n in names), f"unexpected credential symbol: {bad}"


# --------------------------------------------------------------------------- #
# build_live_client — ready+enabled → native; else fail-closed
# --------------------------------------------------------------------------- #
def test_build_live_client_native_when_ready_and_enabled(tmp_path):
    det = _ready_detector(tmp_path)
    connect(detector=det, base_dir=tmp_path)
    client = build_live_client(base_dir=tmp_path, detector=det,
                               session_factory=_RecordingFactory(), canceller=lambda j: None)
    assert isinstance(client, NativeHermesAgentClient)
    assert client.available() is True


def test_build_live_client_unavailable_when_not_enabled(tmp_path):
    det = _ready_detector(tmp_path)  # ready but never connected → not enabled
    client = build_live_client(base_dir=tmp_path, detector=det)
    assert isinstance(client, _UnavailableAgentClient)
    assert client.available() is False
    with pytest.raises(OrchestratorUnavailable):
        client.create_job(project_id="p", run_id="r",
                          requested_duration_seconds=1, idempotency_key="k")


def test_build_live_client_unavailable_when_not_ready(tmp_path):
    _install_hermes(tmp_path)
    not_ready = HermesAgentDetector(home=tmp_path, runner=_Runner(check=(1, "", "x")))
    # Even if a stale config enabled it, a not-ready verify fails closed.
    HA._write_config(tmp_path, {"enabled": True})
    client = build_live_client(base_dir=tmp_path, detector=not_ready)
    assert isinstance(client, _UnavailableAgentClient)
