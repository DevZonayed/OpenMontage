// @vitest-environment jsdom
//
// REAL integration: load the SHIPPED /ui/studio.bundle.js into jsdom and mount it
// via window.BacklotStudio.mount with a stubbed fetch returning the canonical
// /status + timeline for the exact rejected fixture (research/proposal approved,
// plan approved, no timeline, requested 150s) with the NATIVE Hermes Agent
// detected-but-not-connected. Asserts the full rendered document has NO Mochlet /
// endpoint / token / MCP leakage, exactly one primary action, the native agent
// setup panel, three visually-separated domains, sanitized short job id when live,
// and the truthful 2:30 target.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const BUNDLE = path.resolve(__dirname, "../../../backlot/ui/studio.bundle.js");
const PID = "the-electricity-bulb";

let fetchCalls: string[] = [];

function emptyTimelinePayload() {
  return {
    timeline: { version: "1.0", fps: 30, target_duration_seconds: 60, total_frames: 1800, width: 1920, height: 1080, layers: [] },
    etag: "e1", persisted: true, fps: 30, total_frames: 1800,
    target_duration_seconds: 60, target_formatted: "1:00", word_budget: 100,
    measured_output_seconds: null, remotion_render_ready: false,
    remotion_reason: "not ready", layer_types: ["video", "image", "text"],
  };
}

function agentConnection(over: Record<string, unknown> = {}) {
  return {
    kind: "hermes_agent", status: "detected", available: false, server_name: "Hermes Agent",
    headline: "Hermes Agent detected on this machine", detail: "Connect the Hermes Agent to start production.",
    actions: [{ id: "connect_agent", label: "Connect Hermes Agent" }],
    enabled: false, installed: true, ready: true, version: "1.4.0", ...over,
  };
}

function statusPayload(over: Record<string, unknown> = {}) {
  return {
    version: "1.0", kind: "production_status_view", project_id: PID, mode: "local",
    authoritative_source: "checkpoints", overall_state: "ready_to_produce",
    current_stage: "assets", current_stage_label: "Asset generation",
    stage_index: 4, stage_number: 5, stage_count: 11,
    headline: "Waiting for Hermes to begin asset generation",
    active_task: "Plan approved — production has not started yet.", owner: "user",
    why_waiting: "Hermes Agent detected — connect to start production",
    primary_action: { id: "connect_agent", label: "Connect Hermes Agent to continue", owner: "user", kind: "connect", advances_production: true },
    secondary_actions: [{ id: "preview", label: "Preview approved plan locally", owner: "user", kind: "preview", advances_production: false }],
    latest_event: { label: "No production run has started for this project." },
    elapsed_seconds: null, progress: 0.36, completed_stages: 4,
    stages: [
      { id: "research", index: 0, label: "Research", status: "completed", progress: 1 },
      { id: "proposal", index: 1, label: "Proposal", status: "completed", progress: 1 },
      { id: "script", index: 2, label: "Script", status: "completed", progress: 1 },
      { id: "scene_plan", index: 3, label: "Scene planning", status: "completed", progress: 1 },
      { id: "assets", index: 4, label: "Asset generation", status: "current", progress: 0 },
      { id: "complete", index: 10, label: "Completion", status: "upcoming", progress: 0 },
    ],
    identity: { agent: null, job: null, session: null, engine: null, tool: null, provider: null },
    run_id: "run_eb_001", stop_available: false,
    render: { renderable: false, active: false, reason: "The plan is approved but no assets exist yet.", layer_count: 0 },
    target: { available: true, duration_seconds: 150, formatted: "2:30", frames: 4500, fps: 30, source: "requested", is_target: true, label: "target 2:30 · 4500 target frames" },
    connection: agentConnection(),
    diagnostics: [], sources: { brain_state: "not_started", brain_run_id: null, run_state: "waiting_for_approval", plan_approved: true, has_checkpoints: true },
    stale: false, is_demo: false, is_live: false, is_fixture: false,
    ...over,
  };
}

let statusOverride: Record<string, unknown> = {};

