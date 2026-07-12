import { describe, it, expect } from "vitest";
import {
  CanonicalComposition,
  CompositionAsset,
  createEmptyComposition,
  Layer,
} from "./model";
import { isSafeAssetUrl, validateComposition } from "./validate";

function layer(partial: Partial<Layer> & { id: string }): Layer {
  return {
    type: "image",
    trackId: "visual",
    startFrame: 0,
    durationFrames: 90,
    z: 0,
    enabled: true,
    locked: false,
    opacity: 1,
    ...partial,
  };
}

function asset(partial: Partial<CompositionAsset> & { id: string }): CompositionAsset {
  return {
    kind: "image",
    url: "/media/p1/assets/images/a.png",
    status: "ready",
    approved: false,
    version: 1,
    ...partial,
  };
}

function base(): CanonicalComposition {
  return createEmptyComposition({ id: "p1", targetDurationSeconds: 10 }); // 300 frames
}

describe("isSafeAssetUrl", () => {
  it("accepts null, http(s), blob, and project-local paths", () => {
    expect(isSafeAssetUrl(null)).toBe(true);
    expect(isSafeAssetUrl("https://cdn.example.com/x.mp4")).toBe(true);
    expect(isSafeAssetUrl("http://localhost:4750/media/p1/a.png")).toBe(true);
    expect(isSafeAssetUrl("blob:https://x/abc")).toBe(true);
    expect(isSafeAssetUrl("/media/p1/assets/images/a.png")).toBe(true);
    expect(isSafeAssetUrl("assets/video/clip.mp4")).toBe(true);
  });
  it("rejects unsafe schemes, traversal, backslashes, control chars", () => {
    expect(isSafeAssetUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeAssetUrl("JavaScript:alert(1)")).toBe(false);
    expect(isSafeAssetUrl("data:text/html;base64,PHNjcmlwdD4=")).toBe(false);
    expect(isSafeAssetUrl("file:///etc/passwd")).toBe(false);
    expect(isSafeAssetUrl("vbscript:msgbox(1)")).toBe(false);
    expect(isSafeAssetUrl("../../../etc/passwd")).toBe(false);
    expect(isSafeAssetUrl("assets/../../secret")).toBe(false);
    expect(isSafeAssetUrl("a\\b")).toBe(false);
    expect(isSafeAssetUrl("http://x/\nabc")).toBe(false);
    expect(isSafeAssetUrl("   ")).toBe(false);
  });
});

describe("validateComposition", () => {
  it("passes a clean empty composition", () => {
    const r = validateComposition(base());
    expect(r.ok).toBe(true);
    expect(r.errors).toEqual([]);
    expect(r.renderReady).toBe(true);
  });

  it("flags a broken total_frames invariant", () => {
    const c = base();
    c.totalFrames = 999;
    const r = validateComposition(c);
    expect(r.ok).toBe(false);
    expect(r.errors.map((e) => e.code)).toContain("total_frames");
  });

  it("rejects an out-of-contract duration (0 and 301)", () => {
    for (const secs of [0, 301]) {
      const c = base();
      c.targetDurationSeconds = secs as number;
      const r = validateComposition(c);
      expect(r.ok).toBe(false);
      expect(r.errors.map((e) => e.code)).toContain("target_duration");
    }
  });

  it("detects duplicate and malformed layer ids", () => {
    const c = base();
    c.layers = [layer({ id: "a" }), layer({ id: "a" }), layer({ id: "bad id" })];
    const r = validateComposition(c);
    expect(r.errors.map((e) => e.code)).toEqual(
      expect.arrayContaining(["layer_dup", "layer_id"]),
    );
  });

  it("rejects unsafe asset urls", () => {
    const c = base();
    c.assets = [asset({ id: "img1", url: "javascript:alert(1)" })];
    const r = validateComposition(c);
    expect(r.ok).toBe(false);
    expect(r.errors.map((e) => e.code)).toContain("asset_url");
  });

  it("errors on a dangling asset reference", () => {
    const c = base();
    c.layers = [layer({ id: "L1", assetId: "ghost" })];
    const r = validateComposition(c);
    expect(r.ok).toBe(false);
    expect(r.errors.map((e) => e.code)).toContain("layer_asset_missing");
  });

  it("is renderable-but-not-render-ready with a placeholder asset (missing asset)", () => {
    const c = base();
    c.assets = [asset({ id: "img1", url: null, status: "placeholder" })];
    c.layers = [layer({ id: "L1", assetId: "img1" })];
    const r = validateComposition(c);
    expect(r.ok).toBe(true); // structurally valid
    expect(r.renderReady).toBe(false); // but not ready to render
  });

  it("is render-ready once the asset is ready with a safe url", () => {
    const c = base();
    c.assets = [asset({ id: "img1", url: "/media/p1/a.png", status: "approved" })];
    c.layers = [layer({ id: "L1", assetId: "img1" })];
    const r = validateComposition(c);
    expect(r.renderReady).toBe(true);
  });

  it("rejects out-of-range volume and opacity", () => {
    const c = base();
    c.layers = [layer({ id: "L1", type: "music", trackId: "audio", volume: 2, opacity: -0.1 })];
    const r = validateComposition(c);
    expect(r.errors.map((e) => e.code)).toEqual(
      expect.arrayContaining(["layer_volume", "layer_opacity"]),
    );
  });

  it("warns (not errors) when a layer overflows totalFrames", () => {
    const c = base(); // 300 frames
    c.layers = [layer({ id: "L1", startFrame: 250, durationFrames: 200 })];
    const r = validateComposition(c);
    expect(r.ok).toBe(true);
    expect(r.warnings.map((w) => w.code)).toContain("layer_overflow");
  });
});
