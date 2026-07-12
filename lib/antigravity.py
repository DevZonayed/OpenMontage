"""Antigravity CLI (`agy`) — Google's current consumer-OAuth coding engine.

Google moved consumer Gemini OAuth to the Antigravity CLI on 2026-06-18; the
legacy Gemini CLI remains for enterprise / API-key use. This module discovers
`agy`, reports auth status via its documented non-interactive probe, and installs
it with official checksum verification. Everything here is grounded in the ACTUAL
installed CLI contract (verified via `agy --help` / `agy models`), not guesses:

  * `agy` — no subcommand — launches the interactive browser sign-in.
  * `agy models` — non-interactive; prints "Please sign in …" when not
    authenticated, or lists models when signed in. We use this as the status
    probe and FAIL CLOSED on any ambiguity: `signed_in` is True ONLY when the
    output carries affirmative model-list evidence (a real model id). A benign
    banner, warning, help text, or error — even with exit 0 — is treated as NOT
    signed in. The CLI has no `--json`/machine format, so an unknown future
    listing format also fails closed until the matcher is updated. Non-billable.
  * There is no `logout` subcommand — sign-out is interactive (TUI).

We never parse or return identity (email/account) — only allowlisted booleans.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

CONSUMER_CUTOVER_NOTE = (
    "Google moved consumer Gemini OAuth to the Antigravity CLI on 2026-06-18. "
    "Use 'Google AI (Antigravity OAuth)' for a consumer sign-in; the Gemini CLI "
    "now covers enterprise / API-key usage only."
)

# Official install metadata (verified: user-local, sha512-checked).
_MANIFEST_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
_LOCAL_BIN = Path.home() / ".local" / "bin" / "agy"

# Phrases that mean "not authenticated" in the `agy models` probe output.
_SIGNIN_MARKERS = ("sign in", "sign-in", "log in", "log-in", "please sign", "not authenticated",
                   "unauthorized", "not signed in", "authenticate")

# Substrings that mean the probe did NOT return a clean model listing — help
# text, usage banners, or transient failures. Any of these => fail closed, even
# with exit code 0 (an unrelated rc==0 banner must never read as OAuth-green).
_ERROR_MARKERS = ("error", "failed", "failure", "cannot", "could not", "unable",
                  "not defined", "usage:", "flags:", "try again", "timeout",
                  "timed out", "panic", "unexpected", "denied", "forbidden",
                  "expired", "quota", "rate limit", "network", "no models")

# Affirmative evidence of a REAL model listing: at least one model id such as
# "gemini-2.5-pro", "claude-sonnet-4", "gpt-4o", "o3-mini", "models/gemini-1.5".
# `agy models` (v1.1.1) exposes NO machine-readable/`--json` format, so we
# validate the human listing conservatively. A signed-in result requires this
# evidence — never merely non-empty output. An unknown FUTURE output format with
# no recognizable model token therefore FAILS CLOSED until this matcher is
# updated (a deliberate safe default, not a silent green).
_MODEL_LINE = re.compile(
    r"(?:^|[\s/(\[])(?:gemini|claude|gpt|grok|llama|palm|mistral|qwen|deepseek|o[1-4])"
    r"[-/][a-z0-9][a-z0-9.\-]*",
    re.IGNORECASE,
)


def agy_path() -> Optional[str]:
    """Locate the agy binary even if PATH is stale (search ~/.local/bin too)."""
    p = shutil.which("agy")
    if p:
        return p
    if _LOCAL_BIN.is_file() and os.access(_LOCAL_BIN, os.X_OK):
        return str(_LOCAL_BIN)
    return None


def is_installed() -> bool:
    return agy_path() is not None


def _run_probe(path: str, timeout: int, runner: Optional[Callable]) -> tuple[int, str]:
    """Run `agy models` non-interactively (stdin closed). Returns (rc, combined_lower_text)."""
    if runner is not None:
        rc, out, err = runner([path, "models"], timeout)
        return rc, ((out or "") + "\n" + (err or "")).lower()
    try:
        proc = subprocess.run(
            [path, "models"], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout, check=False,
            env={**os.environ, "CI": "1"},
        )
        return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    except Exception:
        return -1, ""


def probe_status(*, timeout: int = 20, runner: Optional[Callable] = None) -> dict:
    """Sanitized auth status via `agy models`. Fail-closed: signed_in is True only
    on an unambiguous authenticated listing.

    Returns {installed, signed_in, probe_ran}. Never identity, never raw output.
    """
    path = agy_path()
    if not path:
        return {"installed": False, "signed_in": False, "probe_ran": False}
    rc, text = _run_probe(path, timeout, runner)
    if rc != 0 and rc != -1:
        # Non-zero from something other than the sign-in message → not signed in.
        return {"installed": True, "signed_in": False, "probe_ran": True}
    if rc == -1:
        # Probe failed to run (timeout/exec) → cannot confirm → fail closed.
        return {"installed": True, "signed_in": False, "probe_ran": False}
    if any(m in text for m in _SIGNIN_MARKERS):
        return {"installed": True, "signed_in": False, "probe_ran": True}
    # rc == 0, no sign-in marker: still fail closed on help/banner/error output.
    if any(m in text for m in _ERROR_MARKERS):
        return {"installed": True, "signed_in": False, "probe_ran": True}
    # Positive result REQUIRES affirmative model-list evidence — never merely a
    # non-empty stdout. This blocks a benign warning/banner (exit 0) from being
    # mistaken for a signed-in OAuth session.
    signed_in = bool(_MODEL_LINE.search(text))
    return {"installed": True, "signed_in": signed_in, "probe_ran": True}


# ---------------------------------------------------------------------------
# Install (official manifest + sha512 verification, user-local)
# ---------------------------------------------------------------------------

class AntigravityInstallError(RuntimeError):
    """UI-safe install failure (no internal paths/exceptions)."""


def _platform_slug() -> str:
    import platform
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    sysname = platform.system().lower()
    osk = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(sysname, sysname)
    return f"{osk}_{arch}"


def install(*, http_get: Optional[Callable] = None, timeout: int = 120) -> dict:
    """Install agy user-locally: fetch the official manifest, download the binary,
    VERIFY its sha512, extract, and place at ~/.local/bin/agy. Mirrors the vetted
    official installer's security steps. Returns {installed, version}.

    ``http_get`` (bytes downloader) is injectable for tests.
    """
    import hashlib
    import json as _json
    import tarfile
    import tempfile

    def _get_bytes(url: str) -> bytes:
        if http_get is not None:
            return http_get(url)
        import requests
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content

    slug = _platform_slug()
    try:
        manifest = _json.loads(_get_bytes(f"{_MANIFEST_BASE}/manifests/{slug}.json").decode("utf-8"))
        version = str(manifest["version"])
        url = str(manifest["url"])
        sha512 = str(manifest["sha512"]).lower()
    except Exception:
        raise AntigravityInstallError("Could not fetch the official release manifest.")

    if not url.startswith("https://storage.googleapis.com/"):
        raise AntigravityInstallError("Release URL is not an official Google Storage origin — aborting.")

    payload = _get_bytes(url)
    if hashlib.sha512(payload).hexdigest().lower() != sha512:
        # Same "Security Halt" semantics as the official installer.
        raise AntigravityInstallError("Downloaded package checksum did not match the manifest — aborting.")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        binary_src: Optional[Path] = None
        if url.endswith(".tar.gz") or url.endswith(".tgz"):
            arc = tdp / "agy.tar.gz"
            arc.write_bytes(payload)
            with tarfile.open(arc, "r:gz") as tf:
                # Extract only a member named 'antigravity' (no path traversal).
                member = next((m for m in tf.getmembers()
                               if Path(m.name).name == "antigravity" and m.isfile()), None)
                if member is None:
                    raise AntigravityInstallError("Release archive did not contain the expected binary.")
                member.name = "antigravity"
                tf.extract(member, tdp)
            binary_src = tdp / "antigravity"
        else:
            binary_src = tdp / "agy"
            binary_src.write_bytes(payload)

        _LOCAL_BIN.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(binary_src, _LOCAL_BIN)
            _LOCAL_BIN.chmod(0o755)
        except OSError:
            raise AntigravityInstallError("Could not write the binary to ~/.local/bin (permission denied).")

    # Best-effort quarantine removal on macOS (non-fatal).
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(_LOCAL_BIN)],
                       capture_output=True, timeout=10)
    except Exception:
        pass

    return {"installed": is_installed(), "version": version}


# Fixed, allowlisted interactive commands (no user interpolation). The UI opens a
# Terminal running these; sign-in / sign-out happen interactively in that window.
def sign_in_command() -> list[str]:
    """`agy` with no args launches the browser sign-in."""
    return [agy_path() or "agy"]


def sign_out_note() -> str:
    return ("Antigravity has no non-interactive sign-out. Open a session with `agy` "
            "and use the in-session sign-out (e.g. /logout).")
