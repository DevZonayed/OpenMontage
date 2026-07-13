import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ProductionInspector } from "./ProductionInspector";
import { CommandCenter } from "./CommandCenter";
import { AgentConnection, StatusView } from "../composition/status";
import { StatusController } from "./useStatusView";

// The exact rejected fixture: research/proposal approved, plan approved, no timeline,
// requested 150s — with the NATIVE Hermes Agent detected but not yet connected.
// There is NO endpoint/token/project surface anywhere.
function detectedAgent(): AgentConnection {
  return {
    kind: "hermes_agent",
    status: "detected",
    available: false,
    server_name: "Hermes Agent",
    headline: "Hermes Agent detected on this machine",
    detail: "Connect the Hermes Agent to start production.",
    actions: [{ id: "connect_agent", label: "Connect Hermes Agent" }],
    enabled: false,
    installed: true,
    ready: true,
    version: "1.4.0",
  };
}

function fixtureView(over: Partial<StatusView> = {}): StatusView {
  return {
    version: "1.0",
    kind: "production_status_view",
    project_id: "the-electricity-bulb",
    mode: "local",
    authoritative_source: "checkpoints",
    overall_state: "ready_to_produce",
    current_stage: "assets",
    current_stage_label: "Asset generation",
    stage_index: 4,
    stage_number: 5,
    stage_count: 11,
    headline: "Waiting for Hermes to begin asset generation",
    active_task: "Plan approved — production has not started yet.",
    owner: "user",
    why_waiting: "Hermes Agent detected — connect to start production",
    primary_action: { id: "connect_agent", label: "Connect Hermes Agent to continue", owner: "user", kind: "connect", advances_production: true },
    secondary_actions: [{ id: "preview", label: "Preview approved plan locally", owner: "user", kind: "preview", advances_production: false }],
    latest_event: { label: "No production run has started for this project." },
    elapsed_seconds: null,
    progress: 4 / 11,
    completed_stages: 4,
    stages: [
      { id: "research", index: 0, label: "Research", status: "completed", progress: 1 },
      { id: "assets", index: 4, label: "Asset generation", status: "current", progress: 0 },
    ],
    identity: { agent: null, job: null, session: null, engine: null, tool: null, provider: null },
    run_id: "run_eb_001",
    stop_available: false,
    render: { renderable: false, active: false, reason: "The plan is approved but no assets exist yet.", layer_count: 0 },
    target: { available: true, duration_seconds: 150, formatted: "2:30", frames: 4500, fps: 30, source: "requested", is_target: true, label: "target 2:30 · 4500 target frames" },
    connection: detectedAgent(),
    diagnostics: [],
    sources: { brain_state: "not_started", brain_run_id: null, run_state: "waiting_for_approval", plan_approved: true, has_checkpoints: true },
    stale: false,
    is_demo: false,
    is_live: false,
    is_fixture: false,
    ...over,
  };
}

function controller(view: StatusView | null): StatusController {
  return {
    view, coldError: false, busy: false, actionError: null,
    refresh: () => {}, runAction: () => {},
  };
}

// No Mochlet / endpoint / token / MCP surface may ever leak into the UI.
const FORBIDDEN = ["fake_driver", "NO LIVE RUN", "NOT STARTED", "brain: —", "DETERMINISTIC FIXTURE", "OFFLINE DRIVER", "Mochlet", "mochlet", "9235", "endpoint", "MCP"];

describe("Production inspector — canonical, no legacy leakage", () => {
  it("shows the canonical stage/headline/owner and NO raw brain/run/driver labels", () => {
    const html = renderToStaticMarkup(<ProductionInspector status={controller(fixtureView())} />);
    expect(html).toContain("Asset generation");
    expect(html).toContain("Stage 5 of 11");
    expect(html).toContain("Owner: You");
    expect(html).toContain("target 2:30 · 4500 target frames");
    for (const bad of FORBIDDEN) expect(html).not.toContain(bad);
    // The inspector is STATUS-ONLY: no buttons at all (no second Start/Connect/goto).
    expect(html).not.toMatch(/<button/i);
    expect(html).toContain("Next step:");           // static reference, not a button
  });

  it("does not render a live badge or handles when there is no live run", () => {
    const html = renderToStaticMarkup(<ProductionInspector status={controller(fixtureView())} />);
    expect(html).not.toContain("● LIVE");
    expect(html).not.toContain("job ");
  });

  it("shows real sanitized handles only when live", () => {
    const live = fixtureView({
      is_live: true, mode: "live",
      identity: { agent: "a", job: "11111111-1111-4111-8111-111111111111", session: "22222222-2222-4222-8222-222222222222", engine: "hermes", tool: "image_selector", provider: "flux" },
    });
    const html = renderToStaticMarkup(<ProductionInspector status={controller(live)} />);
    expect(html).toContain("● LIVE");
    expect(html).toContain("job 11111111");       // short id, sanitized
    expect(html).not.toContain("11111111-1111-4111-8111-111111111111".slice(0, 20) + "-"); // not the full id inline
    expect(html).toContain("image_selector");
  });
});

describe("Command center — native Hermes Agent panel + one primary action", () => {
  it("renders the native agent panel, exactly one primary action, and no Mochlet/credential surface", () => {
    const html = renderToStaticMarkup(<CommandCenter status={controller(fixtureView())} />);
    // exactly one production-primary action button on the card
    const primaryCount = (html.match(/data-testid="cc-primary"/g) || []).length;
    expect(primaryCount).toBe(1);
    // the native, auto-detected agent panel is present as its own domain section
    expect(html).toContain('data-testid="agent-panel"');
    expect(html).toContain('data-testid="domain-agent"');
    expect(html).toContain("Production Agent");
    // detected → the panel offers a single native "Connect Hermes Agent" (no fields)
    expect(html).toContain('data-testid="agent-connect"');
    expect(html).toContain("Hermes Agent detected on this machine");
    expect(html).toContain("v1.4.0");             // version surfaced when present
    // absolutely no credential / endpoint / token / project inputs
    expect(html).not.toMatch(/<input/i);
    for (const bad of FORBIDDEN) expect(html).not.toContain(bad);
  });

  it("connected agent shows the connected state + a Disconnect control", () => {
    const connected = fixtureView({
      connection: {
        kind: "hermes_agent", status: "connected", available: true, server_name: "Hermes Agent",
        headline: "Hermes Agent connected", detail: "", actions: [{ id: "disconnect_agent", label: "Disconnect" }],
        enabled: true, installed: true, ready: true, version: "1.4.0",
      },
    });
    const html = renderToStaticMarkup(<CommandCenter status={controller(connected)} />);
    expect(html).toContain("Hermes Agent connected");
    expect(html).toContain('data-testid="agent-disconnect"');
    expect(html).not.toContain('data-testid="agent-connect"');
    for (const bad of FORBIDDEN) expect(html).not.toContain(bad);
  });

  it("not-installed agent shows install guidance + a re-check control, editing stays available", () => {
    const notInstalled = fixtureView({
      connection: {
        kind: "hermes_agent", status: "not_installed", available: false, server_name: "Hermes Agent",
        headline: "Hermes Agent is not installed", detail: "Install the Hermes Agent to run production.",
        actions: [{ id: "connect_agent", label: "Re-check for Hermes" }],
        enabled: false, installed: false, ready: false, version: null,
      },
    });
    const html = renderToStaticMarkup(<CommandCenter status={controller(notInstalled)} />);
    expect(html).toContain("Hermes Agent is not installed");
    expect(html).toContain('data-testid="agent-recheck"');
    expect(html).toContain("Install the Hermes Agent");
  });
});
