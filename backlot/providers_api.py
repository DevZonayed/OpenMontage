"""Backlot providers API — the data layer behind the settings page.

Assembles a single non-secret payload the settings UI renders:
  * subscription engines + auth state (lib.engines)
  * a safe CATALOG of selectable engine/provider ids (so the UI offers validated
    choices and the server can reject anything off-catalog)
  * effective per-purpose engine routing (what the agent will honor)
  * composition runtimes + diagnostics (tool registry / video_compose)
  * media generation capability rollups (image / video / tts / music)
  * the current, editable provider preferences (lib.provider_prefs)

And validates + persists preference writes: pydantic (no secrets/unknown keys) +
semantic validation against the catalog and the LIVE runtime snapshot (so a
direct POST cannot save an off-catalog id or an unavailable render runtime).
Kept out of ``server.py`` so routes stay thin and this is unit-testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from lib.engines import discover_engines
from lib.provider_prefs import (
    AUTHORING_MODES,
    RENDER_RUNTIMES,
    TEXT_PURPOSES,
    ProviderPreferences,
    effective_text_engines,
)

# Media capabilities we surface on the settings page (name -> friendly label).
_MEDIA_CAPS = {
    "image_generation": "Image Generation",
    "video_generation": "Video Generation",
    "tts": "Text-to-Speech",
    "music_generation": "Music Generation",
}


def _registry_providers(capability: str) -> list[str]:
    """All provider names for a capability (available OR not) — the catalog.

    A currently-unavailable provider is still a VALID configuration target (the
    user may be setting up for later), so the catalog includes every registered
    provider; only truly-unknown ids are rejected.
    """
    try:
        from tools.tool_registry import registry
        registry.ensure_discovered()
        names = {
            t.provider for t in registry.get_by_capability(capability)
            if t.provider and t.provider != "selector"
        }
        # tool names too, so a user can target a specific backend
        names |= {
            t.name for t in registry.get_by_capability(capability)
            if t.name and t.provider != "selector"
        }
        return sorted(names)
    except Exception:
        return []


def build_catalog() -> dict[str, Any]:
    """The safe set of selectable ids the UI offers and the server validates."""
    return {
        "engines": list(dict.fromkeys(e.id for e in discover_engines(probe_auth=False))),
        "image_providers": _registry_providers("image_generation"),
        "video_providers": _registry_providers("video_generation"),
        "render_runtimes": list(RENDER_RUNTIMES),
        "authoring_modes": list(AUTHORING_MODES),
        "text_purposes": list(TEXT_PURPOSES),
    }


def _composition_and_media() -> dict[str, Any]:
    """Composition-runtime availability + media rollups from the registry.

    Degrades gracefully (no raw exception text to the UI) rather than 500-ing.
    """
    try:
        from tools.tool_registry import registry
        registry.discover()
        summary = registry.provider_menu_summary()
    except Exception as exc:  # sanitized: type only, never the message/paths
        return {
            "composition_runtimes": {},
            "runtime_warnings": [f"registry discovery unavailable ({type(exc).__name__})"],
            "media_capabilities": [],
            "degraded": True,
        }

    comp = summary.get("composition_runtimes", {}) or {}
    warnings = list(summary.get("runtime_warnings", []) or [])

    caps_by_name = {c.get("capability"): c for c in summary.get("capabilities", []) or []}
    media_caps: list[dict[str, Any]] = []
    for cap_name, label in _MEDIA_CAPS.items():
        entry = caps_by_name.get(cap_name)
        if not entry:
            media_caps.append({
                "capability": cap_name, "label": label, "configured": 0, "total": 0,
                "available_providers": [], "unavailable_providers": [],
            })
            continue
        media_caps.append({
            "capability": cap_name, "label": label,
            "configured": entry.get("configured", 0), "total": entry.get("total", 0),
            "available_providers": entry.get("available_providers", []),
            "unavailable_providers": entry.get("unavailable_providers", []),
        })

    return {
        "composition_runtimes": {k: bool(v) for k, v in comp.items()},
        "runtime_warnings": warnings,
        "media_capabilities": media_caps,
        "degraded": False,
    }


def _render_runtime_options(comp_runtimes: dict[str, bool], warnings: list[str]) -> list[dict[str, Any]]:
    """Describe each render runtime with availability + a reason.

    Governance: an unavailable runtime is presented as DISABLED with the reason,
    never silently dropped — the UI must not let a user pick a runtime that would
    force a silent swap at compose time, and the server rejects it too.
    """
    hf_reasons = [w for w in warnings if w.lower().startswith("hyperframes")]
    options: list[dict[str, Any]] = []
    for rt in RENDER_RUNTIMES:
        available = bool(comp_runtimes.get(rt, False))
        reason = ""
        if not available:
            if rt == "hyperframes" and hf_reasons:
                reason = "; ".join(hf_reasons)
            elif rt == "remotion":
                # Precise, browser-aware reason from the runtime doctor (install
                # vs. missing-browser), not a generic string.
                try:
                    from lib import remotion_runtime as _rr
                    reason = _rr.doctor()["reason"] or "Remotion is not render-ready."
                except Exception:
                    reason = "Remotion is not render-ready."
            elif rt == "hyperframes":
                reason = "HyperFrames runtime floor not met (needs Node >= 22, ffmpeg, npx)."
            else:
                reason = f"{rt} unavailable on this machine."
        options.append({"id": rt, "available": available, "reason": reason})
    return options


def build_providers_payload(
    *, probe_auth: bool = True, prefs_path: Optional[Path] = None
) -> dict[str, Any]:
    """The full, non-secret settings payload."""
    from lib.engine_actions import supported_actions
    engines = discover_engines(probe_auth=probe_auth)
    comp = _composition_and_media()
    prefs = ProviderPreferences.load(prefs_path)
    ready = [e.id for e in engines if e.subscription_backed and e.logged_in]

    # Z.AI secure-credential status (non-secret) so the UI can render its panel.
    try:
        from lib import zai_credentials
        zai_status = zai_credentials.status()
    except Exception:
        zai_status = {"engine": "zai", "configured": False, "status": "not_configured",
                      "plan_type": None, "keychain_available": False}

    engines_out = []
    for e in engines:
        d = e.to_dict()
        d["actions"] = supported_actions(e.id)  # {status/connect/logout -> auto|manual|unsupported}
        if e.id == "zai":
            d["credential"] = zai_status  # drives the interactive key panel
        engines_out.append(d)
    return {
        "engines": engines_out,
        "zai_credential": zai_status,
        "subscription_ready": ready,
        "any_subscription_ready": bool(ready),
        "image_via_subscription_supported": any(e.image_capable for e in engines),
        "text_purposes": list(TEXT_PURPOSES),
        "effective_text_engines": effective_text_engines(prefs, engines),
        "catalog": build_catalog(),
        "composition_runtimes": comp["composition_runtimes"],
        "runtime_warnings": comp["runtime_warnings"],
        "render_runtime_options": _render_runtime_options(
            comp["composition_runtimes"], comp["runtime_warnings"]
        ),
        "authoring_modes": list(AUTHORING_MODES),
        "media_capabilities": comp["media_capabilities"],
        "preferences": prefs.model_dump(),
        "degraded": comp["degraded"],
    }


# ---------------------------------------------------------------------------
# Z.AI credential lifecycle (store / verify / remove / launch) — secure.
# ---------------------------------------------------------------------------

class CredentialError(ValueError):
    """UI-safe credential error carrying an HTTP status."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _default_zai_launch() -> bool:
    """Open a real Terminal on macOS running the SCOPED Z.AI launcher.

    The command is FIXED (no user string interpolation) and contains NO key —
    ``lib.zai_launcher`` reads the key from the keychain at runtime. Returns True
    if the launch was initiated.
    """
    import platform
    import subprocess
    import sys
    from lib.paths import REPO_ROOT

    if platform.system() != "Darwin":
        raise CredentialError(
            "The one-click Terminal launcher is macOS-only. Run "
            "`python -m lib.zai_launcher` in a terminal instead.", status=400)
    # Fixed, allowlisted argv — repo path + interpreter are constants, not input.
    py = sys.executable
    inner = f"cd {REPO_ROOT!s} && {py} -m lib.zai_launcher"
    script = f'tell application "Terminal" to do script {json.dumps(inner)}'
    subprocess.run(["osascript", "-e", script], check=True, timeout=15,
                   capture_output=True)
    return True


