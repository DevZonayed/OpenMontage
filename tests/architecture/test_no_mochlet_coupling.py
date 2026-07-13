"""Architecture guard: OpenMontage is a manual-first editor with ZERO agent coupling.

There is no Mochlet, and no user-facing/runtime "Hermes"/agent connection concept:
no Mochlet endpoint/port/MCP tools, no ``/api/hermes`` or ``/api/agent`` routes, no
"Connect Hermes" / "Hermes Agent" / ``AgentPanel`` in runtime or UI source. The
Board is a read-only overview whose single dominant action is "Open Production
Studio"; the Studio is a manual editor. This test fails loudly on any regression.
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

# Patterns that must never appear in runtime or UI source.
FORBIDDEN = [
    (re.compile(r"mochlet", re.IGNORECASE), "a Mochlet reference"),
    (re.compile(r"\b9235\b"), "the Mochlet MCP port 9235"),
    (re.compile(r"sendChat"), "the Mochlet 'sendChat' MCP tool"),
    (re.compile(r"runJob"), "the Mochlet 'runJob' MCP tool"),
    (re.compile(r"listJobPage"), "the Mochlet 'listJobPage' MCP tool"),
    (re.compile(r"listProjects"), "the Mochlet 'listProjects' MCP tool"),
    (re.compile(r"/api/hermes/"), "a removed /api/hermes/* route"),
    (re.compile(r"/api/agent/"), "a removed /api/agent/* route"),
    (re.compile(r"Connect Hermes", re.IGNORECASE), "a 'Connect Hermes' connection UX"),
    (re.compile(r"Hermes Agent"), "a 'Hermes Agent' label"),
    (re.compile(r"AgentPanel"), "the removed AgentPanel component"),
    (re.compile(r"hermes_agent"), "the removed native Hermes adapter module"),
    (re.compile(r"hermes_connection\.json"), "the removed Mochlet connection config"),
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
            name = path.name.lower()
            if ".test." in name or ".spec." in name or "__tests__" in path.parts:
                continue
            yield path


def test_no_agent_or_mochlet_coupling_in_runtime_or_ui():
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
        "Agent/Mochlet coupling re-introduced into runtime/UI source:\n  "
        + "\n  ".join(violations))


def test_agent_and_adapter_modules_are_gone():
    for gone in ("lib/production_brain/mochlet.py",
                 "lib/production_brain/mcp_client.py",
                 "lib/production_brain/connection.py",
                 "lib/production_brain/hermes_agent.py",
                 "lib/production_brain/orchestrator.py",
                 "lib/production_brain/adapter.py",
                 "backlot/brain_api.py",
                 # Production-run automation subsystem (start/stop/preview/queue).
                 "lib/production_run.py",
                 "lib/production_worker.py",
                 "lib/preview_render.py",
                 "lib/agent_inbox.py",
                 # Legacy classic editor with agent/replan copy + its Hermes brain fixture.
                 "backlot/ui/editor.js",
                 "remotion-composer/src/composition/brain.ts",
                 "remotion-composer/src/studio/AgentPanel.tsx",
                 "remotion-composer/src/studio/CommandCenter.tsx",
                 "remotion-composer/src/studio/ProductionInspector.tsx",
                 "remotion-composer/src/studio/useStatusView.tsx"):
        assert not (REPO_ROOT / gone).exists(), f"{gone} must be deleted"


def test_no_production_run_lifecycle_routes():
    """server.py must expose no start/stop/preview production-run automation routes
    and no agent-inbox route — OpenMontage is manual-first."""
    server = (REPO_ROOT / "backlot" / "server.py").read_text(encoding="utf-8")
    for banned in ('/run/preview', '/run/cancel', '/run/approve', '/agent-inbox',
                   'agent_inbox', 'production_worker', 'preview_render',
                   'import start_run', 'import cancel_run'):
        assert banned not in server, f"server.py must not reference {banned!r}"
    # No `POST /api/project/{id}/run` (the run-start route).
    assert 'project_id}/run"' not in server, "server.py must not expose a run-start route"


def test_no_classic_editor_agent_surface():
    """The classic editor (agent/replan copy) must be unreachable."""
    editor_html = (REPO_ROOT / "backlot" / "ui" / "editor.html").read_text(encoding="utf-8")
    for banned in ("classic=1", "editor.js", "Classic editor"):
        assert banned not in editor_html, f"editor.html must not reference {banned!r}"


def test_status_view_has_no_connection_or_worker_inference():
    """The overview view model must not carry a connection block or infer an
    autonomous worker (no 'producing'/'start'/agent owner)."""
    from lib.production_status import build_status_view

    v = build_status_view(project={"id": "p", "title": "P"})
    assert "connection" not in v
    assert v["owner"] == "you"
    assert v["primary_action"]["id"] == "open_studio"
    # No automation/worker state field.
    assert "overall_state" not in v


def test_board_is_overview_with_single_studio_action():
    board = (REPO_ROOT / "backlot" / "ui" / "board.js").read_text(encoding="utf-8")
    assert "Open Production Studio" in board, "Board must offer 'Open Production Studio'"
    for banned in ("openConnectModal", "/api/hermes/", "/api/agent/", "9235",
                   "mochlet", "Mochlet", "Hermes Agent"):
        assert banned not in board, f"board.js must not contain {banned!r}"


def test_studio_client_has_no_agent_or_run_automation():
    client_ts = (REPO_ROOT / "remotion-composer" / "src" / "composition" / "client.ts").read_text(encoding="utf-8")
    for banned in ("/api/hermes/", "/api/agent/", "connectHermes", "connectAgent",
                   "Hermes Agent"):
        assert banned not in client_ts, f"client.ts must not contain {banned!r}"
