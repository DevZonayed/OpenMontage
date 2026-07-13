import { describe, it, expect } from "vitest";
import { BacklotClient, FetchLike } from "./client";
import { deterministicOverview, ProjectOverview } from "./status";

function jsonRes(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) };
}

describe("project overview (read-only)", () => {
  it("demo overview is clearly labelled, never live, and carries NO agent/automation surface", () => {
    const v = deterministicOverview("demo");
    expect(v.kind).toBe("project_overview");
    expect(v.is_demo).toBe(true);
    expect(v.owner).toBe("you");
    // a pending duration is truthful — never the composer's internal minimum
    expect(v.target.available).toBe(false);
    expect(v.target.label).toBe("Duration set after first scene");
    // the only action is opening the (manual) studio — no run/agent controls
    expect(v.primary_action.id).toBe("open_studio");
    expect(v).not.toHaveProperty("connection");
    expect(v).not.toHaveProperty("secondary_actions");
    expect(v).not.toHaveProperty("run_id");
  });

  it("getStatus reads the canonical /status overview endpoint", async () => {
    const calls: string[] = [];
    const fetchImpl: FetchLike = async (url) => {
      calls.push(url);
      return jsonRes({
        version: "2.0",
        kind: "project_overview",
        project_id: "eb",
        title: "The Electricity Bulb",
        owner: "you",
        mode: "local",
        headline: "3 scenes on the timeline",
        guidance: "Open the Studio to keep editing, preview, and render.",
        has_timeline: true,
        layer_count: 3,
        milestones: [],
        milestone_progress: { completed: 0, total: 0 },
        last_saved: null,
        blockers: [],
        outputs: { renders: [], render_count: 0, latest_render: null, asset_count: 0 },
        target: { available: true, formatted: "2:30", frames: 4500, fps: 30, source: "timeline", is_target: false, label: "2:30 · 4500 frames" },
        render: { renderable: true, active: false, reason: null, layer_count: 3 },
        primary_action: { id: "open_studio", label: "Open Production Studio" },
        diagnostics: [],
        stale: false,
        is_demo: false,
        is_fixture: false,
      } satisfies ProjectOverview);
    };
    const c = new BacklotClient({ projectId: "eb", fetchImpl });
    const v = await c.getStatus();
    expect(calls[0]).toBe("/api/project/eb/status");
    expect(v.kind).toBe("project_overview");
    expect(v.render.renderable).toBe(true);
    expect(v.target.formatted).toBe("2:30");
    expect(c.usedFixture).toBe(false);
  });

  it("getStatus returns the demo overview ONLY in explicit demo mode", async () => {
    const c = new BacklotClient({ projectId: "eb", forceFixtures: true });
    const v = await c.getStatus();
    expect(c.usedFixture).toBe(true);
    expect(v.is_demo).toBe(true);
    expect(v.target.available).toBe(false);
  });
});