def handle_credential(body: Any, *, launcher=None) -> dict[str, Any]:
    """Dispatch a Z.AI credential action. Never returns/logs the key.

    body: {engine:"zai", action:"status"|"store"|"verify"|"remove"|"launch",
           plan_type?, api_key?}
    """
    from lib import zai_credentials as zc

    if not isinstance(body, dict):
        raise CredentialError("body must be a JSON object")
    engine = body.get("engine")
    action = body.get("action")
    if engine != "zai":
        raise CredentialError("unsupported engine for credential management")
    if action not in ("status", "store", "verify", "remove", "launch"):
        raise CredentialError("unknown credential action")

    try:
        if action == "status":
            return zc.status()
        if action == "store":
            key = body.get("api_key")
            plan = body.get("plan_type") or "coding"
            if not isinstance(key, str):
                raise CredentialError("api_key is required")
            st = zc.store_key(key, plan)
            # Best-effort immediate verify (non-billable) if requested.
            if body.get("verify"):
                st = zc.verify(plan)
            return st
        if action == "verify":
            return zc.verify(body.get("plan_type"))
        if action == "remove":
            return zc.remove_key()
        if action == "launch":
            spawn = launcher or _default_zai_launch
            if not zc.status().get("configured"):
                raise CredentialError("no Z.AI key is stored", status=400)
            ok = bool(spawn())
            return {"launched": ok, **zc.status()}
    except zc.ZaiCredentialError as exc:
        # Already UI-safe (never contains the key).
        raise CredentialError(str(exc), status=400)
    raise CredentialError("unhandled credential action")  # pragma: no cover


