"""Remotion runtime doctor — real, sanitized, render-ready availability checks.

Contract (drives the implementation): availability means genuinely RENDER-READY
— node + local package parity + entry + a usable browser — not a single dir stat.
`installed` (packages) is reported separately from `available` (render-ready).
Render argv uses the PINNED local CLI, never `npx`. Browser discovery is SAFE
(env override only under allowlisted roots; no arbitrary/hardcoded paths). Every
returned string is UI-safe (no absolute paths, no raw exception text).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lib import remotion_runtime as rr

_OK_BROWSER = lambda: ("/ok/chrome", "system")
_NO_BROWSER = lambda: (None, None)


def _make_composer(tmp_path: Path, *, installed="4.0.484", node_modules=True,
                   remotion_pkg=True, cli_bin=True, lockfile=True, entry=True,
                   required="^4.0.484") -> Path:
    d = tmp_path / "remotion-composer"
    (d / "src").mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({
        "dependencies": {"remotion": required, "@remotion/cli": required}}))
    if lockfile:
        (d / "package-lock.json").write_text("{}")
    if entry:
        (d / "src" / "index.tsx").write_text("// entry")
    if node_modules:
        nm = d / "node_modules"
        nm.mkdir()
        if remotion_pkg:
            (nm / "remotion").mkdir()
            (nm / "remotion" / "package.json").write_text(
                json.dumps({"name": "remotion", "version": installed}))
        if cli_bin:
            (nm / ".bin").mkdir()
            (nm / ".bin" / "remotion").write_text("#!/bin/sh\n")
    return d


def _which_all(name):
    return f"/usr/bin/{name}"


def _runner_node(version="v22.23.1"):
    def run(cmd, timeout):
        if cmd[:2] == ["node", "--version"]:
            return (0, version + "\n", "")
        return (0, "", "")
    return run


def _doctor(d, *, browser=_OK_BROWSER, which=_which_all, runner=None, **kw):
    return rr.doctor(composer=d, which=which, runner=runner or _runner_node(),
                     browser_resolver=browser, **kw)


class TestDoctorAvailable:
    def test_all_good_is_render_ready(self, tmp_path):
        r = _doctor(_make_composer(tmp_path))
        assert r["available"] is True and r["render_ready"] is True
        assert r["installed"] is True
        assert r["version"] == "4.0.484"
        assert r["reason"] == ""
        assert r["browser_source"] == "system"

    def test_node24_still_ok(self, tmp_path):
        r = _doctor(_make_composer(tmp_path), runner=_runner_node("v24.16.0"))
        assert r["available"] is True

    def test_available_without_global_npx(self, tmp_path):
        # Render uses the pinned local CLI, so missing global npx must NOT make
        # Remotion unavailable (npx/npm matter only to the installer).
        r = _doctor(_make_composer(tmp_path), which=lambda n: None)
        assert r["available"] is True
        assert r["checks"]["npx"] is False  # reported, non-gating

    def test_cli_bin_path_is_pinned_local(self, tmp_path):
        d = _make_composer(tmp_path)
        assert rr.cli_bin_path(d) == d / "node_modules" / ".bin" / "remotion"


class TestBrowserReadiness:
    def test_installed_but_no_browser_is_not_available(self, tmp_path):
        r = _doctor(_make_composer(tmp_path), browser=_NO_BROWSER)
        assert r["installed"] is True          # packages fine
        assert r["available"] is False         # but not render-ready
        assert r["render_ready"] is False
        assert "browser" in r["reason"].lower()
        assert r["checks"]["browser"] is False

    def test_no_browser_reason_is_actionable(self, tmp_path):
        r = _doctor(_make_composer(tmp_path), browser=_NO_BROWSER)
        assert "repair" in r["reason"].lower() or "chrome" in r["reason"].lower()


class TestDoctorFailsHonestly:
    def test_missing_node_modules(self, tmp_path):
        r = _doctor(_make_composer(tmp_path, node_modules=False))
        assert r["available"] is False and r["installed"] is False
        assert "install" in r["reason"].lower()
        assert r["checks"]["node_modules"] is False

    def test_missing_remotion_package(self, tmp_path):
        r = _doctor(_make_composer(tmp_path, remotion_pkg=False))
        assert r["available"] is False
        assert "remotion" in r["reason"].lower()

    def test_version_mismatch_fails(self, tmp_path):
        r = _doctor(_make_composer(tmp_path, installed="3.3.0"))
        assert r["available"] is False
        assert "version" in r["reason"].lower()

    def test_node_too_old_fails(self, tmp_path):
        r = _doctor(_make_composer(tmp_path), runner=_runner_node("v16.0.0"))
        assert r["available"] is False
        assert "node" in r["reason"].lower()

    def test_missing_local_cli_bin_fails(self, tmp_path):
        r = _doctor(_make_composer(tmp_path, cli_bin=False))
        assert r["available"] is False
        assert r["checks"]["cli_bin"] is False

    def test_missing_entry_point_fails(self, tmp_path):
        r = _doctor(_make_composer(tmp_path, entry=False))
        assert r["available"] is False


class TestSafeBrowserDiscovery:
    def test_env_override_under_allowed_root_accepted(self, tmp_path):
        home = tmp_path / "home"
        cache = home / ".cache" / "remotion" / "chrome"
        cache.parent.mkdir(parents=True)
        cache.write_text("#!/bin/sh\n"); os.chmod(cache, 0o755)
        path, src = rr.resolve_browser(env={"REMOTION_BROWSER_EXECUTABLE": str(cache)},
                                       which=lambda n: None, home=home, system_paths=())
        assert path == str(cache) and src == "env"

    def test_env_override_outside_allowed_root_rejected(self, tmp_path):
        home = tmp_path / "home"; home.mkdir()
        evil = tmp_path / "evil" / "chrome"; evil.parent.mkdir()
        evil.write_text("x"); os.chmod(evil, 0o755)
        path, src = rr.resolve_browser(env={"REMOTION_BROWSER_EXECUTABLE": str(evil)},
                                       which=lambda n: None, home=home, system_paths=())
        assert path is None  # arbitrary path is refused

    def test_discovers_puppeteer_cache(self, tmp_path):
        home = tmp_path / "home"
        exe = home / ".cache" / "puppeteer" / "chrome-headless-shell" / "mac" / "chrome-headless-shell"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n"); os.chmod(exe, 0o755)
        path, src = rr.resolve_browser(env={}, which=lambda n: None, home=home, system_paths=())
        assert path == str(exe) and src == "cache"

    def test_system_chrome_path_used(self, tmp_path):
        fake = tmp_path / "Chrome"; fake.write_text("x")
        path, src = rr.resolve_browser(env={}, which=lambda n: None,
                                       home=tmp_path / "nohome", system_paths=(str(fake),))
        assert path == str(fake) and src == "system"

    def test_nothing_found_returns_none(self, tmp_path):
        path, src = rr.resolve_browser(env={}, which=lambda n: None,
                                       home=tmp_path / "nohome", system_paths=())
        assert path is None and src is None


class TestRenderArgv:
    def test_uses_pinned_bin_never_npx(self, tmp_path):
        d = _make_composer(tmp_path)
        argv = rr.render_argv("src/index.tsx", "EndTag", "/tmp/o.mp4",
                              composer=d, browser="/ok/chrome")
        assert "npx" not in argv
        assert argv[0].endswith("node_modules/.bin/remotion")
        assert argv[1] == "render"
        assert "--browser-executable=/ok/chrome" in argv

    def test_no_browser_flag_when_unresolved(self, tmp_path):
        d = _make_composer(tmp_path)
        argv = rr.render_argv("src/index.tsx", "EndTag", "/tmp/o.mp4",
                              composer=d, browser="")  # explicitly none
        assert not any(a.startswith("--browser-executable") for a in argv)
        assert "npx" not in argv


class TestNoNpxInRenderPaths:
    """Runtime rendering must invoke the pinned local CLI, never `npx remotion`."""

    def test_render_commands_never_build_npx_argv(self):
        import re
        root = Path(rr.__file__).resolve().parent.parent
        for rel in ("tools/video/video_compose.py", "tools/video/remotion_caption_burn.py"):
            src = (root / rel).read_text()
            assert not re.search(r'"npx"\s*,\s*"remotion"', src), f"{rel} builds an npx remotion argv"
            assert not re.search(r'npx_bin\s*,\s*"remotion"', src), f"{rel} builds an npx_bin remotion argv"

    def test_render_paths_reference_pinned_cli(self):
        root = Path(rr.__file__).resolve().parent.parent
        for rel in ("tools/video/video_compose.py", "tools/video/remotion_caption_burn.py"):
            src = (root / rel).read_text()
            assert "cli_bin_path()" in src, f"{rel} should use the pinned cli_bin_path()"


class TestSanitized:
    def test_no_absolute_paths_leak(self, tmp_path):
        r = _doctor(_make_composer(tmp_path, node_modules=False))
        assert str(tmp_path) not in r["reason"]
        assert str(tmp_path) not in json.dumps(r["checks"])

    def test_checks_are_all_booleans(self, tmp_path):
        r = _doctor(_make_composer(tmp_path))
        assert all(isinstance(v, bool) for v in r["checks"].values())
