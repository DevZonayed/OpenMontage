import { describe, it, expect } from "vitest";
import {
  audioRow,
  clampPercent,
  MAX_AUDIO_ROWS,
  safeArea,
  SAFE_MARGIN_X,
  SAFE_MARGIN_Y,
  stackWithinZone,
  truncateLabel,
  volumePercent,
  ZONE_ORDER,
  ZONES,
  zoneRect,
} from "./layout";

const W = 1920;
const H = 1080;

describe("safe area", () => {
  it("is symmetric and inside the frame", () => {
    const s = safeArea(W, H);
    expect(s.left).toBe(Math.round(W * SAFE_MARGIN_X));
    expect(s.top).toBe(Math.round(H * SAFE_MARGIN_Y));
    expect(s.right).toBe(W - s.left);
    expect(s.bottom).toBe(H - s.top);
    expect(s.width).toBe(W - 2 * s.left);
    expect(s.left).toBeGreaterThan(0);
    expect(s.right).toBeLessThan(W);
  });
});

describe("zones are disjoint (no collisions by construction)", () => {
  it("each band's bottom <= the next band's top", () => {
    for (let i = 0; i < ZONE_ORDER.length - 1; i++) {
      const cur = ZONES[ZONE_ORDER[i]];
      const next = ZONES[ZONE_ORDER[i + 1]];
      expect(cur.bottom).toBeLessThanOrEqual(next.top);
    }
  });

  it("zoneRects for adjacent zones do not overlap in pixels", () => {
    for (let i = 0; i < ZONE_ORDER.length - 1; i++) {
      const cur = zoneRect(ZONE_ORDER[i], W, H);
      const next = zoneRect(ZONE_ORDER[i + 1], W, H);
      expect(cur.top + cur.height).toBeLessThanOrEqual(next.top);
    }
  });

  it("every zone stays within the safe area vertically", () => {
    const s = safeArea(W, H);
    for (const z of ZONE_ORDER) {
      const r = zoneRect(z, W, H);
      expect(r.top).toBeGreaterThanOrEqual(s.top - 1);
      expect(r.top + r.height).toBeLessThanOrEqual(s.bottom + 1);
      expect(r.left).toBe(s.left);
      expect(r.width).toBe(s.width);
    }
  });
});

describe("clampPercent — long/edge values stay legible", () => {
  it("bounds to 0..100 and never emits absurd values", () => {
    expect(clampPercent(4090)).toBe("100%");
    expect(clampPercent(100)).toBe("100%");
    expect(clampPercent(80)).toBe("80%");
    expect(clampPercent(0)).toBe("0%");
    expect(clampPercent(-5)).toBe("0%");
    expect(clampPercent(NaN)).toBe("0%");
    expect(clampPercent(Infinity)).toBe("0%");
  });
  it("volumePercent maps 0..1", () => {
    expect(volumePercent(0.8)).toBe("80%");
    expect(volumePercent(1)).toBe("100%");
    expect(volumePercent(undefined)).toBe("100%");
    expect(volumePercent(0)).toBe("0%");
  });
});

describe("truncateLabel", () => {
  it("keeps short labels and ellipsizes long ones within budget", () => {
    expect(truncateLabel("Short label")).toBe("Short label");
    const long = "This is an extremely long lower-third label that would overflow";
    const t = truncateLabel(long, 30);
    expect(t.length).toBeLessThanOrEqual(30);
    expect(t.endsWith("…")).toBe(true);
  });
});

describe("audioRow — stacked, non-overlapping presence rows", () => {
  it("stacks up to MAX_AUDIO_ROWS without overlap, inside the audio band", () => {
    const band = zoneRect("audio", W, H);
    const rows = Array.from({ length: MAX_AUDIO_ROWS }, (_, i) => audioRow(i, W, H));
    // sort by top and assert no vertical overlap
    const sorted = rows.slice().sort((a, b) => a.top - b.top);
    for (let i = 0; i < sorted.length - 1; i++) {
      expect(sorted[i].top + sorted[i].height).toBeLessThanOrEqual(sorted[i + 1].top);
    }
    // all rows within the audio band
    for (const r of rows) {
      expect(r.top).toBeGreaterThanOrEqual(band.top);
      expect(r.top + r.height).toBeLessThanOrEqual(band.top + band.height + 1);
    }
    // distinct slots produce distinct positions
    expect(new Set(rows.map((r) => r.top)).size).toBe(MAX_AUDIO_ROWS);
  });
});

describe("stackWithinZone", () => {
  it("returns non-overlapping tops for N items in a band", () => {
    const tops = stackWithinZone(3, "lowerThird", W, H);
    expect(tops.length).toBe(3);
    for (let i = 0; i < tops.length - 1; i++) {
      expect(tops[i]).toBeLessThan(tops[i + 1]);
    }
  });
});