class PreferencesSaveError(ValueError):
    """Raised when a preferences write is rejected. ``is_secret`` marks the
    credential-injection case so the route can return a tailored message."""

    def __init__(self, message: str, *, is_secret: bool = False) -> None:
        super().__init__(message)
        self.is_secret = is_secret


def _semantic_validation(prefs: ProviderPreferences, catalog: dict, comp_runtimes: dict) -> None:
    """Validate ids against the catalog + runtime availability (F4/F5).

    All messages are safe to show the user (no filesystem/command details).
    """
    engines = set(catalog["engines"])
    images = set(catalog["image_providers"])
    videos = set(catalog["video_providers"])

    def _check_list(items: list[str], allowed: set[str], label: str) -> None:
        seen: set[str] = set()
        for item in items:
            if item not in allowed:
                raise PreferencesSaveError(
                    f"{label} {item!r} is not a known provider/engine. "
                    f"Choose from the discovered options."
                )
            if item in seen:
                raise PreferencesSaveError(f"{label} lists {item!r} more than once.")
            seen.add(item)

    # Text purposes: engine + fallback must be known; primary not in its own fallback.
    for purpose, sel in prefs.purposes.items():
        if sel.engine is not None and sel.engine not in engines:
            raise PreferencesSaveError(
                f"purpose {purpose!r}: engine {sel.engine!r} is not a known engine."
            )
        if sel.engine and sel.engine in sel.fallback:
            raise PreferencesSaveError(
                f"purpose {purpose!r}: primary engine {sel.engine!r} must not also be a fallback."
            )
        _check_list(sel.fallback, engines, f"purpose {purpose!r} fallback")

    # Media: provider + fallback must be known; primary not in its own fallback.
    for label, sel, allowed in (("image", prefs.image, images), ("video", prefs.video, videos)):
        if sel.provider is not None and sel.provider not in allowed:
            raise PreferencesSaveError(
                f"{label} provider {sel.provider!r} is not a known provider."
            )
        if sel.provider and sel.provider in sel.fallback:
            raise PreferencesSaveError(
                f"{label}: primary provider {sel.provider!r} must not also be a fallback."
            )
        _check_list(sel.fallback, allowed, f"{label} fallback")

    # F5: the global PREFERRED runtime must be currently available. This is a
    # default that feeds the per-project proposal lock; refusing an unavailable
    # one keeps the config honest and prevents a later silent swap.
    rt = prefs.preferred_render_runtime
    if rt is not None and not comp_runtimes.get(rt, False):
        raise PreferencesSaveError(
            f"preferred_render_runtime {rt!r} is not available on this machine right now. "
            f"Pick an available runtime, or install it first."
        )


def save_preferences(data: Any, *, prefs_path: Optional[Path] = None) -> dict[str, Any]:
    """Validate + persist a preferences write. Returns the fresh payload.

    Rejects unknown keys (extra='forbid'), secret-looking values, off-catalog
    ids, duplicate/primary-in-own fallback, and an unavailable preferred runtime.
    """
    if not isinstance(data, dict):
        raise PreferencesSaveError("preferences body must be a JSON object")
    from pydantic import ValidationError
    try:
        prefs = ProviderPreferences.model_validate(data)
    except ValidationError as exc:
        msg = str(exc)
        if "secret" in msg.lower() or "credential" in msg.lower():
            raise PreferencesSaveError(
                "Rejected: a value looks like a secret/credential. Preferences "
                "store provider names only — credentials belong in the vendor CLI "
                "login or .env, never in providers.yaml.",
                is_secret=True,
            ) from exc
        # Surface a concise, safe reason (pydantic messages are about our own
        # schema, not filesystem/commands).
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "preferences"
        raise PreferencesSaveError(f"invalid {loc}: {first.get('msg', 'validation error')}") from exc

    comp = _composition_and_media()
    _semantic_validation(prefs, build_catalog(), comp["composition_runtimes"])
    prefs.save(prefs_path)
    return build_providers_payload(probe_auth=True, prefs_path=prefs_path)
