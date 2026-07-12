"""MochletMcpOrchestratorClient — real create/control against the fake MCP."""

from __future__ import annotations

import pytest

from lib.production_brain.mochlet import (
    JobIdempotencyStore,
    MochletMcpOrchestratorClient,
    MochletProjectError,
    is_uuid,
)
from lib.production_brain.orchestrator import OrchestratorUnavailable
from tests.production_status._fake_mcp import FakeMochletMcp

ENDPOINT = "http://127.0.0.1:9235/mcp"
PID = "669a5386-f37b-4c6f-a712-b12e8221e54d"


def _client(fake, tmp_path=None, project=PID):
    store = JobIdempotencyStore(tmp_path / "idem.json") if tmp_path else None
    return MochletMcpOrchestratorClient(
        endpoint=ENDPOINT, mochlet_project_id=project,
        project_path="/repo/the-electricity-bulb",
        transport=fake.transport, token_getter=lambda: fake.token,
        idempotency_store=store)


def test_create_job_sends_one_chat_and_returns_uuid_handles(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="the-electricity-bulb", run_id="run-1",
                     requested_duration_seconds=150, idempotency_key="k1")
    assert is_uuid(h.job_id) and is_uuid(h.session_id)
    assert len(fake.sent_chats) == 1
    # the instruction carries the run marker + canonical stage contract, not a title guess
    sent = fake.sent_chats[0]
    assert "OM-RUN:run-1" in sent["text"]
    assert sent["projectId"] == PID
    assert sent["agentContext"]["run_id"] == "run-1"


