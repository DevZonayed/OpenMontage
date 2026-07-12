"""Subscription-engine discovery — the honest, secrets-free capability probe.

OpenMontage's core loop is agent-driven: *the coding agent is the intelligence*
(there is no internal LLM-calling layer). What this module adds is a truthful
inventory of the **subscription-backed coding engines** installed on the machine
so the agent (and the Backlot settings UI) can prefer the user's existing
consumer plans over paid per-call APIs, per requirement:

  - Claude  -> Claude Code CLI with Claude OAuth (Max/Pro), NOT an API key.
  - Codex   -> Codex CLI with ChatGPT OAuth (Max/Pro), NOT an API key.
  - Gemini  -> Gemini CLI with Google OAuth (Code Assist), where installed.
  - Z.AI    -> the real supported GLM path (API token / Claude-Code proxy).

Design rules that make this safe and reviewable:

  * **No secrets ever leave this module.** We shell out to each vendor's own
    ``... auth status`` / ``login status`` command (which are explicitly designed
    to report state without printing tokens) and we keep only non-secret fields
    (logged_in, auth method, plan tier). Identity fields the CLIs happen to emit
    (email, org id) are dropped on the floor — never returned, logged, or stored.
  * **Detection is honest.** A missing binary is reported as not-installed. An
    unsupported capability (e.g. image generation over a coding-agent OAuth
    session) is reported with an explicit blocker string, not faked.
  * **Testable.** ``which`` and the subprocess ``runner`` are injectable so tests
    can simulate any install/auth combination with zero real CLIs.

This module never *calls* an engine to do work; it only reports what is available
so preferences (``lib/provider_prefs.py``) and the UI can route intelligently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Text-generation purposes an engine can serve. Media purposes (image/video)
# are handled by the tool registry's generation providers, not by these
# coding-agent engines — see ``image_capable`` below.
TEXT_PURPOSES = ("master", "reviewer", "script", "code")

# How the engine authenticates.
AUTH_OAUTH_SUBSCRIPTION = "oauth_subscription"  # consumer plan via OAuth (preferred)
AUTH_API_KEY = "api_key"                         # per-call API key
AUTH_UNKNOWN = "unknown"                          # logged in, but method/tier NOT verifiable
AUTH_NONE = "none"                               # installed but not authenticated
AUTH_NOT_INSTALLED = "not_installed"             # binary absent

# Fail-closed rule: subscription_backed=True is set ONLY when the vendor CLI
# reports explicit, known consumer-OAuth evidence. Anything ambiguous — an
# unrecognized auth method, a generic "logged in" string, a credential file of
# unknown validity — is reported as AUTH_UNKNOWN and is NEVER subscription-ready.


@dataclass
class ProbeResult:
    """Outcome of running a vendor status command (or failing to)."""
    ran: bool
    returncode: int
    stdout: str
    stderr: str


def _default_runner(cmd: list[str], timeout: int) -> ProbeResult:
    """Run a short, read-only status command. Never raises; never echoes secrets."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return ProbeResult(True, proc.returncode, proc.stdout or "", proc.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as exc:  # pragma: no cover - env-specific
        return ProbeResult(False, -1, "", str(exc))


Runner = Callable[[list[str], int], ProbeResult]
Which = Callable[[str], Optional[str]]