function installFetch() {
  const routes: Array<[RegExp, () => unknown]> = [
    [/\/api\/csrf$/, () => ({ csrf: "TOK" })],
    [/\/api\/agent\/(connect|disconnect)$/, () => agentConnection(statusOverride.connection as Record<string, unknown> ?? {})],
    [/\/api\/agent\/connection$/, () => agentConnection()],
    [/\/timeline$/, () => emptyTimelinePayload()],
    [/\/status(\?|$)/, () => statusPayload(statusOverride)],
    [/\/brain\/events/, () => ({ events: [], cursor: 0, next_cursor: 0, latest_seq: 0, count: 0, has_more: false })],
    [/\/brain\/assets/, () => ({ outputs: [], count: 0, run_id: null, actual_duration_seconds: null })],
    [/\/brain(\?|$)/, () => ({ state: "not_started", stages: [], brain: {} })],
    [/\/preferences/, () => ({ categories: [] })],
    [/\/run(\?|$)/, () => ({ state: "waiting_for_approval" })],
    [/\/agent-inbox/, () => ({ items: [] })],
  ];
  const impl = async (input: string) => {
    const url = String(input);
    fetchCalls.push(url);
    const match = routes.find(([re]) => re.test(url));
    const body = match ? match[1]() : {};
    return {
      ok: true, status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  };
  (globalThis as unknown as { fetch: typeof fetch }).fetch = impl as unknown as typeof fetch;
}

function mountBundle(): HTMLElement {
  const code = fs.readFileSync(BUNDLE, "utf8");
  // Execute the IIFE bundle in the current jsdom global so it sets window.BacklotStudio.
  vm.runInThisContext(code, { filename: "studio.bundle.js" });
  const w = window as unknown as { BacklotStudio?: { mount: (el: HTMLElement, o: { projectId: string }) => () => void } };
  if (!w.BacklotStudio) throw new Error("studio.bundle.js did not expose window.BacklotStudio");
  const container = document.createElement("div");
  document.body.appendChild(container);
  w.BacklotStudio.mount(container, { projectId: PID });
  return container;
}

async function settle(container: HTMLElement, needle: string, tries = 60): Promise<string> {
  for (let i = 0; i < tries; i++) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((r) => setTimeout(r, 25));
    const txt = container.textContent || "";
    if (txt.includes(needle)) return txt;
  }
  return container.textContent || "";
}

// No Mochlet / endpoint / token / MCP surface may ever leak into the served document.
const FORBIDDEN = ["fake_driver", "NO LIVE RUN", "NOT STARTED", "brain: —", "DETERMINISTIC FIXTURE", "OFFLINE DRIVER", "Mochlet", "mochlet", "9235", "endpoint", "MCP"];

