// @vitest-environment jsdom
//
// Manual-first Studio contract. The Studio IS the editor — there is NO Hermes /
// agent / production-run automation surface. This mounts the REAL <StudioApp>
// against a stubbed HTTP layer for an EMPTY project (no timeline, pending
// duration) and asserts:
//   * no AgentPanel / Connect / Hermes / agent / Mochlet strings anywhere;
//   * exactly ONE "Add first scene" primary on the empty timeline, and clicking
//     it runs the REAL add-layer flow (a first scene actually appears);
//   * the grouped domain regions are present (timeline / preview / renderer /
//     inspector / style);
//   * a pending duration is NEVER shown as 1800 frames or 1:00.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import React from "react";
import { createRoot } from "react-dom/client";
import { StudioApp } from "./StudioApp";
import { BacklotClient, FetchLike } from "../composition/client";

// The composer's internal safe minimum (1800f / 60s) must NEVER surface as the
// user's duration for a pending project. Include it in the timeline payload on
// purpose so the test proves it is suppressed.
function emptyTimelinePayload() {
  return {
    timeline: { version: "1.0", fps: 30, target_duration_seconds: 60, total_frames: 1800, width: 1920, height: 1080, layers: [] },
    etag: "e1", persisted: true, fps: 30, total_frames: 1800,
    target_duration_seconds: 60, target_formatted: "1:00", word_budget: 100,
    measured_output_seconds: null, remotion_render_ready: false,
    remotion_reason: "no layers", layer_types: ["video", "image", "text"],
  };
}

// A pending overview: no timeline, duration not chosen yet.
function pendingOverview() {
  return {
    version: "2.0", kind: "project_overview", project_id: "eb", title: "The Electricity Bulb",
    owner: "you", mode: "local",
    headline: "Set up your first scene",
    guidance: "This project has no timeline yet. Add your first scene to start editing.",
    has_timeline: false, layer_count: 0,
    milestones: [], milestone_progress: { completed: 0, total: 0 },
    last_saved: null, blockers: [],
    outputs: { renders: [], render_count: 0, latest_render: null, asset_count: 0 },
    target: { available: false, duration_seconds: null, formatted: null, frames: null, fps: 30, source: "pending", is_target: true, label: "Duration set after first scene" },
    render: { renderable: false, active: false, reason: "Add scenes to the timeline in the Studio to enable rendering.", layer_count: 0 },
    primary_action: { id: "open_studio", label: "Open Production Studio" },
    diagnostics: [], stale: false, is_demo: false, is_fixture: false,
  };
}