@dataclass
class EngineStatus:
    """Non-secret capability report for one subscription engine."""

    id: str
    name: str
    binary: str
    installed: bool
    auth_method: str                       # AUTH_* constant
    logged_in: bool
    subscription_backed: bool              # True only for a genuine consumer-plan OAuth session
    subscription_type: Optional[str] = None  # e.g. "max", "pro" — reported by the CLI, non-secret
    supported_purposes: list[str] = field(default_factory=list)
    image_capable: bool = False
    image_blocker: Optional[str] = None    # why image gen is NOT available via this engine
    api_key_alternative: Optional[str] = None  # env var name of an optional API-key fallback, if set
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-engine probes. Each returns (auth_method, logged_in, subscription_type,
# extra_notes) given a ProbeResult — parsing ONLY non-secret fields.
# ---------------------------------------------------------------------------

def _parse_claude_auth(probe: ProbeResult) -> tuple[str, bool, Optional[str], list[str]]:
    """Claude Code: ``claude auth status --json`` -> loggedIn/authMethod/subscriptionType.

    We intentionally read only these three fields. The command also emits
    email/orgId/orgName — identity, not credentials — which we never surface.
    """
    notes: list[str] = []
    if not probe.ran:
        return AUTH_NONE, False, None, ["claude auth status did not run"]
    payload: Optional[dict] = None
    text = probe.stdout.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, dict):
        # Unparseable output — we have NO explicit evidence of anything. Fail
        # closed: never subscription-backed, never even "logged in" (a garbage
        # exit-0 is not proof of a session). AUTH_UNKNOWN with logged_in=False.
        return AUTH_UNKNOWN, False, None, [
            "could not parse `claude auth status --json` output; auth unverified",
        ]

    logged_in = bool(payload.get("loggedIn"))
    auth_method_raw = str(payload.get("authMethod") or "").lower()
    subscription_type = payload.get("subscriptionType")
    subscription_type = str(subscription_type) if subscription_type else None

    if not logged_in:
        return AUTH_NONE, False, None, notes
    # API-key detection MUST come first: an ``apiKey`` authMethod is a key even
    # when apiProvider=="firstParty" (a first-party API key is still a key, not a
    # consumer OAuth subscription).
    if "apikey" in auth_method_raw.replace("_", "") or "console" in auth_method_raw:
        return AUTH_API_KEY, True, subscription_type, ["authenticated via API key, not consumer OAuth"]
    # Explicit consumer-OAuth evidence is authMethod naming claude.ai. apiProvider
    # =="firstParty" ALONE is NOT sufficient (see above), so it is not accepted here.
    if "claude.ai" in auth_method_raw:
        return AUTH_OAUTH_SUBSCRIPTION, True, subscription_type, notes
    # Logged in, but via a method we don't explicitly recognize. Fail closed.
    return AUTH_UNKNOWN, True, subscription_type, [
        f"logged in via unrecognized authMethod {auth_method_raw!r}; not treated as a "
        "verified consumer subscription",
    ]


def _parse_codex_auth(probe: ProbeResult) -> tuple[str, bool, Optional[str], list[str]]:
    """Codex CLI: ``codex login status`` -> a one-line human string, no tokens.

    Observed outputs: "Logged in using ChatGPT" (consumer OAuth),
    "Logged in using an API key" (per-call), or a not-logged-in message.
    """
    if not probe.ran:
        return AUTH_NONE, False, None, ["codex login status did not run"]
    text = (probe.stdout + "\n" + probe.stderr).lower()
    # NEGATIVE forms first — "Not logged in using ChatGPT" contains both "chatgpt"
    # and (as a substring) "logged in", so a naive positive match would fail open.
    negatives = ("not logged in", "logged out", "not signed in", "no credentials",
                 "not authenticated", "please log in", "please login")
    if any(neg in text for neg in negatives) or "logged in" not in text:
        return AUTH_NONE, False, None, []
    # We now know it's an affirmative "logged in" line. Strict positive matching:
    if "chatgpt" in text:
        return AUTH_OAUTH_SUBSCRIPTION, True, "chatgpt", []
    if "api key" in text:
        return AUTH_API_KEY, True, None, ["authenticated via API key, not ChatGPT OAuth"]
    if probe.returncode == 0:
        # Affirmative but unrecognized method. Fail closed — not a subscription.
        return AUTH_UNKNOWN, True, None, [
            "logged in via an unrecognized method; not treated as a verified ChatGPT subscription",
        ]
    return AUTH_NONE, False, None, []


def _gemini_creds_present() -> bool:
    """Whether Gemini CLI's Google OAuth creds file exists at ~/.gemini/oauth_creds.json.

    Presence is a WEAK hint only — it does not prove the token is currently valid
    or that the account carries any particular plan/tier. We check presence (never
    read the file) and surface it as a note; it never sets logged_in or
    subscription_backed. The Gemini CLI exposes no non-secret status subcommand we
    can rely on, so auth is reported as unknown/unverified when there's no API key.
    """
    try:
        return (Path.home() / ".gemini" / "oauth_creds.json").is_file()
    except OSError:  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# Engine catalog
# ---------------------------------------------------------------------------

