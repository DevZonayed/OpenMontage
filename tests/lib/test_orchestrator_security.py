"""Security hardening of the orchestration port + the native Hermes Agent adapter.

The legacy attack surface (HTTPS endpoint policy, bearer-token replay on redirect,
percent-encoded URL paths) is GONE with Mochlet. The native Hermes Agent surface
is loopback stdio, spawned with an argv list, reading no credential — so the
security contract is now:

  * canonical-id enforcement: external ids are validated against a strict, bounded
    allowlist (no traversal / control / whitespace, length-bounded) before they are
    ever persisted or used — a non-string is NEVER str()-coerced;
  * never fabricate identity: the native adapter refuses to open a run unless a
    real, canonical session id is returned; a non-canonical id fails closed;
  * fail closed: an unavailable / not-ready agent raises OrchestratorUnavailable
    rather than inventing a session;
  * no shell / no user text as a command: the session is opened with the working
    directory bound to the validated repo root, never to caller-supplied text;
  * no credential is read anywhere — there is no keyring account for the agent.
"""

from __future__ import annotations

import pytest

from lib.paths import REPO_ROOT
from lib.production_brain import hermes_agent as HA
from lib.production_brain.hermes_agent import (
    HermesAgentDetector,
    NativeHermesAgentClient,
)
from lib.production_brain.orchestrator import (
    OrchestratorHandle,
    OrchestratorUnavailable,
    is_canonical_id,
)


# --------------------------------------------------------------------------- #
# Fakes (offline; never spawn a process, never touch ~/.hermes)
# --------------------------------------------------------------------------- #
def _install(home):
    agent = home / "hermes-agent"
    (agent / "venv" / "bin").mkdir(parents=True)
    (agent / "venv" / "bin" / "python").write_text("")
    (agent / "acp_adapter").mkdir(parents=True)
    (agent / "acp_adapter" / "__init__.py").write_text("")
    return agent


def _ready_runner(argv, *, cwd=None, timeout=None):
    if argv and argv[-1] == "--check":
        return (0, "", "")
    if argv and argv[-1] == "--version":
        return (0, "9.9.9", "")
    return (0, "", "")


def _ready_client(tmp_path, *, factory=None, canceller=None):
    _install(tmp_path)
    det = HermesAgentDetector(home=tmp_path, runner=_ready_runner)
    return NativeHermesAgentClient(detector=det, enabled=True, base_dir=tmp_path,
                                   session_factory=factory, canceller=canceller)


class _Factory:
    def __init__(self, session_id):
        self._sid = session_id
        self.calls = []

    def __call__(self, cwd, instruction):
        self.calls.append({"cwd": cwd, "instruction": instruction})
        return {"sessionId": self._sid}


# --------------------------------------------------------------------------- #
# Canonical id validation + handle
# --------------------------------------------------------------------------- #
class TestCanonicalIds:
    @pytest.mark.parametrize("val", ["job-1", "sess_ABC.9", "run:123", "a" * 128])
    def test_valid(self, val):
        assert is_canonical_id(val) is True

    @pytest.mark.parametrize("val", [
        "job/1", "job\\1", "..", "a/../b", "job..1", "job%2f1", "job\n1",
        "job 1", "a" * 129, "", 123, None, {"x": 1},
    ])
    def test_invalid(self, val):
        assert is_canonical_id(val) is False

    def test_handle_is_valid_checks_both(self):
        assert OrchestratorHandle("sess-1", "job-1").is_valid() is True
        assert OrchestratorHandle("sess/1", "job-1").is_valid() is False
        assert OrchestratorHandle("sess-1", "job/1").is_valid() is False


# --------------------------------------------------------------------------- #
# Never fabricate identity — the native adapter enforces canonical ids
# --------------------------------------------------------------------------- #
class TestNeverFabricateIdentity:
    @pytest.mark.parametrize("bad", ["../escape", "sess/1", "sess\n1", "sess 1", "", "a" * 200])
    def test_non_canonical_session_id_fails_closed(self, tmp_path, bad):
        factory = _Factory(bad)
        client = _ready_client(tmp_path, factory=factory)
        with pytest.raises(OrchestratorUnavailable):
            client.create_job(project_id="p", run_id="r",
                              requested_duration_seconds=60, idempotency_key="k")

    def test_canonical_session_id_is_recorded_verbatim(self, tmp_path):
        factory = _Factory("hermes-sess-9")
        client = _ready_client(tmp_path, factory=factory)
        h = client.create_job(project_id="p", run_id="r",
                              requested_duration_seconds=60, idempotency_key="k")
        # The id is exactly what Hermes returned — never minted locally.
        assert h.session_id == "hermes-sess-9" and h.job_id == "hermes-sess-9"
        assert h.is_valid()

    def test_non_string_session_id_is_not_coerced(self, tmp_path):
        # A misbehaving factory returning a non-string must fail closed, never
        # str()-coerced into a fabricated id.
        class _BadFactory:
            def __call__(self, cwd, instruction):
                return {"sessionId": 12345}

        client = _ready_client(tmp_path, factory=_BadFactory())
        with pytest.raises(OrchestratorUnavailable):
            client.create_job(project_id="p", run_id="r",
                              requested_duration_seconds=60, idempotency_key="k")


