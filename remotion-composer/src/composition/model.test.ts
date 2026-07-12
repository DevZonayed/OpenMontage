import { describe, it, expect } from "vitest";
import {
  createEmptyComposition,
  framesForDuration,
  isValidId,
  makeId,
  MAX_DURATION_SECONDS,
  MODEL_VERSION,
  trackKindForType,
} from "./model";

describe("framesForDuration — parity with lib/duration.py::frames_for", () => {
  it("computes exact integer frames for the canonical presets", () => {
    expect(framesForDuration(30, 30)).toBe(900);
    expect(framesForDuration(60, 30)).toBe(1800);
    expect(framesForDuration(150, 30)).toBe(4500);
    expect(framesForDuration(300, 30)).toBe(9000); // 5:00 hard invariant
  });

  it("honors non-30 fps exactly", () => {
    expect(framesForDuration(10, 24)).toBe(240);
    expect(framesForDuration(300, 60)).toBe(18000);
  });

  it("rejects out-of-range and non-integer durations", () => {
    expect(() => framesForDuration(0)).toThrow();
    expect(() => framesForDuration(MAX_DURATION_SECONDS + 1)).toThrow();
    expect(() => framesForDuration(1.5)).toThrow();
    expect(() => framesForDuration(NaN)).toThrow();
    expect(() => framesForDuration(60, 0)).toThrow();
    expect(() => framesForDuration(60, -30)).toThrow();
  });
});

describe("ids", () => {
  it("validates the backend id grammar", () => {
    expect(isValidId("cut1")).toBe(true);
    expect(isValidId("A-b_c-9")).toBe(true);
    expect(isValidId("-bad")).toBe(false);
    expect(isValidId("has space")).toBe(false);
    expect(isValidId("")).toBe(false);
    expect(isValidId("x".repeat(65))).toBe(false);
  });
  it("makeId is deterministic and valid", () => {
    expect(makeId("layer", 3)).toBe("layer_3");
    expect(makeId("layer", 3)).toBe(makeId("layer", 3));
    expect(isValidId(makeId("scene#!", 7))).toBe(true);
  });
});

describe("createEmptyComposition", () => {
  it("derives totalFrames from duration and stamps the model version", () => {
    const c = createEmptyComposition({ id: "p1", targetDurationSeconds: 300 });
    expect(c.version).toBe(MODEL_VERSION);
    expect(c.totalFrames).toBe(9000);
    expect(c.tracks.map((t) => t.kind)).toEqual(["visual", "text", "audio"]);
    expect(c.layers).toEqual([]);
    expect(c.approval).toBe("draft");
  });
});

describe("trackKindForType", () => {
  it("routes each layer type to a track kind", () => {
    expect(trackKindForType("video")).toBe("visual");
    expect(trackKindForType("image")).toBe("visual");
    expect(trackKindForType("shape")).toBe("visual");
    expect(trackKindForType("text")).toBe("text");
    expect(trackKindForType("caption")).toBe("text");
    expect(trackKindForType("narration")).toBe("audio");
    expect(trackKindForType("music")).toBe("audio");
    expect(trackKindForType("sfx")).toBe("audio");
  });
});