# Shared blocker: none of these coding-agent OAuth sessions expose a
# programmatic image-generation endpoint. This is the honest answer to
# "image generation via ChatGPT/Claude subscription".
_GEMINI_CUTOVER_NOTE = (
    "Consumer Gemini OAuth moved to the Antigravity CLI on 2026-06-18. For a consumer "
    "Google sign-in use 'Google AI (Antigravity OAuth)'; the Gemini CLI is enterprise / API-key only."
)

_IMAGE_BLOCKER_OAUTH = (
    "This engine is a coding-agent CLI; its subscription/OAuth session exposes "
    "no programmatic image-generation endpoint. ChatGPT/Claude/Gemini image "
    "models are not reachable through it. Use an image_generation provider "
    "(local ComfyUI, free Pexels/Pixabay stock, or an API-key model) instead."
)


def _discover_claude(runner: Runner, which: Which, timeout: int, probe_auth: bool) -> EngineStatus:
    binary = "claude"
    path = which(binary)
    if not path:
        return EngineStatus(
            id="claude", name="Claude Code (Anthropic)", binary=binary, installed=False,
            auth_method=AUTH_NOT_INSTALLED, logged_in=False, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH,
            blockers=["claude CLI not found on PATH — install Claude Code to use the Max/Pro OAuth path"],
        )
    probe = runner([binary, "auth", "status", "--json"], timeout)
    auth_method, logged_in, sub_type, notes = _parse_claude_auth(probe)
    alt = "ANTHROPIC_API_KEY" if os.environ.get("ANTHROPIC_API_KEY") else None
    if alt:
        notes = [*notes, "ANTHROPIC_API_KEY is also set and can serve as an explicit API fallback"]
    blockers: list[str] = []
    if not logged_in:
        blockers.append("installed but not logged in — run `claude auth login` (Max/Pro OAuth)")
    return EngineStatus(
        id="claude", name="Claude Code (Anthropic)", binary=binary, installed=True,
        auth_method=auth_method, logged_in=logged_in,
        subscription_backed=(auth_method == AUTH_OAUTH_SUBSCRIPTION and logged_in),
        subscription_type=sub_type, supported_purposes=list(TEXT_PURPOSES),
        image_capable=False, image_blocker=_IMAGE_BLOCKER_OAUTH,
        api_key_alternative=alt, blockers=blockers, notes=notes,
    )


def _discover_codex(runner: Runner, which: Which, timeout: int, probe_auth: bool) -> EngineStatus:
    binary = "codex"
    path = which(binary)
    if not path:
        return EngineStatus(
            id="codex", name="Codex (OpenAI / ChatGPT)", binary=binary, installed=False,
            auth_method=AUTH_NOT_INSTALLED, logged_in=False, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH,
            blockers=["codex CLI not found on PATH — install Codex to use the ChatGPT OAuth path"],
        )
    probe = runner([binary, "login", "status"], timeout)
    auth_method, logged_in, sub_type, notes = _parse_codex_auth(probe)
    alt = "OPENAI_API_KEY" if os.environ.get("OPENAI_API_KEY") else None
    if alt:
        notes = [*notes, "OPENAI_API_KEY is also set and can serve as an explicit API fallback"]
    blockers: list[str] = []
    if not logged_in:
        blockers.append("installed but not logged in — run `codex login` (ChatGPT OAuth)")
    return EngineStatus(
        id="codex", name="Codex (OpenAI / ChatGPT)", binary=binary, installed=True,
        auth_method=auth_method, logged_in=logged_in,
        subscription_backed=(auth_method == AUTH_OAUTH_SUBSCRIPTION and logged_in),
        subscription_type=sub_type, supported_purposes=list(TEXT_PURPOSES),
        image_capable=False, image_blocker=_IMAGE_BLOCKER_OAUTH,
        api_key_alternative=alt, blockers=blockers, notes=notes,
    )


