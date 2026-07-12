import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ProductionInspector } from "./ProductionInspector";
import { CommandCenter } from "./CommandCenter";
import { StatusController } from "./useStatusView";
import { StatusView } from "../composition/status";
import { BacklotClient } from "../composition/client";

// The exact rejected fixture: research/proposal approved, plan approved, no timeline,
// requested 150s, Hermes disconnected (Mochlet detected).
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
    why_waiting: "Local Hermes (Mochlet) detected — connect to start production",
    primary_action: { id: "connect_hermes", label: "Connect Hermes to continue", owner: "user", kind: "connect", advances_production: true },
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
    connection: { status: "detected", available: false, headline: "Local Hermes (Mochlet) detected — connect to start production", detail: "Mochlet is running on this Mac." },
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
    view, coldError: false, busy: false, actionError: null, connectOpen: false,
    setConnectOpen: () => {}, refresh: () => {}, runAction: () => {}, focusPrimary: () => {},
  };
}

const FORBIDDEN = ["fake_driver", "NO LIVE RUN", "NOT STARTED", "brain: —", "DETERMINISTIC FIXTURE", "OFFLINE DRIVER"];

describe("Production inspector — canonical, no legacy leakage", () => {
  it("shows the canonical stage/headline/owner and NO raw brain/run/driver labels", () => {
    const html = renderToStaticMarkup(<ProductionInspector status={controller(fixtureView())} />);
    expect(html).toContain("Asset generation");
    expect(html).toContain("Stage 5 of 11");
    expect(html).toContain("Owner: You");
    expect(html).toContain("target 2:30 · 4500 target frames");
    for (const bad of FORBIDDEN) expect(html).not.toContain(bad);
    // No second Start/Connect PRIMARY button — only a plain "go to next step" link.
    expect(html).not.toMatch(/<button[^>]*>[^<]*Start production/i);
    expect(html).toContain("Go to the next step");
  });

  it("does not render a live badge or handles when there is no live run", () => {
    const html = renderToStaticMarkup(<ProductionInspector status={controller(fixtureView())} />);
    expect(html).not.toContain("● LIVE");
    expect(html).not.toContain("job ");
  });

  it("shows real sanitized handles only when live", () => {
    const live = fixtureView({
      is_live: true, mode: "live",
      identity: { agent: "a", job: "11111111-1111-4111-8111-111111111111", session: "22222222-2222-4222-8222-222222222222", engine: "mochlet", tool: "image_selector", provider: "flux" },
    });
    const html = renderToStaticMarkup(<ProductionInspector status={controller(live)} />);
    expect(html).toContain("● LIVE");
    expect(html).toContain("job 11111111");       // short id, sanitized
    expect(html).not.toContain("11111111-1111-4111-8111-111111111111".slice(0, 20) + "-"); // not the full id inline
    expect(html).toContain("image_selector");
  });
});

describe("Command center — status-only connection banner (one action invariant)", () => {
  it("renders the connection banner WITHOUT its own button", () => {
    const fakeClient = { projectId: "p" } as unknown as BacklotClient;
    const html = renderToStaticMarkup(<CommandCenter status={controller(fixtureView())} client={fakeClient} />);
    // exactly one primary action button on the card
    const primaryCount = (html.match(/data-testid="cc-primary"/g) || []).length;
    expect(primaryCount).toBe(1);
    // the connection banner is present as status text, with no button inside it
    expect(html).toContain('data-testid="cc-conn"');
    const banner = html.split('data-testid="cc-conn"')[1]?.split("</div>")[0] ?? "";
    expect(banner).not.toContain("<button");
  });
});