describe("shipped studio.bundle.js — real mount, native agent action center, no legacy leakage", () => {
  beforeEach(() => {
    statusOverride = {};
    fetchCalls = [];
    installFetch();
    (globalThis as unknown as { EventSource: unknown }).EventSource = class { close() {} } as unknown;
    if (!fs.existsSync(BUNDLE)) throw new Error("build the studio bundle first (npm run build:studio)");
  });
  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("renders the canonical Studio with the native agent panel, three domains, and no legacy state", async () => {
    const container = mountBundle();
    const text = await settle(container, "Asset generation");
    // canonical stage/headline/owner present
    expect(text).toContain("Asset generation");
    expect(text).toContain("Waiting for Hermes to begin asset generation");
    expect(text).toContain("Owner: You");
    // truthful target duration, never the composer default 1:00/1800 — ANYWHERE
    expect(text).toContain("target 2:30 · 4500 target frames");
    expect(text).toContain("f0/4500");                 // scrubber agrees with the target
    expect(text).not.toMatch(/\b1800\b/);              // no internal composer frame count
    expect(text).not.toMatch(/\b1:00\b/);
    // NO Mochlet / endpoint / token / MCP leakage anywhere in the served document
    for (const bad of FORBIDDEN) expect(text).not.toContain(bad);
    // NO credential inputs anywhere (agent is auto-detected)
    expect(container.querySelectorAll("input[type=password]").length).toBe(0);
    // exactly ONE production-primary action across the whole page (the cc-primary)
    expect(container.querySelectorAll('[data-testid="cc-primary"]').length).toBe(1);
    const buttons = [...container.querySelectorAll("button")] as HTMLButtonElement[];
    const enabledText = buttons.filter((b) => !b.disabled).map((b) => (b.textContent || "").trim());
    expect(enabledText.filter((t) => /start production/i.test(t)).length).toBe(0);
    expect(enabledText.filter((t) => /go to the next step/i.test(t)).length).toBe(0);
    // the NATIVE Hermes Agent setup panel is present with a single connect control (no fields)
    expect(container.querySelector('[data-testid="agent-panel"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="agent-connect"]')).toBeTruthy();
    expect(text).toContain("Hermes Agent detected on this machine");
    expect(text).toContain("v1.4.0");
    // three visually-separated domains: Production Agent · Timeline/Assets · Renderer
    expect(container.querySelector('[data-testid="domain-agent"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-timeline"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-renderer"]')).toBeTruthy();
    // the inspector references the next step as static text, not a button
    expect(container.querySelector('[data-testid="pi-next-ref"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="pi-goto-action"]')).toBeNull();
    // empty-timeline card present (no misleading blank preview)
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeTruthy();
  });

  it("clicking the native Connect control POSTs an empty body to /api/agent/connect", async () => {
    const container = mountBundle();
    await settle(container, "Hermes Agent detected on this machine");
    const connectBtn = container.querySelector('[data-testid="agent-connect"]') as HTMLButtonElement;
    expect(connectBtn).toBeTruthy();
    connectBtn.click();
    // wait for the POST to fire
    for (let i = 0; i < 40 && !fetchCalls.some((u) => u.endsWith("/api/agent/connect")); i++) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 25));
    }
    expect(fetchCalls.some((u) => u.endsWith("/api/agent/connect"))).toBe(true);
    // never a Mochlet MCP endpoint
    expect(fetchCalls.some((u) => /9235|\/api\/hermes/.test(u))).toBe(false);
  });

  it("scrubber advances on an empty timeline (no Player) using the target duration", async () => {
    const container = mountBundle();
    await settle(container, "f0/4500");
    const nextBtn = [...container.querySelectorAll("button")].find(
      (b) => (b.getAttribute("aria-label") || "") === "Next frame") as HTMLButtonElement | undefined;
    expect(nextBtn).toBeTruthy();
    nextBtn!.click();
    const after = await settle(container, "f1/4500");
    expect(after).toContain("f1/4500");     // playhead advanced (denominator = target 4500)
    expect(after).not.toMatch(/\b1800\b/);
  });

  it("live run shows a SANITIZED short job id and the connected agent state", async () => {
    const FULL_JOB = "11111111-1111-4111-8111-111111111111";
    statusOverride = {
      is_live: true, mode: "live", overall_state: "producing", owner: "hermes",
      headline: "Hermes is working on Asset generation",
      primary_action: { id: "monitor", label: "Hermes is producing", owner: "hermes", kind: "status", advances_production: false },
      identity: { agent: "a", job: FULL_JOB, session: "22222222-2222-4222-8222-222222222222", engine: "hermes", tool: "image_selector", provider: "flux" },
      connection: agentConnection({ status: "connected", available: true, headline: "Hermes Agent connected", actions: [{ id: "disconnect_agent", label: "Disconnect" }], enabled: true }),
    };
    const container = mountBundle();
    const text = await settle(container, "image_selector");
    expect(text).toContain("job 11111111");        // short id chip
    expect(text).not.toContain(FULL_JOB);           // never the raw handle
    expect(text).toContain("image_selector");
    expect(container.querySelector('[data-testid="agent-disconnect"]')).toBeTruthy();
    for (const bad of FORBIDDEN) expect(text).not.toContain(bad);
  });
});
