"""Architecture guard: OpenMontage's runtime + UI must have ZERO Mochlet coupling.

Mochlet is (at most) Hermes's private, internal orchestrator. OpenMontage is
operated natively by the **Hermes Agent** through its ACP stdio surface. No
OpenMontage runtime module or user-facing surface may reference a Mochlet
endpoint, token, project, or job, nor call the Mochlet MCP tools
(``sendChat``/``runJob``/…), nor expose an endpoint/token/project connect UX.

This test fails loudly if any regression re-introduces that coupling.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runtime + user-facing surfaces only (NOT tests/docs/vendored Layer-3 skills).
SCAN_DIRS = [
    REPO_ROOT / "lib",
    REPO_ROOT / "backlot",
    REPO_ROOT / "tools",
    REPO_ROOT / "remotion-composer" / "src",
]

SCAN_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css"}

# Substrings/patterns that must never appear in runtime or UI source.
FORBIDDEN = [
    (re.compile(r"mochlet", re.IGNORECASE), "Mochlet reference"),
    (re.compile(r"\b9235\b"), "the Mochlet MCP port 9235"),
    (re.compile(r"sendChat"), "the Mochlet 'sendChat' MCP tool"),
    (re.compile(r"runJob"), "the Mochlet 'runJob' MCP tool"),
    (re.compile(r"listJobPage"), "the Mochlet 'listJobPage' MCP tool"),
    (re.compile(r"continueSession"), "the Mochlet 'continueSession' MCP tool"),
    (re.compile(r"listProjects"), "the Mochlet 'listProjects' MCP tool"),
    (re.compile(r"/api/hermes/"), "a removed /api/hermes/* route"),
    (re.compile(r"hermes_connection\.json"), "the removed Mochlet connection config"),
    (re.compile(r"hermes_jobs\.json"), "the removed Mochlet job store"),
    (re.compile(r"mcp_client"), "the removed Mochlet MCP client module"),
    (re.compile(r"MochletProject"), "a Mochlet project type"),
]


def _iter_source_files():
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            # Test/spec files legitimately reference forbidden strings to ASSERT
            # their absence — scan only shipping runtime + UI source, not tests.
            name = path.name.lower()
            if ".test." in name or ".spec." in name or "__tests__" in path.parts:
                continue
            yield path


def test_no_mochlet_coupling_in_runtime_or_ui():
    violations: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in FORBIDDEN:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{line} contains {label} ('{m.group(0)}')")
    assert not violations, (
        "Mochlet coupling re-introduced into runtime/UI source:\n  "
        + "\n  ".join(violations))


def test_no_mochlet_modules_exist():
    for gone in ("lib/production_brain/mochlet.py",
                 "lib/production_brain/mcp_client.py",
                 "lib/production_brain/connection.py"):
        assert not (REPO_ROOT / gone).exists(), f"{gone} must be deleted"


def test_native_agent_module_is_the_live_client():
    """The live orchestration client must be the native Hermes Agent, fail-closed."""
    from lib.production_brain import hermes_agent

    client = hermes_agent._UnavailableAgentClient()
    assert client.available() is False
    # The engine label for a real connection is the native agent, never 'mochlet'.
    assert hermes_agent.AGENT_ENGINE == "hermes-agent"


def test_board_is_overview_with_single_studio_action():
    """The board must offer exactly one dominant 'Open Production Studio' action and
    no production-control buttons."""
    board = (REPO_ROOT / "backlot" / "ui" / "board.js").read_text(encoding="utf-8")
    assert "Open Production Studio" in board, "Board must offer 'Open Production Studio'"
    # No guided-connect modal and no Mochlet connect UX on the board.
    assert "openConnectModal" not in board
    for banned in ("/api/hermes/", "9235", "mochlet", "Mochlet"):
        assert banned not in board, f"board.js must not contain {banned!r}"


def test_studio_has_native_agent_endpoints_not_mochlet():
    client_ts = (REPO_ROOT / "remotion-composer" / "src" / "composition" / "client.ts").read_text(encoding="utf-8")
    assert "/api/agent/" in client_ts, "Studio client must use the native /api/agent/* routes"
    assert "/api/hermes/" not in client_ts
    assert "connectHermes" not in client_ts
