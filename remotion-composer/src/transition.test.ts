import { describe, it, expect } from "vitest";
import { transitionStyle } from "./TimelineComposition";

// Regression: an EXIT transition must actually fade/zoom OUT toward the end of the
// exit window — it must NOT start hidden and re-show the outgoing text mid-handoff.
describe("transitionStyle — exit direction", () => {
  const dur = 100;
  const tout = { kind: "fade" as const, durationFrames: 20 };

  it("fade-out opacity is ~full at the start of the exit and ~0 at the very end", () => {
    // exit window is [dur-20, dur] = [80, 100]
    const atStart = transitionStyle(80, dur, undefined, tout).opacity; // remaining=1 → visible
    const mid = transitionStyle(90, dur, undefined, tout).opacity; // remaining=0.5
    const atEnd = transitionStyle(99, dur, undefined, tout).opacity; // remaining≈0.05 → nearly gone
    expect(atStart).toBeGreaterThan(0.95);
    expect(mid).toBeGreaterThan(atEnd);
    expect(atStart).toBeGreaterThan(mid);
    expect(atEnd).toBeLessThan(0.1);
  });

  it("does not re-show outgoing content near the end of the exit", () => {
    // The old inverted bug produced opacity≈1 at frame 99; assert it is now low.
    expect(transitionStyle(99, dur, undefined, tout).opacity).toBeLessThan(0.2);
  });

  it("fade-in opacity rises 0 → ~1 over the enter window", () => {
    const tin = { kind: "fade" as const, durationFrames: 20 };
    const atStart = transitionStyle(0, dur, tin).opacity;
    const atEnd = transitionStyle(19, dur, tin).opacity;
    expect(atStart).toBeLessThan(0.1);
    expect(atEnd).toBeGreaterThan(atStart);
  });

  it("is fully visible (opacity 1) outside any transition window", () => {
    const tin = { kind: "fade" as const, durationFrames: 20 };
    const tout2 = { kind: "fade" as const, durationFrames: 20 };
    expect(transitionStyle(50, dur, tin, tout2).opacity).toBeCloseTo(1, 5);
  });
});