def _discover_gemini(runner: Runner, which: Which, timeout: int, probe_auth: bool) -> EngineStatus:
    binary = "gemini"
    path = which(binary)
    alt = None
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(name):
            alt = name
            break
    if not path:
        return EngineStatus(
            id="gemini", name="Gemini CLI (Google — enterprise / API key)", binary=binary, installed=False,
            auth_method=AUTH_NOT_INSTALLED, logged_in=False, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH, api_key_alternative=alt,
            blockers=["gemini CLI not found on PATH. For a CONSUMER Google sign-in use Antigravity (agy) — the current path. The Gemini CLI covers enterprise / API-key usage."],
            notes=[_GEMINI_CUTOVER_NOTE],
        )
    # An API key IS a verifiable auth mode (the key is present/usable), but it is
    # NOT a consumer subscription — never subscription-backed.
    if alt:
        return EngineStatus(
            id="gemini", name="Gemini CLI (Google — enterprise / API key)", binary=binary, installed=True,
            auth_method=AUTH_API_KEY, logged_in=True, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH, api_key_alternative=alt,
            notes=[f"using {alt} (API key) — enterprise / API-key path. This is not a consumer subscription/tier.", _GEMINI_CUTOVER_NOTE],
        )
    # No API key. The Gemini CLI exposes no non-secret status subcommand we can
    # rely on, and a Google OAuth login proves neither a valid current session
    # nor any consumer tier. Fail closed: auth unknown, NEVER subscription-ready.
    creds_note = []
    if probe_auth and _gemini_creds_present():
        creds_note = [
            "An OAuth creds file is present (~/.gemini/oauth_creds.json), but its "
            "validity and plan/tier are NOT verifiable via the CLI — reported as unverified.",
        ]
    return EngineStatus(
        id="gemini", name="Gemini CLI (Google — enterprise / API key)", binary=binary, installed=True,
        auth_method=AUTH_UNKNOWN, logged_in=False, subscription_backed=False,
        supported_purposes=list(TEXT_PURPOSES), image_capable=False,
        image_blocker=_IMAGE_BLOCKER_OAUTH, api_key_alternative=alt,
        blockers=[
            "Auth not verifiable via the Gemini CLI. Sign in with `gemini` (Google OAuth) "
            "or set GEMINI_API_KEY. Not treated as subscription-ready until verified.",
        ],
        notes=[_GEMINI_CUTOVER_NOTE, *creds_note],
    )


def _discover_zai(runner: Runner, which: Which, timeout: int, probe_auth: bool) -> EngineStatus:
    """Z.AI (GLM) has no universal OAuth CLI. Its supported paths are honest:

      1. An API token (ZAI_API_KEY / ZHIPUAI_API_KEY / Z_AI_API_KEY), or
      2. Claude Code pointed at Z.AI's Anthropic-compatible endpoint
         (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN).

    We detect these without asserting an OAuth device flow the tool doesn't have.
    """
    key_var = None
    for name in ("ZAI_API_KEY", "ZHIPUAI_API_KEY", "Z_AI_API_KEY", "GLM_API_KEY"):
        if os.environ.get(name):
            key_var = name
            break
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    proxy_via_claude = "z.ai" in base.lower() and bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    notes = [
        "Z.AI's GLM coding plan is consumed via an API token or by pointing Claude "
        "Code at its Anthropic-compatible endpoint (ANTHROPIC_BASE_URL) — it is not "
        "an OAuth device-flow CLI.",
    ]
    if key_var or proxy_via_claude:
        detail = key_var or "Claude-Code proxy (ANTHROPIC_BASE_URL -> z.ai)"
        return EngineStatus(
            id="zai", name="Z.AI (GLM)", binary="-", installed=True,
            auth_method=AUTH_API_KEY, logged_in=True, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH,
            notes=[*notes, f"configured via {detail}"],
        )
    return EngineStatus(
        id="zai", name="Z.AI (GLM)", binary="-", installed=False,
        auth_method=AUTH_NOT_INSTALLED, logged_in=False, subscription_backed=False,
        supported_purposes=list(TEXT_PURPOSES), image_capable=False,
        image_blocker=_IMAGE_BLOCKER_OAUTH,
        blockers=[
            "No Z.AI credential found. Set ZAI_API_KEY (or ZHIPUAI_API_KEY), or point "
            "Claude Code at Z.AI via ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN.",
        ],
        notes=notes,
    )


