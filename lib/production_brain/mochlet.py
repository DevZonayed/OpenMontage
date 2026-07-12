"""Mochlet MCP orchestrator client — the REAL operational Hermes bridge.

Implements the :class:`~lib.production_brain.orchestrator.HermesOrchestratorClient`
port over Mochlet's authenticated Streamable-HTTP MCP (see
:mod:`lib.production_brain.mcp_client`), using the documented tool contract:

    listProjects / openProject / listSessions / listJobPage / sendChat / getJob /
    cancelJob / runJob / continueSession / health

Key semantics honoured (from the Mochlet workflow references + live probe):
  * ``sendChat`` creates the run and returns ``{session:{id}, job:{id}}``; an empty
    or timed-out reply is INDETERMINATE — the job may already exist, so we dedupe
    via ``listJobPage`` and never double-send (idempotency keyed project+run);
  * a control that Mochlet implements as a SUCCESSOR job (``runJob`` / a fresh
    ``continueSession`` chat) returns the NEW canonical handle, which the caller
    persists — we never pretend the old job resumed;
  * ``cancelJob`` cancels the exact job; a cancel record is not proof of quiescence
    (the caller keeps the run non-terminal until confirmed);
  * ids are validated as canonical UUIDs before they are persisted;
  * the bearer token is read at call time from the keyring, never logged/returned.
"""

from __future__ import annotations

import json as _json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from lib.production_brain.mcp_client import (
    McpAuthError,
    McpError,
    MochletMcpClient,
)
from lib.production_brain.orchestrator import (
    ORCHESTRATOR_TOKEN_ACCOUNT,
    OrchestratorHandle,
    OrchestratorUnavailable,
    validate_endpoint,
)

# Mochlet ids are canonical UUIDs; validate strictly before persisting.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# A stable, non-sensitive marker embedded in the production instruction so a run
# can be de-duplicated against ``listJobPage`` after an indeterminate sendChat.
_RUN_MARKER_PREFIX = "OM-RUN"

# Tools the bridge must have to actually start + control a production.
REQUIRED_TOOLS = ("sendChat", "cancelJob")
CONTROL_TOOLS = ("runJob", "continueSession")


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


class MochletProjectError(OrchestratorUnavailable):
    """Mochlet is reachable but the configured project can't be resolved."""


