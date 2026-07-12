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


def test_retry_returns_successor_handle(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    successor = c.control_job(job_id=h.job_id, action="retry", idempotency_key="k:retry")
    assert successor is not None and is_uuid(successor.job_id)
    assert successor.job_id != h.job_id  # a real successor, not a pretend resume
    assert fake.controls[-1]["action"] == "run"


def test_resume_returns_successor_handle(tmp_path):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    successor = c.control_job(job_id=h.job_id, action="resume", idempotency_key="k:resume")
    assert successor is not None and is_uuid(successor.job_id)
    assert fake.controls[-1]["action"] == "continue"


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


def test_retry_without_successor_handle_raises(tmp_path, monkeypatch):
    fake = FakeMochletMcp()
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    from lib.production_brain import mcp_client as mc
    orig = mc.MochletMcpClient.call_tool

    def patched(self, name, arguments=None):
        if name == "runJob":
            return {}  # no successor handle — indeterminate
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


def test_token_never_appears_in_handle(tmp_path):
    fake = FakeMochletMcp(token="tok-super-secret")
    c = _client(fake, tmp_path)
    h = c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")
    assert "tok-super-secret" not in repr(h)
    assert "tok-super-secret" not in str(fake.sent_chats)
