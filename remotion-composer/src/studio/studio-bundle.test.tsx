// @vitest-environment jsdom
//
// REAL integration: load the SHIPPED /ui/studio.bundle.js into jsdom and mount it
// via window.BacklotStudio.mount with a stubbed fetch returning the canonical
// /status + timeline for the exact rejected fixture (research/proposal approved,
// plan approved, no timeline, requested 150s, Hermes disconnected). Asserts the
// FULL rendered document has NO legacy leakage, exactly one primary, sanitized
// short job id when live, and the truthful 2:30 target — the "tests passed / real
// browser failed" gap the component-markup test could not cover.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const BUNDLE = path.resolve(__dirname, "../../../backlot/ui/studio.bundle.js");
const PID = "the-electricity-bulb";

function emptyTimelinePayload() {
  return {
    timeline: { version: "1.0", fps: 30, target_duration_seconds: 60, total_frames: 1800, width: 1920, height: 1080, layers: [] },
    etag: "e1", persisted: true, fps: 30, total_frames: 1800,
    target_duration_seconds: 60, target_formatted: "1:00", word_budget: 100,
    measured_output_seconds: null, remotion_render_ready: false,
    remotion_reason: "not ready", layer_types: ["video", "image", "text"],
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
    why_waiting: "Local Hermes (Mochlet) detected — connect to start production",
    primary_action: { id: "connect_hermes", label: "Connect Hermes to continue", owner: "user", kind: "connect", advances_production: true },
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
    connection: { status: "detected", available: false, headline: "Local Hermes (Mochlet) detected — connect to start production", detail: "Mochlet is running on this Mac." },
    diagnostics: [], sources: { brain_state: "not_started", brain_run_id: null, run_state: "waiting_for_approval", plan_approved: true, has_checkpoints: true },
    stale: false, is_demo: false, is_live: false, is_fixture: false,
    ...over,
  };
}

let statusOverride: Record<string, unknown> = {};

function installFetch() {
  const routes: Array<[RegExp, () => unknown]> = [
    [/\/api\/csrf$/, () => ({ csrf: "TOK" })],
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

const FORBIDDEN = ["fake_driver", "NO LIVE RUN", "NOT STARTED", "brain: —", "DETERMINISTIC FIXTURE", "OFFLINE DRIVER"];

describe("shipped studio.bundle.js — real mount, canonical UI, no legacy leakage", () => {
  beforeEach(() => {
    statusOverride = {};
    installFetch();
    (globalThis as unknown as { EventSource: unknown }).EventSource = class { close() {} } as unknown;
    if (!fs.existsSync(BUNDLE)) throw new Error("build the studio bundle first (npm run build:studio)");
  });
  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("renders the canonical Studio (command center + inspector) with no legacy state", async () => {
    const container = mountBundle();
    const text = await settle(container, "Asset generation");
    // canonical stage/headline/owner present
    expect(text).toContain("Asset generation");
    expect(text).toContain("Waiting for Hermes to begin asset generation");
    expect(text).toContain("Owner: You");
    // truthful target duration, never the composer default 1:00/1800 — ANYWHERE
    // in the full document (header, empty card, AND the transport scrubber).
    expect(text).toContain("target 2:30 · 4500 target frames");
    expect(text).toContain("f0/4500");                 // scrubber agrees with the target
    expect(text).not.toMatch(/\b1800\b/);              // no internal composer frame count
    expect(text).not.toMatch(/\b1:00\b/);
    // NO legacy leakage anywhere in the served document
    for (const bad of FORBIDDEN) expect(text).not.toContain(bad);
    // exactly ONE production-next action across the whole page (the cc-primary);
    // no enabled Start, no duplicate Connect, no "go to next step" button.
    expect(container.querySelectorAll('[data-testid="cc-primary"]').length).toBe(1);
    const buttons = [...container.querySelectorAll("button")] as HTMLButtonElement[];
    const enabledText = buttons.filter((b) => !b.disabled).map((b) => (b.textContent || "").trim());
    expect(enabledText.filter((t) => /start production/i.test(t)).length).toBe(0);
    expect(enabledText.filter((t) => /go to the next step/i.test(t)).length).toBe(0);
    expect(enabledText.filter((t) => /connect hermes/i.test(t)).length).toBe(1); // exactly one Connect
    // the inspector references the next step as static text, not a button
    expect(container.querySelector('[data-testid="pi-next-ref"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="pi-goto-action"]')).toBeNull();
    // empty-timeline card present (no misleading blank preview)
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeTruthy();
    // connection banner is status text only (no button inside)
    const conn = container.querySelector('[data-testid="cc-conn"]');
    expect(conn).toBeTruthy();
    expect(conn?.querySelector("button")).toBeNull();
  });

  it("scrubber advances on an empty timeline (no Player) using the target duration", async () => {
    const container = mountBundle();
    await settle(container, "f0/4500");
    // step forward — with no Player, `seek` must still move the playhead/timecode.
    const nextBtn = [...container.querySelectorAll("button")].find(
      (b) => (b.getAttribute("aria-label") || "") === "Next frame") as HTMLButtonElement | undefined;
    expect(nextBtn).toBeTruthy();
    nextBtn!.click();
    const after = await settle(container, "f1/4500");
    expect(after).toContain("f1/4500");     // playhead advanced (denominator = target 4500)
    expect(after).not.toMatch(/\b1800\b/);
  });

  it("live run shows a SANITIZED short job id (never the raw handle)", async () => {
    const FULL_JOB = "11111111-1111-4111-8111-111111111111";
    statusOverride = {
      is_live: true, mode: "live", overall_state: "producing", owner: "hermes",
      headline: "Hermes is working on Asset generation",
      primary_action: { id: "monitor", label: "Hermes is producing", owner: "hermes", kind: "status", advances_production: false },
      identity: { agent: "a", job: FULL_JOB, session: "22222222-2222-4222-8222-222222222222", engine: "mochlet", tool: "image_selector", provider: "flux" },
      connection: { status: "connected", available: true, headline: "Connected to Hermes" },
    };
    const container = mountBundle();
    const text = await settle(container, "image_selector");
    expect(text).toContain("job 11111111");        // short id chip
    expect(text).not.toContain(FULL_JOB);           // never the raw handle
    expect(text).toContain("image_selector");
  });
});
