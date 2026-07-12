"""Hermes / Mochlet connection layer — MCP-aware status, guided connect, client.

The local **Mochlet** brain speaks an AUTHENTICATED Streamable-HTTP MCP server on
the loopback gateway (``POST http://127.0.0.1:9235/mcp``) — verified live: plain
REST (``GET /health``, ``POST /jobs``) 404s; ``/mcp`` returns 401 until a bearer
token is supplied. So "connected" here means a REAL MCP capability+project
handshake, never a bare TCP/200:

  * verify = ``initialize`` (401 ⇒ needs credential) → ``ping``/``health`` tool →
    ``tools/list`` (the run/control tools ``sendChat``+``cancelJob`` must exist,
    else "tools disabled") → ``listProjects`` (pick the OpenMontage project);
  * ``connection.available`` is True ONLY after that full handshake, so
    "Continue production with Hermes" is guaranteed to invoke a real ``sendChat``;
  * the bearer token lives ONLY in the OS keyring (never echoed/logged/persisted
    to a file); the non-secret endpoint + kind + chosen project id persist to a
    gitignored config;
  * endpoints are validated fail-closed (loopback-HTTP or HTTPS), redirects are
    rejected, and the transport is injectable so tests never touch net/keyring.

A generic REST client is retained for an explicitly-configured non-Mochlet
endpoint (``kind="rest"``), but the endpoint kind is explicit and persisted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from lib.paths import REPO_ROOT
from lib.production_brain.mcp_client import (
    McpAuthError,
    McpError,
    MochletMcpClient,
)
from lib.production_brain.mochlet import (
    REQUIRED_TOOLS,
    JobIdempotencyStore,
    MochletMcpOrchestratorClient,
    extract_project_list,
    looks_like_mochlet,
)
from lib.production_brain.orchestrator import (
    ORCHESTRATOR_TOKEN_ACCOUNT,
    ORCHESTRATOR_URL_ENV,
    ConfiguredHermesOrchestratorClient,
    OrchestratorUnavailable,
    validate_endpoint,
)

# The fixed local Mochlet MCP endpoint (note the /mcp path). Suggested + probed,
# but only ADOPTED once the user connects (verify + project selection + persist).
DEFAULT_MOCHLET_URL = "http://127.0.0.1:9235/mcp"
HEALTH_PATH = "/health"  # legacy REST probe path (kind="rest" only)
_PROBE_TIMEOUT_SECONDS = 1.5

_CONFIG_DIRNAME = ".backlot"
_CONFIG_FILENAME = "hermes_connection.json"
_JOBS_FILENAME = "hermes_jobs.json"


class ConnectionError(RuntimeError):
    """A guided-connect failure carrying an HTTP-ish ``status`` for the API."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Config persistence (endpoint/kind/project — never the token)
# --------------------------------------------------------------------------- #
def _config_dir(base_dir: Optional[Path] = None) -> Path:
    return Path(base_dir or REPO_ROOT) / _CONFIG_DIRNAME


def _config_path(base_dir: Optional[Path] = None) -> Path:
    return _config_dir(base_dir) / _CONFIG_FILENAME