def _discover_antigravity(runner: Runner, which: Which, timeout: int, probe_auth: bool) -> EngineStatus:
    """Google's CURRENT consumer-OAuth coding engine (Antigravity CLI `agy`).

    Consumer Gemini OAuth moved here on 2026-06-18. Auth state comes from the
    documented non-interactive `agy models` probe (fail-closed) — never identity,
    never a claimed plan tier (the CLI cannot verify one)."""
    from lib import antigravity
    name = "Google AI (Antigravity OAuth)"
    if not antigravity.is_installed():
        return EngineStatus(
            id="antigravity", name=name, binary="agy", installed=False,
            auth_method=AUTH_NOT_INSTALLED, logged_in=False, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH,
            blockers=["Antigravity CLI (agy) not installed — install it for the current Google consumer OAuth path."],
            notes=[antigravity.CONSUMER_CUTOVER_NOTE],
        )
    if not probe_auth:
        return EngineStatus(
            id="antigravity", name=name, binary="agy", installed=True,
            auth_method=AUTH_UNKNOWN, logged_in=False, subscription_backed=False,
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH, notes=["auth not probed"],
        )
    st = antigravity.probe_status(timeout=timeout)
    if st.get("signed_in"):
        return EngineStatus(
            id="antigravity", name=name, binary="agy", installed=True,
            auth_method=AUTH_OAUTH_SUBSCRIPTION, logged_in=True, subscription_backed=True,
            subscription_type=None,  # CLI cannot verify a plan tier — never claim one
            supported_purposes=list(TEXT_PURPOSES), image_capable=False,
            image_blocker=_IMAGE_BLOCKER_OAUTH,
            notes=["signed in via Antigravity (Google) OAuth; plan tier not asserted"],
        )
    return EngineStatus(
        id="antigravity", name=name, binary="agy", installed=True,
        auth_method=AUTH_NONE, logged_in=False, subscription_backed=False,
        supported_purposes=list(TEXT_PURPOSES), image_capable=False,
        image_blocker=_IMAGE_BLOCKER_OAUTH,
        blockers=["Installed but not signed in — use Connect Google (opens a terminal + browser)."],
        notes=[antigravity.CONSUMER_CUTOVER_NOTE],
    )


_DISCOVERERS: dict[str, Callable[[Runner, Which, int, bool], EngineStatus]] = {
    "claude": _discover_claude,
    "codex": _discover_codex,
    "antigravity": _discover_antigravity,
    "gemini": _discover_gemini,
    "zai": _discover_zai,
}

ENGINE_IDS = tuple(_DISCOVERERS.keys())


def discover_engines(
    *,
    probe_auth: bool = True,
    runner: Optional[Runner] = None,
    which: Optional[Which] = None,
    timeout: int = 8,
    only: Optional[list[str]] = None,
) -> list[EngineStatus]:
    """Return the non-secret capability status of every known engine.

    Args:
        probe_auth: when False, skip the subprocess auth probe (fast path for
            UIs that only need install state) — logged_in is reported False.
        runner/which: injectable for tests; default to subprocess/shutil.which.
        timeout: per-probe subprocess timeout (seconds).
        only: restrict to a subset of engine ids.
    """
    run: Runner = runner or _default_runner
    where: Which = which or shutil.which
    if not probe_auth:
        # A no-op runner keeps parsers honest (they see ran=False -> not logged in).
        run = lambda cmd, t: ProbeResult(False, -1, "", "auth probe skipped")
    ids = only or list(ENGINE_IDS)
    out: list[EngineStatus] = []
    for engine_id in ids:
        discoverer = _DISCOVERERS.get(engine_id)
        if discoverer is None:
            continue
        try:
            out.append(discoverer(run, where, timeout, probe_auth))
        except Exception as exc:  # never let one probe break the whole scan
            out.append(EngineStatus(
                id=engine_id, name=engine_id, binary=engine_id, installed=False,
                auth_method=AUTH_UNKNOWN, logged_in=False, subscription_backed=False,
                supported_purposes=list(TEXT_PURPOSES), image_capable=False,
                blockers=[f"probe error: {type(exc).__name__}"],  # sanitized: no exc detail to UI
            ))
    return out


def engines_summary(**kwargs) -> dict:
    """UI/preflight-ready rollup: engine list + convenience aggregates.

    Purely non-secret. ``subscription_ready`` lists engines usable *right now*
    on a consumer plan — the ones the subscription-first router should prefer.
    """
    engines = discover_engines(**kwargs)
    ready = [e.id for e in engines if e.subscription_backed and e.logged_in]
    return {
        "engines": [e.to_dict() for e in engines],
        "text_purposes": list(TEXT_PURPOSES),
        "subscription_ready": ready,
        "any_subscription_ready": bool(ready),
        "image_via_subscription_supported": any(e.image_capable for e in engines),
    }
