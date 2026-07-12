"""Remotion runtime detection + doctor (sanitized, dependency-free).

Grounds Backlot's "is Remotion available?" answer in REAL, concrete checks — a
resolvable `node` of a supported major, the composer project + lockfile, an
installed `node_modules` whose `remotion` package version satisfies the project
requirement, the PINNED local CLI bin, the composition entry, AND a usable
browser executable. A render is not possible without a browser, so the real
`available` (render-ready) verdict includes it; `installed` reports package
readiness separately.

Design rules baked in here:
  * Render argv uses the PINNED local CLI (``node_modules/.bin/remotion``), NEVER
    ``npx remotion`` (which can hit the network when local resolution breaks).
  * The browser is resolved by SAFE discovery only — an env override is honored
    ONLY when it points under an allowlisted cache/app root; there is no
    user-specific hardcoded path in source and no arbitrary path from a request.
  * Everything returned is UI-safe: booleans + short generic strings + a coarse
    ``browser_source`` label, never absolute paths or raw exception text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_MIN_NODE_MAJOR = 18  # Remotion 4.x supports Node 18/20/22 (and newer); 24 works.

# Package/tooling checks (installed-ness). `browser` is tracked separately and
# gates render-readiness. Global `npx` is intentionally NOT gating — render uses
# the pinned local CLI; npx/npm matter only to the installer.
_INSTALL_CHECKS = ("node", "node_version", "project", "lockfile",
                   "node_modules", "remotion_pkg", "version_match", "cli_bin", "entry")
# Ordered reason lookup; FIRST failing check wins. `browser` last: packages can be
# fine while the browser is the only thing missing (that's what Repair fixes).
_CHECK_ORDER = _INSTALL_CHECKS + ("browser",)

_REASONS = {
    "node": "Node.js is not installed or not on PATH.",
    "node_version": f"Node.js is too old — Remotion needs Node {_MIN_NODE_MAJOR}+.",
    "project": "The remotion-composer project is missing (no package.json).",
    "lockfile": "The remotion-composer lockfile (package-lock.json) is missing.",
    "node_modules": "Remotion dependencies are not installed — run the install action "
                    "(or `make remotion-install`).",
    "remotion_pkg": "The installed 'remotion' package is missing or corrupt — reinstall dependencies.",
    "version_match": "The installed Remotion version does not satisfy the project requirement — reinstall.",
    "cli_bin": "The Remotion CLI binary is missing — reinstall dependencies.",
    "entry": "The composition entry point (src/index.tsx) is missing.",
    "browser": "No usable browser was found for Remotion — run Repair to download the "
               "Remotion browser (or install Chrome/Chromium).",
}


def composer_dir(composer: Optional[Path] = None) -> Path:
    return composer if composer is not None else (REPO_ROOT / "remotion-composer")


def cli_bin_path(composer: Optional[Path] = None) -> Path:
    """Absolute path to the PINNED local Remotion CLI. Render paths MUST invoke
    this argv directly (never ``npx remotion``)."""
    return composer_dir(composer) / "node_modules" / ".bin" / "remotion"


# --- Safe browser discovery -------------------------------------------------
# Standard, well-known system browser install locations (macOS + Linux).
_SYSTEM_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
_WHICH_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


def _browser_roots(home: Path) -> list[Path]:
    """Allowlisted base dirs an env-provided or cached browser may live under."""
    return [
        home / "Library" / "Caches" / "remotion",       # Remotion-managed (macOS)
        home / ".cache" / "remotion",                    # Remotion-managed (Linux)
        home / ".cache" / "puppeteer",                   # puppeteer chrome-headless-shell
        Path("/Applications"),
        Path("/opt"), Path("/usr/bin"), Path("/usr/local/bin"),
    ]


def _under_allowed_root(p: Path, home: Path) -> bool:
    try:
        rp = p.resolve()
    except Exception:
        return False
    for root in _browser_roots(home):
        try:
            rp.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _first_cache_browser(home: Path) -> Optional[str]:
    for base in (home / ".cache" / "puppeteer", home / "Library" / "Caches" / "remotion",
                 home / ".cache" / "remotion"):
        if not base.exists():
            continue
        for name in ("chrome-headless-shell", "chrome", "chromium", "Chromium",
                     "Google Chrome for Testing"):
            try:
                for exe in base.rglob(name):
                    if exe.is_file() and os.access(exe, os.X_OK):
                        return str(exe)
            except OSError:
                continue
    return None


def resolve_browser(*, env: Optional[dict] = None, which: Callable = shutil.which,
                    home: Optional[Path] = None,
                    system_paths=_SYSTEM_BROWSERS) -> tuple[Optional[str], Optional[str]]:
    """Return ``(executable_path, source)`` for a usable browser, or ``(None, None)``.

    Discovery order (all SAFE — no arbitrary request path, no hardcoded user path):
      1. ``REMOTION_BROWSER_EXECUTABLE`` env — accepted ONLY if it is a file under
         an allowlisted root (defense against a poisoned env pointing anywhere).
      2. Standard system Chrome/Chromium install paths.
      3. ``which`` for chrome/chromium on PATH.
      4. Remotion-managed / puppeteer browser caches under $HOME.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    p = (env.get("REMOTION_BROWSER_EXECUTABLE") or "").strip()
    if p:
        pp = Path(p)
        if pp.is_file() and _under_allowed_root(pp, home):
            return str(pp), "env"

    for cand in system_paths:
        if Path(cand).is_file():
            return cand, "system"

    for name in _WHICH_BROWSERS:
        w = which(name)
        if w and Path(w).exists():
            return w, "path"

    cached = _first_cache_browser(home)
    if cached:
        return cached, "cache"
    return None, None


