"""Provider/engine preferences — the configurable routing the agent honors.

OpenMontage keeps orchestration logic in the agent, not in Python. This module
persists the *preferences* that steer those decisions: which subscription engine
serves each purpose (master / reviewer / script / code), whether to prefer
consumer subscriptions over paid APIs, fallback ordering, per-purpose model,
image/video provider, and the composition ``render_runtime`` + authoring mode.

Storage is a small ``providers.yaml`` at the repo root, written by the Backlot
settings UI. Two hard security rules, both enforced here so a malicious/mistaken
POST cannot subvert them:

  * ``extra='forbid'`` at every level — unknown keys are rejected, so no secret
    field can be smuggled into the config artifact.
  * a secret-value guard — any string value that looks like an API key/token is
    rejected. Preferences reference providers by NAME; credentials live only in
    the vendor CLIs' own credential stores / ``.env`` (never here).
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lib.paths import REPO_ROOT

DEFAULT_PREFS_PATH = REPO_ROOT / "providers.yaml"

# Serialize concurrent saves (the Backlot server writes from a threadpool). The
# write itself is atomic (tmp + os.replace); the lock prevents two writers from
# interleaving tmp files for the same path.
_SAVE_LOCK = threading.Lock()

# Purposes an engine can be assigned to (text/reasoning work).
TEXT_PURPOSES = ("master", "reviewer", "script", "code")
RENDER_RUNTIMES = ("hyperframes", "remotion", "ffmpeg")
AUTHORING_MODES = ("templated", "atelier")

# Values matching these patterns are treated as secrets and refused. This keeps
# credentials out of the config artifact no matter what the UI posts.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),        # OpenAI / Anthropic style
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),    # Anthropic
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),        # Google API key
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),          # GitHub token
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),        # any long opaque token
)


class SecretInPreferencesError(ValueError):
    """Raised when a preference value looks like a credential."""


def _reject_secrets(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    for pat in _SECRET_PATTERNS:
        if pat.search(value):
            raise SecretInPreferencesError(
                f"{field_name!r} looks like a secret/credential. Preferences store "
                f"provider NAMES only — put credentials in the vendor CLI login or "
                f".env, never in providers.yaml."
            )
    return value


class PurposeSelection(BaseModel):
    """Engine assignment for one text purpose."""
    model_config = ConfigDict(extra="forbid")

    engine: Optional[str] = None           # engine id (see lib.engines.ENGINE_IDS)
    model: Optional[str] = None            # optional model override, e.g. "opus", "gpt-5"
    fallback: list[str] = Field(default_factory=list)  # ordered engine ids to try if primary unavailable

    @field_validator("engine", "model")
    @classmethod
    def _no_secret_scalar(cls, v, info):
        return _reject_secrets(v, info.field_name)

    @field_validator("fallback")
    @classmethod
    def _no_secret_list(cls, v):
        for item in v:
            _reject_secrets(item, "fallback[]")
        return v


class MediaSelection(BaseModel):
    """Provider assignment for a media capability (image / video)."""
    model_config = ConfigDict(extra="forbid")

    provider: Optional[str] = None         # registry tool/provider name
    model: Optional[str] = None
    fallback: list[str] = Field(default_factory=list)

    @field_validator("provider", "model")
    @classmethod
    def _no_secret_scalar(cls, v, info):
        return _reject_secrets(v, info.field_name)

    @field_validator("fallback")
    @classmethod
    def _no_secret_list(cls, v):
        for item in v:
            _reject_secrets(item, "fallback[]")
        return v


class ProviderPreferences(BaseModel):
    """Top-level, UI-editable provider/engine preferences."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    subscription_first: bool = True
    purposes: dict[str, PurposeSelection] = Field(default_factory=dict)
    image: MediaSelection = Field(default_factory=MediaSelection)
    video: MediaSelection = Field(default_factory=MediaSelection)
    # Global DEFAULT composition runtime preference. This is an INPUT to the
    # per-project proposal `render_runtime` lock — NOT the lock itself. The
    # binding lock is recorded per project at the proposal stage with explicit
    # user approval and all considered runtimes (see AGENT_GUIDE "Present Both
    # Composition Runtimes"). The server rejects setting this to a runtime that
    # is not currently available on the machine.
    preferred_render_runtime: Optional[str] = None
    authoring_mode: Optional[str] = None

    @field_validator("purposes")
    @classmethod
    def _known_purposes(cls, v):
        for key in v:
            if key not in TEXT_PURPOSES:
                raise ValueError(
                    f"Unknown purpose {key!r}. Valid purposes: {list(TEXT_PURPOSES)}"
                )
        return v

    @field_validator("preferred_render_runtime")
    @classmethod
    def _valid_runtime(cls, v):
        if v is not None and v not in RENDER_RUNTIMES:
            raise ValueError(
                f"Invalid preferred_render_runtime {v!r}. Valid: {list(RENDER_RUNTIMES)}"
            )
        return v

    @field_validator("authoring_mode")
    @classmethod
    def _valid_authoring(cls, v):
        if v is not None and v not in AUTHORING_MODES:
            raise ValueError(
                f"Invalid authoring_mode {v!r}. Valid: {list(AUTHORING_MODES)}"
            )
        return v

    @classmethod
    def default(cls) -> "ProviderPreferences":
        """Sensible defaults: subscription-first, one selection slot per purpose."""
        return cls(
            subscription_first=True,
            purposes={p: PurposeSelection() for p in TEXT_PURPOSES},
        )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ProviderPreferences":
        path = path or DEFAULT_PREFS_PATH
        if not path.exists():
            return cls.default()
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        prefs = cls.model_validate(raw)
        # Ensure every purpose has a slot for UI rendering, without discarding
        # any the user set.
        for p in TEXT_PURPOSES:
            prefs.purposes.setdefault(p, PurposeSelection())
        return prefs

    def save(self, path: Optional[Path] = None) -> Path:
        """Persist to YAML atomically. Re-validates (secret guard) before write."""
        path = path or DEFAULT_PREFS_PATH
        # Round-trip through validation to guarantee no secret slips in even if
        # a field was mutated after construction.
        validated = ProviderPreferences.model_validate(self.model_dump())
        payload = yaml.safe_dump(
            validated.model_dump(), sort_keys=False, default_flow_style=False
        )
        with _SAVE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unique temp per writer so concurrent saves can't clobber each
            # other's tmp; os.replace is atomic on POSIX + Windows.
            tmp = path.with_suffix(f".yaml.{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        return path


# ---------------------------------------------------------------------------
# Resolution — the routing the agent honors at run time.
# ---------------------------------------------------------------------------

# Map a registry capability family -> the media preference attribute.
_CAP_TO_MEDIA = {"image_generation": "image", "video_generation": "video"}


def media_preference_for(
    capability: str, *, path: Optional[Path] = None
) -> Optional[MediaSelection]:
    """Return the saved MediaSelection for an image/video capability, or None."""
    key = _CAP_TO_MEDIA.get(capability)
    if not key:
        return None
    return getattr(ProviderPreferences.load(path), key)


def resolve_media_selection(
    capability: str, inputs: dict, *, path: Optional[Path] = None
) -> dict:
    """Resolve the effective media provider hint for a selector call.

    An explicit ``inputs['preferred_provider']`` (not 'auto') always wins — the
    agent asked for something specific. Otherwise the SAVED UI preference is
    surfaced so a choice made in the Backlot settings page steers routing.

    Returns {preferred_provider, allowed_providers, model, source}. ``source`` is
    'explicit' | 'saved_preference' | 'auto'. This is a lightweight hint used for
    reporting; the *binding* deterministic resolution lives in
    ``pick_saved_media_tool`` (primary → ordered fallback → auto).
    """
    explicit = inputs.get("preferred_provider", "auto")
    allowed = list(inputs.get("allowed_providers") or [])
    if explicit and explicit != "auto":
        return {"preferred_provider": explicit, "allowed_providers": allowed,
                "model": inputs.get("model"), "source": "explicit"}

    sel = media_preference_for(capability, path=path)
    if sel and sel.provider:
        if not allowed:
            allowed = [sel.provider, *[f for f in sel.fallback if f != sel.provider]]
        return {"preferred_provider": sel.provider, "allowed_providers": allowed,
                "model": sel.model, "source": "saved_preference"}

    return {"preferred_provider": "auto", "allowed_providers": allowed,
            "model": (sel.model if sel else None), "source": "auto"}


def find_provider_tool(candidates: list, provider_id: str, *, selectable):
    """Return the first SELECTABLE candidate whose tool name OR provider name
    matches ``provider_id`` (deterministic), else None."""
    for t in candidates:
        if (getattr(t, "name", None) == provider_id
                or getattr(t, "provider", None) == provider_id) and selectable(t):
            return t
    return None


def pick_saved_media_tool(candidates: list, sel: MediaSelection, *, selectable) -> tuple:
    """Deterministic saved-preference resolution (C, review 3).

    Try the saved PRIMARY provider first; if it isn't selectable, walk the saved
    fallback list in EXACT order; only if none is selectable return (None, 'auto').

    Returns (tool_or_None, source) where source is
    'saved_preference:primary' | 'saved_preference:fallback:<id>' | 'auto'.
    """
    if sel.provider:
        t = find_provider_tool(candidates, sel.provider, selectable=selectable)
        if t is not None:
            return t, "saved_preference:primary"
    for fb in sel.fallback:
        t = find_provider_tool(candidates, fb, selectable=selectable)
        if t is not None:
            return t, f"saved_preference:fallback:{fb}"
    return None, "auto"


def apply_saved_model(
    inputs: dict, adapted: dict, sel: Optional[MediaSelection], tool, pref_source: str
) -> tuple:
    """Apply the saved model ONLY when the saved PRIMARY provider was selected (B,
    review 4). A saved model belongs to the saved primary provider — never inject
    it into a fallback/explicit/auto-chosen provider it wasn't authored for.

    Rules: applies only when ``pref_source == 'saved_preference:primary'``; an
    explicit caller ``model`` always wins; if the primary tool doesn't accept a
    model, surface unsupported rather than claiming it. When a saved model exists
    but the primary wasn't selected, return not_applicable=True so the caller can
    report it honestly.

    Returns (selected_model_or_None, unsupported_bool, not_applicable_bool).
    """
    if not (sel and sel.model):
        return None, False, False
    if pref_source != "saved_preference:primary":
        # Saved model is scoped to the saved primary; a different provider is in
        # use, so it does not apply here.
        return None, False, True
    if inputs.get("model"):  # explicit per-call model wins
        return None, False, False
    props = (getattr(tool, "input_schema", {}) or {}).get("properties", {}) or {}
    if "model" in props:
        adapted["model"] = sel.model
        return sel.model, False, False
    return None, True, False


def effective_text_engines(
    prefs: ProviderPreferences, engines: list
) -> dict:
    """Per-purpose effective engine routing — what the agent should honor.

    Uses ``resolve_purpose_engine`` for each text purpose so preflight/UI can show
    the concrete engine that will serve master/reviewer/script/code work given the
    current preferences and live auth state.
    """
    return {p: resolve_purpose_engine(prefs, engines, p) for p in TEXT_PURPOSES}


def resolve_purpose_engine(
    prefs: ProviderPreferences,
    engines: list,  # list[lib.engines.EngineStatus]
    purpose: str,
) -> dict:
    """Compute the effective engine for a purpose given availability + policy.

    Returns a dict {engine, reason, considered, subscription_first}. Never
    raises; if nothing is usable, engine is None with an explanatory reason.

    Policy:
      1. If the explicitly-selected engine is available (logged in), use it.
      2. Otherwise walk the user's fallback list, using the first available.
      3. Otherwise, when subscription_first is on, use any subscription-ready
         engine (prefer Claude, then Codex, then the rest — deterministic).
      4. Otherwise report no engine, honestly.
    """
    by_id = {e.id: e for e in engines}

    def available(engine_id: Optional[str]) -> bool:
        e = by_id.get(engine_id) if engine_id else None
        return bool(e and e.logged_in)

    selection = prefs.purposes.get(purpose) or PurposeSelection()
    considered: list[str] = []

    if selection.engine:
        considered.append(selection.engine)
        if available(selection.engine):
            return {
                "engine": selection.engine,
                "model": selection.model,
                "reason": "explicit selection is available",
                "considered": considered,
                "subscription_first": prefs.subscription_first,
            }

    for fb in selection.fallback:
        considered.append(fb)
        if available(fb):
            return {
                "engine": fb, "model": None,
                "reason": f"primary unavailable; fell back to {fb}",
                "considered": considered,
                "subscription_first": prefs.subscription_first,
            }

    if prefs.subscription_first:
        # Deterministic preference order among subscription-ready engines.
        order = ["claude", "codex", "gemini", "zai"]
        ready = [e.id for e in engines if e.subscription_backed and e.logged_in]
        for engine_id in order:
            if engine_id in ready:
                considered.append(engine_id)
                return {
                    "engine": engine_id, "model": None,
                    "reason": "subscription_first: chose an available consumer-plan engine",
                    "considered": considered,
                    "subscription_first": True,
                }

    return {
        "engine": None, "model": None,
        "reason": "no configured engine is available and no subscription engine is ready",
        "considered": considered,
        "subscription_first": prefs.subscription_first,
    }