def stored_config(base_dir: Optional[Path] = None) -> dict:
    """The persisted connection config (validated endpoint), or ``{}``."""
    try:
        data = json.loads(_config_path(base_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    url = data.get("endpoint")
    try:
        validate_endpoint(url)
    except OrchestratorUnavailable:
        return {}
    return data


def stored_endpoint(base_dir: Optional[Path] = None) -> Optional[str]:
    return stored_config(base_dir).get("endpoint")


def _persist_config(*, endpoint: str, kind: str, project_id: Optional[str],
                    project_path: Optional[str], base_dir: Optional[Path] = None) -> None:
    d = _config_dir(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = {"endpoint": endpoint, "endpoint_kind": kind,
               "mochlet_project_id": project_id, "mochlet_project_path": project_path,
               "kind": "hermes_connection"}
    tmp = d / (_CONFIG_FILENAME + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(_config_path(base_dir))


def _clear_config(base_dir: Optional[Path] = None) -> None:
    try:
        _config_path(base_dir).unlink()
    except OSError:
        pass


def _endpoint_kind(url: Optional[str], stored_kind: Optional[str] = None) -> str:
    if stored_kind in ("mcp", "rest"):
        return stored_kind
    if not url:
        return "mcp"
    path = (urlsplit(url).path or "").rstrip("/").lower()
    return "mcp" if path.endswith("/mcp") else "rest"


def configured_endpoint(*, env: Optional[dict] = None,
                        base_dir: Optional[Path] = None) -> Optional[str]:
    """The live endpoint the adapter uses: env var → persisted config (fail-closed;
    the Mochlet default is only a suggestion, never auto-adopted)."""
    environ = env if env is not None else os.environ
    raw = environ.get(ORCHESTRATOR_URL_ENV)
    if raw:
        try:
            return validate_endpoint(raw)
        except OrchestratorUnavailable:
            return None
    return stored_endpoint(base_dir)


def jobs_store(base_dir: Optional[Path] = None) -> JobIdempotencyStore:
    return JobIdempotencyStore(_config_dir(base_dir) / _JOBS_FILENAME)


# --------------------------------------------------------------------------- #
# MCP verify handshake
# --------------------------------------------------------------------------- #
def verify_mcp(endpoint: str, *, transport: Optional[Callable[..., Any]] = None,
               token: Optional[str] = None) -> dict:
    """Do the real MCP capability handshake. Never raises.

    Returns ``{reachable, authenticated, needs_token, server_name, tools,
    has_required_tools, health_ok, projects, detail}``.
    """
    out: dict = {"reachable": False, "authenticated": False, "needs_token": False,
                 "server_name": None, "tools": [], "has_required_tools": False,
                 "health_ok": False, "projects": [], "projects_listed": False,
                 "is_mochlet": False, "detail": ""}
    try:
        client = MochletMcpClient(endpoint, transport=transport, token_getter=lambda: token)
    except Exception as exc:
        out["detail"] = f"invalid endpoint ({exc.__class__.__name__})"
        return out
    try:
        client.initialize()
    except McpAuthError:
        out.update(reachable=True, needs_token=True,
                   detail="The MCP server requires a valid credential.")
        return out
    except McpError as exc:
        out["detail"] = str(exc)
        return out
    server_name = (client.server_info or {}).get("name")
    out.update(reachable=True, authenticated=True, server_name=server_name,
               is_mochlet=looks_like_mochlet(server_name))
    try:
        client.ping()
    except McpError:
        pass
    try:
        tools = client.list_tools()
    except McpError as exc:
        out["detail"] = f"could not list MCP tools ({exc})"
        return out
    out["tools"] = tools
    out["has_required_tools"] = all(t in tools for t in REQUIRED_TOOLS)
    out["health_ok"] = True
    if "health" in tools:
        try:
            h = client.call_tool("health", {})
            out["health_ok"] = bool(h.get("ok", True))
        except McpError:
            out["health_ok"] = False
    if "listProjects" in tools:
        try:
            page = client.call_tool("listProjects", {})
            # Accept the live Mochlet ``{"result": [...]}`` envelope AND the legacy
            # ``{"projects": [...]}`` shape; ``None`` means no recognizable array
            # (unverifiable → leave projects_listed False, fail-closed).
            projs = extract_project_list(page)
            if projs is not None:
                out["projects_listed"] = True  # the discovery call actually succeeded
                out["projects"] = projs
        except McpError:
            pass
    out["detail"] = "Connected." if out["has_required_tools"] else (
        "Mochlet connection found, but chat/control tools are disabled.")
    return out


# --------------------------------------------------------------------------- #
# Legacy REST probe (kind="rest" only)
# --------------------------------------------------------------------------- #
def _default_rest_transport(url: str, *, timeout: float, headers: Optional[dict] = None):
    import requests

    return requests.get(url, timeout=timeout, allow_redirects=False, headers=headers)


def probe_health(url: str, *, transport: Optional[Callable[..., Any]] = None,
                 timeout: float = _PROBE_TIMEOUT_SECONDS, token: Optional[str] = None) -> dict:
    """GET ``<url>/health`` for an explicitly-REST endpoint. Never raises."""
    try:
        base = validate_endpoint(url)
    except OrchestratorUnavailable as exc:
        return {"reachable": False, "healthy": False, "status_code": None,
                "service": None, "detail": str(exc)}
    target = base.rstrip("/") + HEALTH_PATH
    call = transport or _default_rest_transport
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        resp = call(target, timeout=timeout, headers=headers)
    except Exception as exc:
        return {"reachable": False, "healthy": False, "status_code": None,
                "service": None, "detail": f"Endpoint did not respond ({exc.__class__.__name__})."}
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 0:
        return {"reachable": False, "healthy": False, "status_code": None,
                "service": None, "detail": "Health check returned no status code."}
    if 300 <= status < 400:
        return {"reachable": True, "healthy": False, "status_code": status,
                "service": None, "detail": "Endpoint redirected the health check."}
    if not (200 <= status < 300):
        return {"reachable": True, "healthy": False, "status_code": status,
                "service": None, "detail": f"Health check returned HTTP {status}."}
    service = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            service = body.get("service") or body.get("name")
    except Exception:
        pass
    return {"reachable": True, "healthy": True, "status_code": status,
            "service": service, "detail": "Connected."}


# --------------------------------------------------------------------------- #
# Structured connection status
# --------------------------------------------------------------------------- #
def connection_status(
    *,
    env: Optional[dict] = None,
    base_dir: Optional[Path] = None,
    transport: Optional[Callable[..., Any]] = None,
    probe: bool = True,
    secret_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> dict:
    """Plain-language, actionable Hermes connection status. Never returns a token."""
    cfg = stored_config(base_dir)
    endpoint = configured_endpoint(env=env, base_dir=base_dir)
    kind = _endpoint_kind(endpoint, cfg.get("endpoint_kind"))
    selected_project = cfg.get("mochlet_project_id")
    getter = secret_getter or _keyring_getter
    token = getter(ORCHESTRATOR_TOKEN_ACCOUNT)
    token_present = bool(token)

    def _base(status, available, headline, detail, actions, **extra):
        return {"status": status, "available": available,
                "endpoint": endpoint, "endpoint_kind": kind,
                "suggested_endpoint": DEFAULT_MOCHLET_URL,
                "loopback": _is_loopback(endpoint or DEFAULT_MOCHLET_URL),
                "token_configured": token_present,
                "project": selected_project,
                "headline": headline, "detail": detail, "actions": actions, **extra}

    connect_action = {"id": "connect_hermes", "label": "Connect Hermes"}
    retry_action = {"id": "retry_connect", "label": "Retry connection"}

    # --- unconfigured: detect a local Mochlet MCP so Connect is one step ----
    if not endpoint:
        if probe:
            v = verify_mcp(DEFAULT_MOCHLET_URL, transport=transport, token=token)
            if v["reachable"]:
                return _base("detected", False,
                             "Local Hermes (Mochlet) detected — connect to start production",
                             "Mochlet is running on this Mac. Connect to verify its "
                             "capabilities and choose the project.",
                             [{"id": "connect_hermes", "label": "Connect Hermes (Mochlet)"}])
        return _base("needs_setup", False, "Hermes isn't connected yet",
                     "OpenMontage runs production through the Hermes brain. Connect the "
                     "local Mochlet orchestrator (127.0.0.1:9235/mcp) to begin.",
                     [connect_action])

    if not probe:
        return _base("configured", bool(selected_project),
                     "Hermes configured", "An orchestrator endpoint is configured.", [])

    # --- REST endpoint (explicitly non-Mochlet) -----------------------------
    if kind == "rest":
        h = probe_health(endpoint, transport=transport, token=token)
        if h["healthy"]:
            return _base("connected", True, "Connected to Hermes",
                         "Production runs are enabled.", [])
        if h["reachable"] and h.get("status_code") in (401, 403):
            return _base("needs_token", False, "Hermes needs a credential",
                         "The endpoint rejected the request. Reconnect with a valid token.",
                         [{"id": "connect_hermes", "label": "Reconnect Hermes"}])
        return _base("unreachable", False, "Hermes is configured but not responding",
                     h.get("detail") or "The endpoint did not respond.",
                     [retry_action, connect_action])

    # --- Mochlet MCP endpoint ----------------------------------------------
    v = verify_mcp(endpoint, transport=transport, token=token)
    if not v["reachable"]:
        return _base("unreachable", False, "Hermes is configured but not responding",
                     v.get("detail") or "The MCP endpoint did not respond.",
                     [retry_action, connect_action])
    if v["needs_token"]:
        return _base("needs_token", False, "Hermes needs a credential",
                     "Mochlet is reachable but rejected the request. Reconnect with a valid token.",
                     [{"id": "connect_hermes", "label": "Reconnect Hermes"}])
    if not v.get("is_mochlet"):
        # A foreign MCP server that merely exposes the tool names must NOT be
        # trusted to run production.
        return _base("wrong_server", False,
                     "Connected endpoint is not a recognized Hermes/Mochlet server",
                     "This MCP endpoint did not identify as Hermes/Mochlet. Point "
                     "OpenMontage at the local Mochlet orchestrator.",
                     [connect_action], server_name=v["server_name"])
    if not v["has_required_tools"]:
        return _base("tools_disabled", False,
                     "Mochlet connection found, but chat/control tools are disabled",
                     "Enable the chat/control tool categories in Mochlet so OpenMontage "
                     "can start and control production, then reconnect.",
                     [{"id": "connect_hermes", "label": "Re-check after enabling tools"}],
                     server_name=v["server_name"])
    if not v.get("health_ok", True):
        return _base("degraded", False, "Hermes is connected but reports unhealthy",
                     "The Mochlet health check reported a problem. Production is paused "
                     "until it recovers.", [retry_action], server_name=v["server_name"])
    # capable + healthy — the project must be VERIFIABLE (listProjects succeeded)
    # AND an exact match; a stale persisted id can't pass on an unverifiable list.
    project_ids = {p["id"] for p in v["projects"]}
    if not v.get("projects_listed") or not selected_project or selected_project not in project_ids:
        return _base("needs_project", False,
                     "Choose the OpenMontage project in Mochlet",
                     "Mochlet is connected. Select which Mochlet project drives this "
                     "OpenMontage workspace to enable production.",
                     [{"id": "connect_hermes", "label": "Choose project"}],
                     server_name=v["server_name"], projects=v["projects"])
    svc = v["server_name"]
    return _base("connected", True,
                 f"Connected to Hermes{f' ({svc})' if svc else ''}",
                 "Production runs are enabled.", [], server_name=svc,
                 projects=v["projects"])


# --------------------------------------------------------------------------- #
# Guided connect / disconnect
# --------------------------------------------------------------------------- #
def connect(
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    project_id: Optional[str] = None,
    kind: Optional[str] = None,
    env: Optional[dict] = None,
    base_dir: Optional[Path] = None,
    transport: Optional[Callable[..., Any]] = None,
    secret_setter: Optional[Callable[[str, str], None]] = None,
    secret_getter: Optional[Callable[[str], Optional[str]]] = None,
    persist: bool = True,
) -> dict:
    """Guided connect: validate endpoint → store token → MCP verify → choose
    project → persist. Returns a connection-status dict; never returns the token.
    A failed verify does NOT persist (fail-closed)."""
    target = url or DEFAULT_MOCHLET_URL
    try:
        target = validate_endpoint(target)
    except OrchestratorUnavailable as exc:
        raise ConnectionError(str(exc), status=400) from exc
    endpoint_kind = _endpoint_kind(target, kind)

    if token is not None:
        if not isinstance(token, str) or not token.strip():
            raise ConnectionError("token must be a non-empty string", status=400)
        setter = secret_setter or _keyring_setter
        try:
            setter(ORCHESTRATOR_TOKEN_ACCOUNT, token)
        except Exception as exc:
            raise ConnectionError(
                f"could not store the credential securely ({exc.__class__.__name__})",
                status=500) from exc

    getter = secret_getter or _keyring_getter
    verify_token = token or getter(ORCHESTRATOR_TOKEN_ACCOUNT)

    if endpoint_kind == "rest":
        return _connect_rest(target, verify_token, base_dir=base_dir, env=env,
                             transport=transport, secret_getter=secret_getter, persist=persist)

    v = verify_mcp(target, transport=transport, token=verify_token)
    loop = _is_loopback(target)
    if not v["reachable"]:
        return _fail("unreachable", False, "Couldn't reach Hermes",
                     v.get("detail") or "No local Mochlet MCP responded. Start Mochlet and retry.",
                     [{"id": "retry_connect", "label": "Retry connection"}], loop, verify_token)
    if v["needs_token"]:
        return _fail("needs_token", False, "Hermes rejected the connection",
                     "Mochlet requires a valid credential.",
                     [{"id": "connect_hermes", "label": "Reconnect Hermes"}], loop, verify_token)
    if not v.get("is_mochlet"):
        return _fail("wrong_server", False,
                     "Connected endpoint is not a recognized Hermes/Mochlet server",
                     "This MCP endpoint did not identify as Hermes/Mochlet — refusing to "
                     "enable production against an unknown server.",
                     [{"id": "connect_hermes", "label": "Connect Hermes"}], loop, verify_token,
                     server_name=v["server_name"])
    if not v["has_required_tools"]:
        return _fail("tools_disabled", False,
                     "Mochlet connection found, but chat/control tools are disabled",
                     "Enable the chat/control tool categories in Mochlet, then reconnect.",
                     [{"id": "connect_hermes", "label": "Re-check after enabling tools"}],
                     loop, verify_token, server_name=v["server_name"])
    if not v.get("health_ok", True):
        return _fail("degraded", False, "Hermes is connected but reports unhealthy",
                     "The Mochlet health check reported a problem; try again once it recovers.",
                     [{"id": "retry_connect", "label": "Retry connection"}], loop, verify_token,
                     server_name=v["server_name"])

    # choose the project — the discovery call MUST have succeeded (fail closed).
    projects = v["projects"]
    if not v.get("projects_listed"):
        return _fail("needs_project", False, "Choose the OpenMontage project in Mochlet",
                     "Mochlet did not return a project list to verify against. Ensure "
                     "project discovery is enabled, then reconnect.",
                     [{"id": "connect_hermes", "label": "Choose project"}], loop, verify_token,
                     server_name=v["server_name"], projects=projects)
    chosen = _resolve_project(projects, project_id, stored_config(base_dir).get("mochlet_project_id"))
    if chosen is None:
        return _fail("needs_project", False, "Choose the OpenMontage project in Mochlet",
                     "Mochlet is connected. Select which Mochlet project drives this "
                     "OpenMontage workspace.",
                     [{"id": "connect_hermes", "label": "Choose project"}], loop, verify_token,
                     server_name=v["server_name"], projects=projects, endpoint=target,
                     endpoint_kind=endpoint_kind)

    if persist:
        _persist_config(endpoint=target, kind=endpoint_kind,
                        project_id=chosen.get("id"), project_path=chosen.get("path"),
                        base_dir=base_dir)
    return connection_status(env=env, base_dir=base_dir, transport=transport,
                             secret_getter=secret_getter, probe=True)


def _resolve_project(projects: list[dict], project_id: Optional[str],
                     stored_id: Optional[str]) -> Optional[dict]:
    by_id = {p["id"]: p for p in projects if p.get("id")}
    if project_id:
        return by_id.get(project_id)  # must be a real project; else None → needs_project
    if stored_id and stored_id in by_id:
        return by_id[stored_id]
    if len(projects) == 1:
        return projects[0]
    return None


def _connect_rest(target, verify_token, *, base_dir, env, transport, secret_getter, persist):
    h = probe_health(target, transport=transport, token=verify_token)
    loop = _is_loopback(target)
    if not h["reachable"]:
        return _fail("unreachable", False, "Couldn't reach Hermes",
                     h.get("detail") or "The endpoint did not respond.",
                     [{"id": "retry_connect", "label": "Retry connection"}], loop, verify_token)
    if not h["healthy"]:
        code = h.get("status_code")
        return _fail("needs_token" if code in (401, 403) else "unreachable", False,
                     "Hermes rejected the connection",
                     "The endpoint rejected the credential." if code in (401, 403)
                     else (h.get("detail") or "The endpoint is unhealthy."),
                     [{"id": "connect_hermes", "label": "Reconnect Hermes"}], loop, verify_token)
    if persist:
        _persist_config(endpoint=target, kind="rest", project_id=None, project_path=None,
                        base_dir=base_dir)
    return connection_status(env=env, base_dir=base_dir, transport=transport,
                             secret_getter=secret_getter, probe=True)


def _fail(status, available, headline, detail, actions, loopback, token, **extra):
    base = {"status": status, "available": available, "endpoint": None,
            "endpoint_kind": extra.pop("endpoint_kind", "mcp"),
            "suggested_endpoint": DEFAULT_MOCHLET_URL, "loopback": loopback,
            "token_configured": bool(token), "project": None,
            "headline": headline, "detail": detail, "actions": actions}
    base.update(extra)
    return base


def disconnect(*, base_dir: Optional[Path] = None,
               secret_deleter: Optional[Callable[[str], bool]] = None,
               wipe_token: bool = False) -> dict:
    _clear_config(base_dir)
    if wipe_token:
        deleter = secret_deleter or _keyring_deleter
        try:
            deleter(ORCHESTRATOR_TOKEN_ACCOUNT)
        except Exception:
            pass
    return connection_status(base_dir=base_dir, probe=False)


# --------------------------------------------------------------------------- #
# Live client build (what the adapter uses)
# --------------------------------------------------------------------------- #
def build_live_client(*, env: Optional[dict] = None, base_dir: Optional[Path] = None,
                      transport: Optional[Callable[..., Any]] = None):
    """Build the orchestrator client the adapter uses (env/persisted config).

    kind="mcp" → :class:`MochletMcpOrchestratorClient`; kind="rest" → the generic
    :class:`ConfiguredHermesOrchestratorClient`. Unconfigured → a fail-closed REST
    client bound to ``None`` (so ``available()`` is False)."""
    cfg = stored_config(base_dir)
    endpoint = configured_endpoint(env=env, base_dir=base_dir)
    kind = _endpoint_kind(endpoint, cfg.get("endpoint_kind"))
    if endpoint and kind == "mcp":
        return MochletMcpOrchestratorClient(
            endpoint=endpoint, mochlet_project_id=cfg.get("mochlet_project_id"),
            project_path=cfg.get("mochlet_project_path"), transport=transport,
            idempotency_store=jobs_store(base_dir))
    return ConfiguredHermesOrchestratorClient(url=endpoint, transport=transport)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _is_loopback(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host in {"127.0.0.1", "::1", "localhost", "ip6-localhost"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _keyring_getter(account: str) -> Optional[str]:
    try:
        from lib import secret_store

        return secret_store.get_secret(account)
    except Exception:
        return None


def _keyring_setter(account: str, value: str) -> None:
    from lib import secret_store

    secret_store.set_secret(account, value)


def _keyring_deleter(account: str) -> bool:
    try:
        from lib import secret_store

        return secret_store.delete_secret(account)
    except Exception:
        return False