def browser_executable(**kw) -> Optional[str]:
    return resolve_browser(**kw)[0]


# --- Version helpers --------------------------------------------------------
def _default_runner(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception:
        return -1, "", ""


def _node_major(runner: Callable, timeout: int) -> Optional[int]:
    rc, out, _ = runner(["node", "--version"], timeout)
    if rc != 0:
        return None
    m = re.search(r"v?(\d+)\.", out.strip())
    return int(m.group(1)) if m else None


def _installed_version(nm: Path) -> Optional[str]:
    pkg = nm / "remotion" / "package.json"
    if not pkg.is_file():
        return None
    try:
        return str(json.loads(pkg.read_text()).get("version") or "") or None
    except Exception:
        return None


def _required_major(project_pkg: Path) -> Optional[int]:
    try:
        dep = json.loads(project_pkg.read_text()).get("dependencies", {}).get("remotion", "")
    except Exception:
        return None
    m = re.search(r"(\d+)\.", str(dep))
    return int(m.group(1)) if m else None


def doctor(*, composer: Optional[Path] = None, which: Callable = shutil.which,
           runner: Callable = _default_runner, timeout: int = 15,
           browser_resolver: Optional[Callable[[], tuple]] = None) -> dict:
    """Sanitized runtime report.

    Shape: ``{available, installed, render_ready, reason, version, node_version,
    browser_source, checks:{...bool}}``. ``installed`` = packages ready.
    ``available``/``render_ready`` additionally require a usable browser — this is
    what the runtime selector promises, so a package-only install is NOT
    "available".
    """
    d = composer_dir(composer)
    project_pkg = d / "package.json"
    nm = d / "node_modules"

    node_major = _node_major(runner, timeout)
    installed_ver = _installed_version(nm) if nm.is_dir() else None
    required_major = _required_major(project_pkg) if project_pkg.is_file() else None
    version_ok = bool(installed_ver and required_major
                      and installed_ver.split(".")[0].isdigit()
                      and int(installed_ver.split(".")[0]) == required_major)

    if browser_resolver is not None:
        browser_path, browser_source = browser_resolver()
    else:
        browser_path, browser_source = resolve_browser(which=which)

    checks = {
        "node": node_major is not None,
        "node_version": node_major is not None and node_major >= _MIN_NODE_MAJOR,
        "npx": which("npx") is not None,  # reported for info; NON-gating
        "project": project_pkg.is_file(),
        "lockfile": (d / "package-lock.json").is_file(),
        "node_modules": nm.is_dir(),
        "remotion_pkg": installed_ver is not None,
        "version_match": version_ok,
        "cli_bin": (nm / ".bin" / "remotion").exists(),
        "entry": (d / "src" / "index.tsx").is_file(),
        "browser": bool(browser_path),
    }

    installed = all(checks[c] for c in _INSTALL_CHECKS)
    reason = ""
    for name in _CHECK_ORDER:
        if not checks[name]:
            reason = _REASONS[name]
            break
    available = reason == ""

    return {
        "available": available,
        "render_ready": available,
        "installed": installed,
        "reason": reason,
        "version": installed_ver,
        "node_version": f"v{node_major}" if node_major is not None else None,
        "browser_source": browser_source if browser_path else None,
        "checks": checks,
    }


def is_available(**kw) -> bool:
    """True only when genuinely render-ready (packages + browser)."""
    return bool(doctor(**kw)["available"])


def render_argv(entry, comp_id, output, *, props: Optional[str] = None,
                extra: Optional[list] = None, composer: Optional[Path] = None,
                browser: Optional[str] = None) -> list[str]:
    """Build a Remotion render argv using the PINNED local CLI and the resolved
    browser executable. Never contains ``npx``."""
    argv = [str(cli_bin_path(composer)), "render", str(entry), str(comp_id), str(output)]
    if props:
        argv.append(f"--props={props}")
    be = browser if browser is not None else browser_executable()
    if be:
        argv.append(f"--browser-executable={be}")
    if extra:
        argv.extend(str(x) for x in extra)
    return argv


def still_argv(entry, comp_id, output, *, frame: int = 0, props: Optional[str] = None,
               extra: Optional[list] = None, composer: Optional[Path] = None,
               browser: Optional[str] = None) -> list[str]:
    """Build a Remotion ``still`` argv (single-frame PNG) using the PINNED local
    CLI and resolved browser. Never contains ``npx``."""
    argv = [str(cli_bin_path(composer)), "still", str(entry), str(comp_id), str(output),
            f"--frame={int(frame)}"]
    if props:
        argv.append(f"--props={props}")
    be = browser if browser is not None else browser_executable()
    if be:
        argv.append(f"--browser-executable={be}")
    if extra:
        argv.extend(str(x) for x in extra)
    return argv