class MochletMcpOrchestratorClient:
    """The live Hermes orchestrator over Mochlet MCP."""

    kind = "live"

    def __init__(
        self,
        *,
        endpoint: str,
        mochlet_project_id: Optional[str] = None,
        project_path: Optional[str] = None,
        engine: str = "mochlet",
        transport: Optional[Callable[..., Any]] = None,
        token_getter: Optional[Callable[[], Optional[str]]] = None,
        idempotency_store: Optional["JobIdempotencyStore"] = None,
    ) -> None:
        self._endpoint = endpoint
        self.mochlet_project_id = mochlet_project_id
        self.project_path = project_path
        self.engine = engine
        self._transport = transport
        self._token_getter = token_getter or _keyring_token_getter
        self._idem = idempotency_store

    # -- client / availability ---------------------------------------------
    def _new_client(self) -> MochletMcpClient:
        return MochletMcpClient(self._endpoint, transport=self._transport,
                               token_getter=self._token_getter)

    def _open(self) -> MochletMcpClient:
        c = self._new_client()
        c.initialize()
        return c

    def available(self) -> bool:
        """Config-only availability (a valid endpoint + a chosen project).

        The authoritative capability+project verification is the connection-layer
        health handshake; this keeps the fail-closed contract for Start.
        """
        try:
            validate_endpoint(self._endpoint)
        except OrchestratorUnavailable:
            return False
        return bool(self.mochlet_project_id)

    # -- create -------------------------------------------------------------
    def create_job(self, *, project_id: str, run_id: str,
                   requested_duration_seconds: Optional[int], idempotency_key: str) -> OrchestratorHandle:
        if not self.mochlet_project_id:
            raise MochletProjectError(
                "No Mochlet project is selected for this OpenMontage project; "
                "connect Hermes and choose the project first.")
        key = idempotency_key or f"{self.mochlet_project_id}:{run_id}"
        # 1) Already created in a prior attempt? Never double-send.
        pending_prior = False
        if self._idem is not None:
            cached = self._idem.get(key)
            if cached and cached.get("job_id"):
                return _handle(cached, self.engine)
            pending_prior = bool(cached)  # a record w/o job_id = a prior indeterminate send
        client = self._open()
        # 2) Discover a job already created for this run (indeterminate prior send).
        found = self._find_existing_job(client, run_id)
        if found:
            self._remember(key, found)
            return found
        if pending_prior:
            # A prior Start for this run was INDETERMINATE and no matching Mochlet job
            # can be found — refuse to create a duplicate production. Fail closed.
            raise OrchestratorUnavailable(
                "A prior Start for this run is unconfirmed and no matching Mochlet job "
                "was found. Verify or cancel it in Mochlet before retrying.")
        # 3) Mark pending BEFORE sending so a crash/timeout can't lead to a double-send.
        self._mark_pending(key, run_id)
        marker = f"{_RUN_MARKER_PREFIX}:{run_id}"
        text = self._instruction(project_id, run_id, requested_duration_seconds, marker)
        args = {
            "projectId": self.mochlet_project_id,
            "text": text,
            "run_id": run_id,
            "marker": marker,
            "agentContext": self._agent_context(project_id, run_id,
                                                requested_duration_seconds, marker),
        }
        try:
            result = client.call_tool("sendChat", args)
        except McpAuthError:
            raise
        except McpError as exc:
            # Indeterminate: the job may have been persisted before the error.
            recovered = self._find_existing_job(client, run_id)
            if recovered:
                self._remember(key, recovered)
                return recovered
            raise OrchestratorUnavailable(f"Mochlet sendChat failed ({exc}).") from exc
        handle = self._parse_handle(result)
        if handle is None:
            # Empty reply is INDETERMINATE — the job may exist; discover it.
            recovered = self._find_existing_job(client, run_id)
            if recovered is None:
                raise OrchestratorUnavailable(
                    "Mochlet sendChat returned no job handle and no matching job was "
                    "found; refusing to open a run without a real external job.")
            handle = recovered
        self._remember(key, handle)
        return handle

    def _find_existing_job(self, client: MochletMcpClient, run_id: str) -> Optional[OrchestratorHandle]:
        marker = f"{_RUN_MARKER_PREFIX}:{run_id}"
        try:
            page = client.call_tool("listJobPage", {"projectId": self.mochlet_project_id})
        except McpError:
            return None
        jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(jobs, list):
            return None
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if marker in _json.dumps(job):
                jid = job.get("id") or (job.get("job") or {}).get("id")
                sid = job.get("sessionId") or (job.get("session") or {}).get("id")
                if is_uuid(jid):
                    return OrchestratorHandle(
                        session_id=sid if is_uuid(sid) else jid, job_id=jid,
                        engine=self.engine, detail="existing Mochlet job (deduped)")
        return None

    def _parse_handle(self, result: dict) -> Optional[OrchestratorHandle]:
        if not isinstance(result, dict):
            return None
        session = result.get("session") or {}
        job = result.get("job") or {}
        sid = session.get("id") if isinstance(session, dict) else None
        jid = job.get("id") if isinstance(job, dict) else None
        # tolerate flat shapes too
        sid = sid or result.get("sessionId") or result.get("session_id")
        jid = jid or result.get("jobId") or result.get("job_id")
        if not is_uuid(jid):
            return None
        if not is_uuid(sid):
            sid = jid  # a job without a distinct session — use the job id
        return OrchestratorHandle(session_id=sid, job_id=jid, engine=self.engine,
                                 detail="Mochlet MCP job")

    # -- control ------------------------------------------------------------
    def cancel_job(self, *, job_id: str) -> None:
        if not is_uuid(job_id):
            raise OrchestratorUnavailable("refusing to cancel a non-canonical job id")
        client = self._open()
        client.call_tool("cancelJob", {"id": job_id})

    def control_job(self, *, job_id: str, action: str, idempotency_key: str) -> Optional[OrchestratorHandle]:
        """Issue a lifecycle control. Cancel returns None; retry/resume return the
        SUCCESSOR handle Mochlet creates (the caller persists it)."""
        if action == "cancel":
            self.cancel_job(job_id=job_id)
            return None
        if not is_uuid(job_id):
            raise OrchestratorUnavailable("refusing to control a non-canonical job id")
        client = self._open()
        if action == "retry":
            # Mochlet re-runs an existing job as a SUCCESSOR — return the new handle.
            result = client.call_tool("runJob", {"id": job_id})
            handle = self._parse_handle(result)
            if handle is None:
                # Indeterminate: no confirmed successor — do NOT let local state
                # advance pretending the old job resumed.
                raise OrchestratorUnavailable(
                    "Mochlet runJob returned no successor job handle; retry is unconfirmed.")
            return handle
        if action == "resume":
            # Resume the run by continuing its EXACT session (a successor job).
            job = client.call_tool("getJob", {"id": job_id})
            session_id = _session_of(job)
            if not is_uuid(session_id):
                raise OrchestratorUnavailable(
                    "Could not resolve the run's Mochlet session to resume; resume is "
                    "unconfirmed (refusing to start an unrelated session).")
            result = client.call_tool("continueSession", {
                "projectId": self.mochlet_project_id,
                "sessionId": session_id,
                "text": "Resume the paused production stage.",
            })
            handle = self._parse_handle(result)
            if handle is None:
                raise OrchestratorUnavailable(
                    "Mochlet continueSession returned no successor handle; resume is unconfirmed.")
            return handle
        raise OrchestratorUnavailable(f"unsupported control action: {action}")

    # -- discovery ----------------------------------------------------------
    def list_projects(self) -> list[dict]:
        client = self._open()
        page = client.call_tool("listProjects", {})
        projects = page.get("projects") if isinstance(page, dict) else None
        out = []
        if isinstance(projects, list):
            for p in projects:
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    out.append({"id": p["id"], "name": p.get("name"),
                                "path": p.get("path")})
        return out

    # -- instruction --------------------------------------------------------
    def _instruction(self, project_id: str, run_id: str,
                     requested_duration_seconds: Optional[int], marker: str) -> str:
        dur = requested_duration_seconds
        return (
            f"[{marker}] OpenMontage production run.\n"
            f"OpenMontage project_id: {project_id}\n"
            f"OpenMontage run_id: {run_id}\n"
            f"Project path: {self.project_path or '(configured workspace)'}\n"
            f"Requested duration (seconds): {dur if dur is not None else 'unset'}\n"
            "Drive the canonical 11-stage production pipeline: research → proposal → "
            "script → scene_plan → assets → narration → edit → render → review → "
            "approval → complete. Emit per-stage status/tool/output events to the "
            "OpenMontage project brain event log so the board and studio can observe "
            "progress. Pause at approval gates for the operator. Do not exceed the "
            "granted authorization.")

    def _agent_context(self, project_id: str, run_id: str,
                       requested_duration_seconds: Optional[int], marker: str) -> dict:
        return {
            "system": "openmontage",
            "marker": marker,
            "project_id": project_id,
            "run_id": run_id,
            "requested_duration_seconds": requested_duration_seconds,
            "project_path": self.project_path,
            "stage_contract": [
                "research", "proposal", "script", "scene_plan", "assets", "narration",
                "edit", "render", "review", "approval", "complete"],
        }

    def _remember(self, key: str, handle: OrchestratorHandle) -> None:
        if self._idem is not None:
            self._idem.put(key, {"session_id": handle.session_id, "job_id": handle.job_id})

    def _mark_pending(self, key: str, run_id: str) -> None:
        if self._idem is not None:
            self._idem.put(key, {"pending": True, "run_id": run_id})


