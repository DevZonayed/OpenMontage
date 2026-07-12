import { describe, it, expect } from "vitest";
import {
  ACTIVE_RUN_STATES,
  BrainState,
  deterministicBrainState,
  isLive,
  orchestrationKind,
  prefId,
  TERMINAL_RUN_STATES,
} from "./brain";

function withOrchestration(o: "external_job" | "fake_driver"): BrainState {
  const s = deterministicBrainState("p1");
  s.brain.orchestration = o;
  s.state = "running";
  return s;
}

describe("live vs deterministic-fixture truthfulness", () => {
  it("LIVE only when a real external_job drives the run AND not on a client fixture", () => {
    // real external job, not a fixture → LIVE
    expect(isLive(withOrchestration("external_job"), false)).toBe(true);
    // external job but we're on a client-side fixture → NOT live
    expect(isLive(withOrchestration("external_job"), true)).toBe(false);
    // fake_driver is never live even if backend-served
    expect(isLive(withOrchestration("fake_driver"), false)).toBe(false);
    expect(isLive(null, false)).toBe(false);
  });

  it("reports the orchestration kind exactly", () => {
    expect(orchestrationKind(withOrchestration("external_job"))).toBe("external_job");
    expect(orchestrationKind(withOrchestration("fake_driver"))).toBe("fake_driver");
    expect(orchestrationKind(null)).toBe("unknown");
  });
});

describe("deterministicBrainState fixture", () => {
  it("is clearly a not_started fake_driver with all 11 stages", () => {
    const s = deterministicBrainState("proj");
    expect(s.state).toBe("not_started");
    expect(s.brain.orchestration).toBe("fake_driver");
    expect(s.stages.map((x) => x.id)).toEqual([
      "research", "proposal", "script", "scene_plan", "assets", "narration",
      "edit", "render", "review", "approval", "complete",
    ]);
    expect(s.stages.every((x) => x.status === "pending")).toBe(true);
    // never claims to be live
    expect(isLive(s, true)).toBe(false);
  });
});

describe("run-state sets", () => {
  it("cancelling is ACTIVE (retryable), not TERMINAL", () => {
    expect(ACTIVE_RUN_STATES.has("cancelling")).toBe(true);
    expect(TERMINAL_RUN_STATES.has("cancelling")).toBe(false);
    expect(TERMINAL_RUN_STATES.has("cancelled")).toBe(true);
    expect(TERMINAL_RUN_STATES.has("failed")).toBe(true);
    expect(TERMINAL_RUN_STATES.has("completed")).toBe(true);
  });
});

describe("prefId", () => {
  it("prefers pref_id then id", () => {
    expect(prefId({ pref_id: "pf_1", category: "c", key: "k", value: 1 })).toBe("pf_1");
    expect(prefId({ id: "id_2", category: "c", key: "k", value: 1 })).toBe("id_2");
  });
});
