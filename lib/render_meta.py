"""Trusted render-meta builder — the operational half of media-resolution parity.

The Remotion ``TimelineFrame`` composition resolves a project-local layer
``source`` (e.g. ``assets/images/hero.png``) to a loadable URL of the form
``{assetBaseUrl}/media/{projectId}/{path}`` (see the TS resolver
``remotion-composer/src/composition/media.ts``). For the embedded Player the base
is the page origin; for a **CLI render** (timeline preview, still/frame, final) the
headless browser must be handed the SAME base so the real media appears in the
output instead of the designed placeholder.

This module builds that meta for every ``TimelineFrame`` render call site with a
**trusted** base:

  * The base is NEVER derived from a request ``Host`` / ``X-Forwarded-*`` header.
  * It is loopback HTTP (``http://127.0.0.1:<active-port>``) — the port comes from
    the operator-set ``BACKLOT_PORT`` env (what ``backlot serve --port`` binds) or
    the documented default — or an explicitly-configured HTTPS base
    (``BACKLOT_RENDER_BASE_URL``), which is operator trust, not request-derived.
  * The port/base is explicitly injectable so tests and the live server pin it.

Path-traversal safety is unchanged: the base is only ``scheme://host[:port]``; the
per-layer path is validated + confined by the TS resolver and by
``lib.timeline`` (project-local sources only).
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

try:  # avoid a hard import cycle; fall back to the documented default port
    from backlot import DEFAULT_PORT as _DEFAULT_PORT
except Exception:  # pragma: no cover - defensive
    _DEFAULT_PORT = 4750

_LOOPBACK_NAMES = {"localhost"}


class RenderBaseError(ValueError):
    """The configured render base URL is unsafe or malformed."""


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _validate_base(base: str) -> str:
    """Return a normalized ``scheme://host[:port]`` base, or raise RenderBaseError.

    Accepts loopback ``http`` (127.0.0.1 / ::1 / localhost) or ANY ``https`` host
    (an explicitly-configured, operator-trusted base). Rejects everything else,
    and rejects any path/query/fragment (a base must be host-only)."""
    b = (base or "").strip().rstrip("/")
    if not b:
        raise RenderBaseError("render base URL is empty")
    parts = urlsplit(b)
    if parts.scheme == "http":
        if not _is_loopback_host(parts.hostname):
            raise RenderBaseError(f"http render base must be loopback, got {parts.hostname!r}")
    elif parts.scheme == "https":
        if not parts.hostname:
            raise RenderBaseError("https render base needs a host")
    else:
        raise RenderBaseError("render base must be http (loopback) or https")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise RenderBaseError("render base must be scheme://host[:port] only (no path/query)")
    if parts.port is not None and not (0 < parts.port < 65536):
        raise RenderBaseError("render base port out of range")
    return b


def resolve_render_base_url(
    *,
    base_url: Optional[str] = None,
    port: Optional[int] = None,
    env: Optional[dict] = None,
    require_explicit: bool = False,
) -> str:
    """Resolve the trusted base URL the CLI render fetches project media from.

    Precedence (all operator-controlled, never request-derived):
      1. an explicit ``base_url`` argument (validated),
      2. an explicit ``port`` argument → ``http://127.0.0.1:<port>``,
      3. ``BACKLOT_RENDER_BASE_URL`` env (validated),
      4. ``BACKLOT_PORT`` env (the active bound port) → loopback.

    When ``require_explicit`` is True (the SERVER path — the base must come from the
    actual runtime), NONE of the above being set raises ``RenderBaseError`` (fail
    closed) — it never silently guesses the default port. When False (a standalone
    lib render with no server), it falls back to the documented default loopback.
    """
    env = env if env is not None else os.environ
    if base_url:
        return _validate_base(base_url)
    if port is not None:
        return _validate_base(f"http://127.0.0.1:{int(port)}")
    cfg = env.get("BACKLOT_RENDER_BASE_URL")
    if cfg:
        return _validate_base(cfg)
    bp = env.get("BACKLOT_PORT")
    if bp is not None and str(bp) != "":
        try:
            p = int(bp)
        except (TypeError, ValueError):
            raise RenderBaseError(f"BACKLOT_PORT is not an integer: {bp!r}")
        if not (0 < p < 65536):
            raise RenderBaseError(f"BACKLOT_PORT out of range: {p}")
        return _validate_base(f"http://127.0.0.1:{p}")
    if require_explicit:
        raise RenderBaseError(
            "no trusted render base configured — pass an explicit base or set "
            "BACKLOT_PORT / BACKLOT_RENDER_BASE_URL")
    return _validate_base(f"http://127.0.0.1:{_DEFAULT_PORT}")


def build_render_meta(
    project_dir: Path,
    *,
    base_url: Optional[str] = None,
    port: Optional[int] = None,
    env: Optional[dict] = None,
) -> dict:
    """Title-card meta PLUS the canonical ``projectId`` + trusted ``assetBaseUrl``
    that the composition resolves project-local media against. Used by every
    ``TimelineFrame`` render call site so preview and CLI render agree."""
    from lib.timeline_render import build_meta  # deferred → no import cycle

    project_dir = Path(project_dir)
    meta = dict(build_meta(project_dir))
    meta["projectId"] = project_dir.name
    meta["assetBaseUrl"] = resolve_render_base_url(base_url=base_url, port=port, env=env)
    return meta