def test_create_job_is_idempotent_no_double_send(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h1 = c.create_job(project_id="p", run_id="run-1", requested_duration_seconds=60,
                      idempotency_key="k1")
    h2 = c.create_job(project_id="p", run_id="run-1", requested_duration_seconds=60,
                      idempotency_key="k1")
    assert h1.job_id == h2.job_id
    assert len(fake.sent_chats) == 1  # never double-sent


def test_indeterminate_empty_sendchat_recovers_via_listjobpage(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    real_tool = None

    # Make sendChat create the job but return an EMPTY payload (indeterminate).
    from lib.production_brain import mcp_client as mc

    orig_call = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        out = orig_call(self, name, arguments)
        if name == "sendChat":
            return {}  # empty/indeterminate — job still persisted server-side
        return out

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    h = c.create_job(project_id="p", run_id="run-9", requested_duration_seconds=60,
                     idempotency_key="k9")
    assert is_uuid(h.job_id)  # recovered from listJobPage, not fabricated


def test_missing_project_refuses(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path, project=None)
    with pytest.raises(MochletProjectError):
        c.create_job(project_id="p", run_id="r", requested_duration_seconds=60,
                     idempotency_key="k")


def test_cancel_calls_canceljob_with_exact_id(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    c.cancel_job(job_id=h.job_id)
    assert fake.cancelled == [h.job_id]


def test_retry_reruns_the_SAME_job(tmp_path):
    # Mochlet runJob re-runs the existing job; retry must return the SAME job id.
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    result = c.control_job(job_id=h.job_id, action="retry", idempotency_key="k:retry")
    assert result is not None
    assert result.job_id == h.job_id  # SAME job, not a fabricated successor
    assert fake.controls[-1]["action"] == "run"


def test_retry_refuses_when_job_stays_terminal(tmp_path, monkeypatch):
    # runJob returned, but getJob shows the job did NOT move to a runnable state.
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "getJob":
            return {"id": h.job_id, "status": "cancelled"}  # rerun didn't take
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.control_job(job_id=h.job_id, action="retry", idempotency_key="k:retry")


@pytest.mark.parametrize("getjob_return", [
    {"id": "12312312-1231-4231-8231-123123123123", "status": "running"},  # different id
    {"id": "11111111-1111-4111-8111-111111111111", "status": "unknown"},   # unknown status
    {"id": "11111111-1111-4111-8111-111111111111"},                        # empty status
    {"id": "11111111-1111-4111-8111-111111111111", "status": "running"},   # no session
])
def test_retry_fails_closed_on_unconfirmed_getjob(tmp_path, monkeypatch, getjob_return):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    # normalize the "different id" case to a real non-matching id
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "getJob":
            return getjob_return
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.control_job(job_id=h.job_id, action="retry", idempotency_key="k:retry")


def test_resume_fails_closed_on_missing_response_session(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "sendChat" and arguments.get("agentContext", {}).get("control") == "resume":
            return {"job": {"id": "88888888-8888-4888-8888-888888888888"}}  # NO session
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.control_job(job_id=h.job_id, action="resume", idempotency_key="k:resume")
    assert "88888888-8888-4888-8888-888888888888" in fake.cancelled  # compensated


def test_resume_creates_successor_via_sendchat_same_session(tmp_path):
    # Resume = sendChat on the exact session → NEW job, SAME session (not continueSession).
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    before = len(fake.sent_chats)
    successor = c.control_job(job_id=h.job_id, action="resume", idempotency_key="k:resume")
    assert successor is not None and is_uuid(successor.job_id)
    assert successor.job_id != h.job_id            # a real successor job
    assert successor.session_id == h.session_id    # SAME session
    # it went through sendChat (not continueSession), with a resume agentContext
    assert len(fake.sent_chats) == before + 1
    resume_chat = fake.sent_chats[-1]
    assert resume_chat["sessionId"] == h.session_id
    assert resume_chat["agentContext"]["control"] == "resume"
    assert not any(cc["action"] == "continue" for cc in fake.controls)


def test_resume_rejects_and_compensates_wrong_session(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "sendChat" and arguments.get("agentContext", {}).get("control") == "resume":
            # returns a job in a DIFFERENT (unrelated) session
            return {"session": {"id": "99999999-9999-4999-8999-999999999999"},
                    "job": {"id": "88888888-8888-4888-8888-888888888888"}}
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.control_job(job_id=h.job_id, action="resume", idempotency_key="k:resume")
    assert "88888888-8888-4888-8888-888888888888" in fake.cancelled  # compensated


def test_cancel_control_returns_none(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    assert c.control_job(job_id=h.job_id, action="cancel", idempotency_key="k:c") is None
    assert fake.cancelled == [h.job_id]


def test_list_projects_returns_ids(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    projects = c.list_projects()
    assert projects[0]["id"] == PID
    assert projects[0]["name"] == "the-electricity-bulb"


def test_non_uuid_job_from_server_is_refused(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    from lib.production_brain import mcp_client as mc

    def patched(self, name, arguments=None):
        if name == "sendChat":
            return {"session": {"id": "not-a-uuid"}, "job": {"id": "also-bad"}}
        if name == "listJobPage":
            return {"jobs": []}
        return mc.MochletMcpClient.__dict__["call_tool"]
    monkeypatch.setattr(mc.MochletMcpClient, "call_tool",
                        lambda self, n, a=None: patched(self, n, a))
    with pytest.raises(OrchestratorUnavailable):
        c.create_job(project_id="p", run_id="rz", requested_duration_seconds=60, idempotency_key="kz")


def test_retry_confirms_via_getjob_when_runjob_omits_id(tmp_path, monkeypatch):
    # runJob may return no explicit id; retry is confirmed via getJob (running) and
    # still returns the SAME job id.
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "runJob":
            return {}  # no id echoed — confirmation must come from getJob
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    result = c.control_job(job_id=h.job_id, action="retry", idempotency_key="k:retry")
    assert result.job_id == h.job_id  # same job, confirmed running via getJob


def test_retry_rejects_mismatched_runjob_id(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "runJob":
            return {"job": {"id": "77777777-7777-4777-8777-777777777777"}}  # WRONG id
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.control_job(job_id=h.job_id, action="retry", idempotency_key="k:retry")


def test_resume_without_valid_session_raises(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "getJob":
            return {"id": h.job_id, "status": "running"}  # no session id at all
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.control_job(job_id=h.job_id, action="resume", idempotency_key="k:resume")


def test_indeterminate_send_never_double_sends(tmp_path, monkeypatch):
    # First Start: sendChat errors AND no job is discoverable → pending recorded,
    # OrchestratorUnavailable raised. A retried Start must NOT send a second chat.
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    sends = {"n": 0}
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "sendChat":
            sends["n"] += 1
            from lib.production_brain.mcp_client import McpError
            raise McpError("network blip after accept")
        if name == "listJobPage":
            return {"jobs": []}  # marker not discoverable
        return orig(self, name, arguments)

    monkeypatch.setattr(mc.MochletMcpClient, "call_tool", patched)
    with pytest.raises(OrchestratorUnavailable):
        c.create_job(project_id="p", run_id="rd", requested_duration_seconds=60, idempotency_key="kd")
    with pytest.raises(OrchestratorUnavailable):
        c.create_job(project_id="p", run_id="rd", requested_duration_seconds=60, idempotency_key="kd")
    assert sends["n"] == 1  # exactly ONE real sendChat attempt, never a duplicate


class _FailingIdem:
    """Idempotency store that fails to persist the FINAL handle (post-send)."""

    def __init__(self):
        self.data = {}
        self.deleted = []

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        if value.get("job_id"):
            raise OSError("disk full")  # fails only for the real-handle persist
        self.data[key] = value

    def delete(self, key):
        self.deleted.append(key)
        self.data.pop(key, None)


def test_persist_failure_after_send_compensates_by_cancelling():
    fake = FakeMochletMcp()
    idem = _FailingIdem()
    c = MochletMcpOrchestratorClient(
        endpoint="http://127.0.0.1:9235/mcp", mochlet_project_id=PID,
        transport=fake.transport, token_getter=lambda: fake.token, idempotency_store=idem)
    with pytest.raises(OrchestratorUnavailable):
        c.create_job(project_id="p", run_id="rc", requested_duration_seconds=60, idempotency_key="kc")
    # the just-created Mochlet job was CANCELLED (no orphan), and the pending marker cleared
    assert len(fake.sent_chats) == 1
    assert len(fake.cancelled) == 1 and is_uuid(fake.cancelled[0])
    assert "kc" in idem.deleted


def test_find_existing_job_skips_cancelled(tmp_path):
    # A cancelled job carrying the marker must not be resurrected as the handle.
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    fake.jobs[h.job_id]["status"] = "cancelled"
    from lib.production_brain.mcp_client import MochletMcpClient
    client = MochletMcpClient("http://127.0.0.1:9235/mcp", transport=fake.transport,
                              token_getter=lambda: fake.token)
    client.initialize()
    assert c._find_existing_job(client, "r") is None


def test_token_never_appears_in_handle(tmp_path):
    fake = FakeMochletMcp(token="tok-super-secret")
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    assert "tok-super-secret" not in repr(h)
    assert "tok-super-secret" not in str(fake.sent_chats)