def _session_of(job: Any) -> Optional[str]:
    """Extract a session id from a job object in either flat or nested shape."""
    if not isinstance(job, dict):
        return None
    sid = job.get("sessionId") or job.get("session_id")
    if not sid:
        sess = job.get("session")
        if isinstance(sess, dict):
            sid = sess.get("id")
    return sid if isinstance(sid, str) else None


def _handle(d: dict, engine: str) -> OrchestratorHandle:
    return OrchestratorHandle(session_id=d["session_id"], job_id=d["job_id"],
                             engine=engine, detail="Mochlet MCP job (idempotent)")


# --------------------------------------------------------------------------- #
# Idempotency store — persists project+run → external handle so a retried Start
# never creates a second Mochlet job.
# --------------------------------------------------------------------------- #
class JobIdempotencyStore:
    def __init__(self, path: Path):
        self._path = Path(path)

    def _read(self) -> dict:
        try:
            data = _json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> Optional[dict]:
        v = self._read().get(key)
        return v if isinstance(v, dict) else None

    def put(self, key: str, value: dict) -> None:
        data = self._read()
        data[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data), encoding="utf-8")
        tmp.replace(self._path)


def _keyring_token_getter() -> Optional[str]:
    try:
        from lib import secret_store

        return secret_store.get_secret(ORCHESTRATOR_TOKEN_ACCOUNT)
    except Exception:
        return None
