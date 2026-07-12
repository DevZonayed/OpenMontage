"""In-process fake of the Mochlet Streamable-HTTP MCP server.

Speaks the same JSON-RPC 2.0 framing the real ``POST /mcp`` uses (verified live),
so the suite exercises the true transport + tool contract without a network or a
keyring. Exposed two ways:

  * ``FakeMochletMcp().transport`` — a ``transport(url, *, json, headers, timeout)``
    callable to inject into ``MochletMcpClient`` / the orchestrator (unit tests);
  * ``FakeMochletMcp().handle(method, params, headers)`` — the raw handler, reused
    by the standalone HTTP fake server used in browser acceptance.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Deterministic canonical UUIDs (no Math.random / uuid4 needed for tests).
_UUIDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
]

ALL_TOOLS = [
    "health", "listProjects", "openProject", "listSessions", "listJobPage",
    "sendChat", "getJob", "cancelJob", "runJob", "continueSession",
]


class _Resp:
    def __init__(self, status_code: int, body: Any, headers: Optional[dict] = None,
                 event_stream: bool = False):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        if body is None:
            self.text = ""
        elif event_stream:
            self.text = f"event: message\ndata: {json.dumps(body)}\n\n"
            self.headers.setdefault("content-type", "text/event-stream")
        else:
            self.text = json.dumps(body)
            self.headers.setdefault("content-type", "application/json; charset=utf-8")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeMochletMcp:
    def __init__(self, *, token: str = "secret-token", server_name: str = "mochlet",
                 version: str = "0.1.99", tools: Optional[list[str]] = None,
                 projects: Optional[list[dict]] = None, event_stream: bool = False,
                 redirect: bool = False, health_ok: bool = True,
                 trailing_notification: bool = False, projects_error: bool = False):
        self.token = token
        self.server_name = server_name
        self.version = version
        self.health_ok = health_ok
        self.trailing_notification = trailing_notification
        self.projects_error = projects_error
        self.tools = list(ALL_TOOLS if tools is None else tools)
        self.projects = projects if projects is not None else [
            {"id": "669a5386-f37b-4c6f-a712-b12e8221e54d",
             "path": "/repo/the-electricity-bulb", "name": "the-electricity-bulb"},
        ]
        self.event_stream = event_stream
        self.redirect = redirect
        self.session_id = "sess-mcp-abc"
        self._job_seq = 0
        self._sess_seq = 0
        # observability for assertions
        self.sent_chats: list[dict] = []
        self.cancelled: list[str] = []
        self.controls: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.initialized = False

    # -- auth ---------------------------------------------------------------
    def _authed(self, headers: dict) -> bool:
        h = {k.lower(): v for k, v in (headers or {}).items()}
        bearer = h.get("authorization")
        if bearer == f"Bearer {self.token}":
            return True
        if h.get("x-maestro-mcp-token") == self.token:
            return True
        return False

    # -- transport callable -------------------------------------------------
    def transport(self, url: str, *, json: dict, headers: dict, timeout: float):
        if self.redirect:
            return _Resp(307, None, headers={"location": "http://evil.example/"})
        if not self._authed(headers):
            return _Resp(401, {"error": "unauthorized"})
        method = json.get("method")
        is_notification = "id" not in json
        if is_notification:
            if method == "notifications/initialized":
                self.initialized = True
            return _Resp(202, None)
        try:
            result = self._dispatch(method, json.get("params") or {})
        except _RpcError as e:
            return self._wrap({"jsonrpc": "2.0", "id": json.get("id"),
                               "error": {"code": e.code, "message": e.message}})
        return self._wrap({"jsonrpc": "2.0", "id": json.get("id"), "result": result},
                          session=(method == "initialize"))

    def _wrap(self, msg: dict, *, session: bool = False):
        headers = {}
        if session:
            headers["mcp-session-id"] = self.session_id
        resp = _Resp(200, msg, headers=headers, event_stream=self.event_stream)
        if self.event_stream and self.trailing_notification:
            # A server notification AFTER the response frame — the client must
            # still id-match the response, not return this last event.
            notif = {"jsonrpc": "2.0", "method": "notifications/progress",
                     "params": {"progress": 1}}
            resp.text = resp.text + f"event: message\ndata: {json.dumps(notif)}\n\n"
        return resp

    # -- JSON-RPC dispatch --------------------------------------------------
    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return {"protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.server_name, "version": self.version}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": n, "description": n} for n in self.tools]}
        if method == "tools/call":
            return self._tool(params.get("name"), params.get("arguments") or {})
        raise _RpcError(-32601, f"Method not found: {method}")

    def _tool(self, name: str, args: dict) -> dict:
        if name not in self.tools:
            # A structured tool error (isError) is how MCP reports an unknown tool.
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
        if name == "health":
            return self._ok({"ok": self.health_ok, "version": self.version})
        if name == "listProjects":
            if self.projects_error:
                return {"isError": True, "content": [{"type": "text", "text": "discovery disabled"}]}
            return self._ok({"projects": self.projects})
        if name == "openProject":
            return self._ok({"ok": True, "id": args.get("id")})
        if name == "listSessions":
            return self._ok({"sessions": []})
        if name == "listJobPage":
            pid = args.get("projectId")
            jobs = [j for j in self.jobs.values() if j.get("projectId") == pid]
            return self._ok({"jobs": jobs})
        if name == "sendChat":
            return self._ok(self._send_chat(args))
        if name == "getJob":
            j = self.jobs.get(args.get("id"))
            return self._ok(j or {"id": args.get("id"), "status": "unknown"})
        if name == "cancelJob":
            jid = args.get("id")
            self.cancelled.append(jid)
            self.controls.append({"action": "cancel", "id": jid})
            if jid in self.jobs:
                self.jobs[jid]["status"] = "cancelled"
            return self._ok({"ok": True, "cancelled": jid})
        if name == "runJob":
            # runJob re-runs an EXISTING job and (per Mochlet) yields a successor.
            src = args.get("id")
            new = self._new_job(self.jobs.get(src, {}).get("projectId"),
                                self.jobs.get(src, {}).get("sessionId"),
                                predecessor=src, marker=self.jobs.get(src, {}).get("marker"))
            self.controls.append({"action": "run", "id": src, "successor": new["job"]["id"]})
            return self._ok(new)
        if name == "continueSession":
            sid = args.get("sessionId")
            new = self._new_job(args.get("projectId"), sid, marker=args.get("marker"))
            self.controls.append({"action": "continue", "sessionId": sid,
                                  "successor": new["job"]["id"]})
            return self._ok(new)
        return {"isError": True, "content": [{"type": "text", "text": f"Unhandled: {name}"}]}

    # -- helpers ------------------------------------------------------------
    def _ok(self, payload: dict) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload, "isError": False}

    def _send_chat(self, args: dict) -> dict:
        self.sent_chats.append(dict(args))
        # Real Mochlet jobs carry the instruction; the run marker embedded in the
        # text is how an indeterminate send is de-duplicated via listJobPage.
        return self._new_job(args.get("projectId"),
                             args.get("sessionId") or self._next_sess(),
                             marker=args.get("marker") or args.get("run_id") or _marker(args.get("text")))

    def _new_job(self, project_id, session_id, *, predecessor=None, marker=None) -> dict:
        session_id = session_id or self._next_sess()
        jid = _UUIDS[self._job_seq % len(_UUIDS)]
        # make later ids distinct
        if self._job_seq >= len(_UUIDS):
            jid = f"{jid[:-1]}{self._job_seq}"
        self._job_seq += 1
        job = {"id": jid, "projectId": project_id, "sessionId": session_id,
               "status": "running", "predecessor": predecessor, "marker": marker}
        self.jobs[jid] = job
        return {"session": {"id": session_id}, "job": {"id": jid}}

    def _next_sess(self) -> str:
        # Real Mochlet session ids are canonical UUIDs.
        sid = f"{self._sess_seq:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self._sess_seq += 1
        return sid


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _marker(text) -> Optional[str]:
    return (str(text)[:24]) if text else None
