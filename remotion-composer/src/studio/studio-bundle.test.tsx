// @vitest-environment jsdom
//
// REAL integration: load the SHIPPED /ui/studio.bundle.js into jsdom and mount it
// via window.BacklotStudio.mount with a stubbed fetch returning the canonical
// timeline + read-only /status overview for an EMPTY project (no timeline, a
// truthful requested target of 2:30). Asserts the manual-first Studio contract:
// NO agent / Hermes / Mochlet / automation leakage anywhere, a single prominent
// "Add first scene" primary that runs the real add-layer flow, the grouped domain
// regions, a truthful target duration (never the composer's 1800/1:00 default),
// and a scrubber that advances against the target.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const BUNDLE = path.resolve(__dirname, "../../../backlot/ui/studio.bundle.js");
const PID = "the-electricity-bulb";

let fetchCalls: string[] = [];

// The composer's internal safe minimum (1800f / 60s) is present in the timeline
// payload ON PURPOSE — the Studio must never surface it as the user's duration.
function emptyTimelinePayload() {
  return {
    timeline: { version: "1.0", fps: 30, target_duration_seconds: 60, total_frames: 1800, width: 1920, height: 1080, layers: [] },
    etag: "e1", persisted: true, fps: 30, total_frames: 1800,
    target_duration_seconds: 60, target_formatted: "1:00", word_budget: 100,
    measured_output_seconds: null, remotion_render_ready: false,
    remotion_reason: "no layers", layer_types: ["video", "image", "text"],
  };
}

function overviewPayload(over: Record<string, unknown> = {}) {
  return {
    version: "2.0", kind: "project_overview", project_id: PID, title: "The Electricity Bulb",
    owner: "you", mode: "local",
    headline: "Set up your first scene",
    guidance: "This project has no timeline yet. Add your first scene to start editing.",
    has_timeline: false, layer_count: 0,
    milestones: [], milestone_progress: { completed: 0, total: 0 },
    last_saved: null, blockers: [],
    outputs: { renders: [], render_count: 0, latest_render: null, asset_count: 0 },
    // A truthful REQUESTED target of 2:30 (4500 frames) — never the composer default.
    target: { available: true, duration_seconds: 150, formatted: "2:30", frames: 4500, fps: 30, source: "requested", is_target: true, label: "target 2:30 · 4500 target frames" },
    render: { renderable: false, active: false, reason: "Add scenes to the timeline in the Studio to enable rendering.", layer_count: 0 },
    primary_action: { id: "open_studio", label: "Open Production Studio" },
    diagnostics: [], stale: false, is_demo: false, is_fixture: false,
    ...over,
  };
}

function installFetch() {
  const routes: Array<[RegExp, () => unknown]> = [
    [/\/api\/csrf$/, () => ({ csrf: "TOK" })],
    [/\/timeline$/, () => emptyTimelinePayload()],
    [/\/status(\?|$)/, () => overviewPayload()],
    [/\/preferences/, () => ({ categories: [] })],
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

async function settle(container: HTMLElement, needle: string, tries = 80): Promise<string> {
  for (let i = 0; i < tries; i++) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((r) => setTimeout(r, 25));
    const txt = container.textContent || "";
    if (txt.includes(needle)) return txt;
  }
  return container.textContent || "";
}

// No agent / Hermes / Mochlet / automation surface may leak into the served document.
const FORBIDDEN = [
  "AgentPanel", "Connect Hermes", "Hermes Agent", "Hermes", "Mochlet", "mochlet",
  "9235", "/api/agent", "/api/hermes", "Production Agent", "Command center", "Start production",
];

describe("shipped studio.bundle.js — manual-first editor, no automation leakage", () => {
  beforeEach(() => {
    fetchCalls = [];
    installFetch();
    (globalThis as unknown as { EventSource: unknown }).EventSource = class { close() {} } as unknown;
    if (!fs.existsSync(BUNDLE)) throw new Error("build the studio bundle first (npm run build:studio)");
  });
  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("renders the empty-state editor with one 'Add first scene' primary, grouped domains, and a truthful target", async () => {
    const container = mountBundle();
    const text = await settle(container, "Add first scene");
    // empty-timeline card + a single prominent CTA
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeTruthy();
    expect(container.querySelectorAll('[data-testid="add-first-scene"]').length).toBe(1);
    // truthful requested target, never the composer default 1:00 / 1800 — ANYWHERE
    expect(text).toContain("target 2:30 · 4500 target frames");
    expect(text).toContain("f0/4500");                 // scrubber agrees with the target
    expect(text).not.toMatch(/\b1800\b/);              // no internal composer frame count
    expect(text).not.toMatch(/\b1:00\b/);
    // NO agent / Hermes / Mochlet / automation leakage in the served document
    for (const bad of FORBIDDEN) expect(text).not.toContain(bad);
    // NO credential inputs anywhere
    expect(container.querySelectorAll("input[type=password]").length).toBe(0);
    // grouped, labelled domain regions
    expect(container.querySelector('[data-testid="domain-timeline"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-preview"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-renderer"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-inspector"]')).toBeTruthy();
    // never a start/connect production control
    const enabled = [...container.querySelectorAll("button")].filter((b) => !(b as HTMLButtonElement).disabled).map((b) => (b.textContent || "").trim());
    expect(enabled.filter((t) => /start production|connect/i.test(t)).length).toBe(0);
  });

  it("scrubber advances on the empty timeline (no Player) using the target duration", async () => {
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

  it("clicking 'Add first scene' runs the real add-layer flow", async () => {
    const container = mountBundle();
    await settle(container, "Add first scene");
    const cta = container.querySelector('[data-testid="add-first-scene"]') as HTMLButtonElement;
    expect(cta).toBeTruthy();
    cta.click();
    await settle(container, "+ Add layer");
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeNull();
    expect(container.querySelector('[data-testid="tl-add-layer"]')).toBeTruthy();
  });
});