function makeFetch(): FetchLike {
  const routes: Array<[RegExp, () => unknown]> = [
    [/\/api\/csrf$/, () => ({ csrf: "TOK" })],
    [/\/timeline$/, () => emptyTimelinePayload()],
    [/\/status(\?|$)/, () => pendingOverview()],
    [/\/preferences/, () => ({ categories: [] })],
  ];
  return (async (input: string) => {
    const url = String(input);
    const match = routes.find(([re]) => re.test(url));
    const body = match ? match[1]() : {};
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  }) as unknown as FetchLike;
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

function mount(): HTMLElement {
  const client = new BacklotClient({ projectId: "eb", fetchImpl: makeFetch() });
  const container = document.createElement("div");
  document.body.appendChild(container);
  createRoot(container).render(React.createElement(StudioApp, { client }));
  return container;
}

// No agent / Hermes / Mochlet / automation surface may ever leak into the editor.
const FORBIDDEN = [
  "AgentPanel", "Connect Hermes", "Hermes Agent", "Hermes", "Mochlet", "mochlet",
  "9235", "Production Agent", "Command center", "Start production", "connectAgent",
  "Queue regeneration", "Selective regeneration", "regeneration", "regenerate",
];

describe("manual-first Studio — empty project", () => {
  beforeEach(() => {
    (globalThis as unknown as { EventSource: unknown }).EventSource = class { close() {} } as unknown;
  });
  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("shows exactly one 'Add first scene' primary and no agent/automation surface", async () => {
    const container = mount();
    const text = await settle(container, "Add first scene");
    // exactly one prominent empty-state CTA
    expect(container.querySelectorAll('[data-testid="add-first-scene"]').length).toBe(1);
    // it is a real button (not a scroll target / dead link)
    const cta = container.querySelector('[data-testid="add-first-scene"]') as HTMLButtonElement;
    expect(cta.tagName).toBe("BUTTON");
    // the empty-timeline card is shown (no misleading blank preview)
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeTruthy();
    // no agent / automation strings anywhere in the served document
    const html = container.innerHTML;
    for (const bad of FORBIDDEN) {
      expect(text).not.toContain(bad);
      expect(html).not.toContain(bad);
    }
    // no credential inputs
    expect(container.querySelectorAll("input[type=password]").length).toBe(0);
  });

  it("never presents the composer's internal minimum (1800 / 1:00) for a pending duration", async () => {
    const container = mount();
    const text = await settle(container, "Add first scene");
    expect(text).toContain("Duration set after first scene");
    expect(text).not.toMatch(/\b1800\b/);
    expect(text).not.toMatch(/\b1:00\b/);
    // the scrubber readout must not show a fabricated f0/1800 count
    expect(text).not.toContain("f0/1800");
  });

  it("groups the controls under labelled domain regions", async () => {
    const container = mount();
    await settle(container, "Add first scene");
    expect(container.querySelector('[data-testid="domain-timeline"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-preview"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-renderer"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="domain-inspector"]')).toBeTruthy();
    // the Style region appears once its tab is selected
    const styleTab = [...container.querySelectorAll("button")].find(
      (b) => (b.textContent || "").trim() === "Style") as HTMLButtonElement | undefined;
    expect(styleTab).toBeTruthy();
    styleTab!.click();
    await settle(container, "LEARNED PREFERENCES");
    expect(container.querySelector('[data-testid="domain-style"]')).toBeTruthy();
  });

  it("Render is disabled with a reason while the timeline is empty", async () => {
    const container = mount();
    await settle(container, "Add first scene");
    const renderBtn = [...container.querySelectorAll("button")].find(
      (b) => /render final/i.test(b.textContent || "")) as HTMLButtonElement | undefined;
    expect(renderBtn).toBeTruthy();
    expect(renderBtn!.disabled).toBe(true);
    // never mid-render label when nothing is rendering
    expect(renderBtn!.textContent || "").not.toMatch(/Rendering…/);
  });

  it("clicking 'Add first scene' runs the REAL add-layer flow and creates a first scene", async () => {
    const container = mount();
    await settle(container, "Add first scene");
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeTruthy();
    const cta = container.querySelector('[data-testid="add-first-scene"]') as HTMLButtonElement;
    cta.click();
    // after the real addLayer() commit the empty state is gone and the ongoing
    // "+ Add layer" control appears — the timeline now has a layer/scene.
    await settle(container, "+ Add layer");
    expect(container.querySelector('[data-testid="empty-timeline"]')).toBeNull();
    expect(container.querySelector('[data-testid="tl-add-layer"]')).toBeTruthy();
    // and no second "Add first scene" primary lingers
    expect(container.querySelectorAll('[data-testid="add-first-scene"]').length).toBe(0);
  });

  it("refreshes the summary after adding a scene — never claims 'no timeline'", async () => {
    const container = mount();
    await settle(container, "Add first scene");
    (container.querySelector('[data-testid="add-first-scene"]') as HTMLButtonElement).click();
    await settle(container, "+ Add layer");
    const text = container.textContent || "";
    // The stale server guidance must be replaced by the live-model summary.
    expect(text).not.toMatch(/no timeline yet/i);
    expect(text).not.toMatch(/add your first scene/i);
    expect(container.querySelector('[data-testid="workspace-guidance"]')!.textContent || "")
      .toMatch(/1 scene on the timeline/i);
  });

  it("seeks the preview to a visible frame after add (creation never looks blank)", async () => {
    const container = mount();
    await settle(container, "Add first scene");
    (container.querySelector('[data-testid="add-first-scene"]') as HTMLButtonElement).click();
    await settle(container, "+ Add layer");
    // The playhead advanced off frame 0 (where the title entrance opacity is 0) to
    // a representative visible frame inside the new layer.
    await new Promise((r) => setTimeout(r, 80)); // let the deferred rAF seek run
    const readout = container.querySelector('[data-testid="scrub-readout"]')?.textContent || "";
    const m = readout.match(/f(\d+)\//);
    expect(m).toBeTruthy();
    expect(Number(m![1])).toBeGreaterThan(0);
  });

  it("the first scene is real, visible content and editable in the Inspector", async () => {
    const container = mount();
    await settle(container, "Add first scene");
    (container.querySelector('[data-testid="add-first-scene"]') as HTMLButtonElement).click();
    await settle(container, "+ Add layer");
    // The new text layer carries a visible default ("New scene") — in the timeline
    // block label and the editable Inspector Content field.
    const content = container.querySelector('[data-testid="inspector-content"]') as HTMLTextAreaElement;
    expect(content).toBeTruthy();
    expect(content.value).toContain("New scene");
    // Editing the content is wired to the model (no crash / stays selected).
    content.value = "How Lightning Forms";
    content.dispatchEvent(new Event("input", { bubbles: true }));
    await settle(container, "How Lightning Forms");
    const content2 = container.querySelector('[data-testid="inspector-content"]') as HTMLTextAreaElement;
    expect(content2).toBeTruthy();
  });
});