# --------------------------------------------------------------------------- #
# Fail closed when unavailable / not ready
# --------------------------------------------------------------------------- #
class TestFailClosed:
    def test_disabled_agent_refuses_to_open_a_run(self, tmp_path):
        _install(tmp_path)
        det = HermesAgentDetector(home=tmp_path, runner=_ready_runner)
        factory = _Factory("hermes-sess-1")
        client = NativeHermesAgentClient(detector=det, enabled=False, base_dir=tmp_path,
                                         session_factory=factory)
        assert client.available() is False
        with pytest.raises(OrchestratorUnavailable):
            client.create_job(project_id="p", run_id="r",
                              requested_duration_seconds=60, idempotency_key="k")
        assert factory.calls == []  # the transport was never reached

    def test_not_ready_agent_refuses_to_open_a_run(self, tmp_path):
        _install(tmp_path)

        def failing(argv, *, cwd=None, timeout=None):
            return (1, "", "adapter import failed")

        det = HermesAgentDetector(home=tmp_path, runner=failing)
        factory = _Factory("hermes-sess-1")
        client = NativeHermesAgentClient(detector=det, enabled=True, base_dir=tmp_path,
                                         session_factory=factory)
        assert client.available() is False
        with pytest.raises(OrchestratorUnavailable):
            client.create_job(project_id="p", run_id="r",
                              requested_duration_seconds=60, idempotency_key="k")
        assert factory.calls == []


# --------------------------------------------------------------------------- #
# Control safety — cancel is canonical-gated; retry/resume are not native
# --------------------------------------------------------------------------- #
class TestControlSafety:
    def test_cancel_rejects_non_canonical_job_id_before_calling(self, tmp_path):
        seen = []
        client = _ready_client(tmp_path, canceller=lambda j: seen.append(j))
        with pytest.raises(OrchestratorUnavailable):
            client.cancel_job(job_id="job/../1")
        assert seen == []  # never dispatched a bad id to the transport

    def test_control_non_cancel_fails_closed(self, tmp_path):
        client = _ready_client(tmp_path, canceller=lambda j: None)
        for action in ("retry", "resume", "delete"):
            with pytest.raises(OrchestratorUnavailable):
                client.control_job(job_id="job-1", action=action, idempotency_key="k")


# --------------------------------------------------------------------------- #
# No shell / no user text as a command; no credential read
# --------------------------------------------------------------------------- #
class TestNoShellNoCredentials:
    def test_cwd_is_bound_to_repo_root_not_caller_input(self, tmp_path):
        # The session's working directory is the validated repo root, NEVER any
        # caller-supplied project_id / run_id text (which could carry traversal or
        # shell metacharacters).
        factory = _Factory("hermes-sess-1")
        client = _ready_client(tmp_path, factory=factory)
        client.create_job(project_id="../../etc", run_id="$(whoami)",
                          requested_duration_seconds=None, idempotency_key="k")
        assert factory.calls[0]["cwd"] == str(REPO_ROOT)

    def test_launch_target_is_argv_list_never_a_shell_string(self, tmp_path):
        agent = _install(tmp_path)
        det = HermesAgentDetector(home=tmp_path, runner=_ready_runner)
        argv = det.launch_argv()
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        # It is the install's OWN allowlisted venv python, under the Hermes home —
        # never an arbitrary PATH entry.
        assert argv[0] == str(agent / "venv" / "bin" / "python")
        assert str(tmp_path) in argv[0]

    def test_module_exposes_no_credential_surface(self):
        # There is no keyring account, token env var, or bearer for the agent.
        names = [n.lower() for n in dir(HA)]
        for bad in ("token", "keyring", "account", "bearer", "password"):
            assert not any(bad in n for n in names), f"unexpected credential symbol: {bad}"
