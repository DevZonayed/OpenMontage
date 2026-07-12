import { describe, it, expect } from "vitest";
import {
  CanonicalComposition,
  CompositionAsset,
  createEmptyComposition,
  Layer,
} from "./model";
import {
  addLayer,
  moveLayer,
  muteTrack,
  OperationError,
  removeLayer,
  reorderScene,
  replaceAsset,
  resizeLayer,
  revertAsset,
  setAssetApproval,
  setVolume,
  splitLayer,
  trimLayer,
} from "./operations";

function mkLayer(p: Partial<Layer> & { id: string }): Layer {
  return {
    type: "image",
    trackId: "visual",
    startFrame: 0,
    durationFrames: 90,
    z: 0,
    enabled: true,
    locked: false,
    opacity: 1,
    ...p,
  };
}

function comp(): CanonicalComposition {
  const c = createEmptyComposition({ id: "p1", targetDurationSeconds: 20 }); // 600 frames
  c.layers = [
    mkLayer({ id: "a", startFrame: 0, durationFrames: 100 }),
    mkLayer({ id: "b", startFrame: 100, durationFrames: 100, type: "music", trackId: "audio" }),
  ];
  return c;
}

describe("immutability", () => {
  it("operations never mutate their input", () => {
    const c = comp();
    const snapshot = JSON.stringify(c);
    moveLayer(c, "a", 50);
    resizeLayer(c, "a", 42);
    removeLayer(c, "b");
    expect(JSON.stringify(c)).toBe(snapshot);
  });
});

describe("move/resize/trim", () => {
  it("moves and clamps to >= 0", () => {
    expect(moveLayer(comp(), "a", 250).layers[0].startFrame).toBe(250);
    expect(moveLayer(comp(), "a", -10).layers[0].startFrame).toBe(0);
  });
  it("resizes and clamps to >= 1", () => {
    expect(resizeLayer(comp(), "a", 7).layers[0].durationFrames).toBe(7);
    expect(resizeLayer(comp(), "a", 0).layers[0].durationFrames).toBe(1);
  });
  it("trims both edges at once", () => {
    const r = trimLayer(comp(), "a", { startFrame: 30, durationFrames: 40 });
    expect(r.layers[0].startFrame).toBe(30);
    expect(r.layers[0].durationFrames).toBe(40);
  });
  it("refuses to edit a locked layer", () => {
    const c = comp();
    c.layers[0].locked = true;
    expect(() => moveLayer(c, "a", 5)).toThrow(OperationError);
  });
});

describe("split", () => {
  it("splits into two continuous clips with a stable new id, preserving asset", () => {
    const c = comp();
    c.layers[0].assetId = "asset_a";
    c.assets = [
      { id: "asset_a", kind: "image", url: "/media/p1/a.png", status: "approved", approved: true, version: 1 },
    ];
    const { composition, newLayerId } = splitLayer(c, "a", 40);
    const first = composition.layers.find((l) => l.id === "a")!;
    const second = composition.layers.find((l) => l.id === newLayerId)!;
    expect(first.durationFrames).toBe(40);
    expect(second.startFrame).toBe(40);
    expect(second.durationFrames).toBe(60);
    expect(second.assetId).toBe("asset_a");
    expect(second.sourceOffsetFrames).toBe(40);
    // deterministic: same split → same id
    expect(splitLayer(comp(), "a", 40).newLayerId).toBe(newLayerId);
  });
  it("rejects a split at or outside the boundary", () => {
    expect(() => splitLayer(comp(), "a", 0)).toThrow();
    expect(() => splitLayer(comp(), "a", 100)).toThrow();
    expect(() => splitLayer(comp(), "a", 500)).toThrow();
  });
});

describe("volume / mute", () => {
  it("sets and clamps volume", () => {
    expect(setVolume(comp(), "b", 0.5).layers[1].volume).toBe(0.5);
    expect(setVolume(comp(), "b", 5).layers[1].volume).toBe(1);
    expect(setVolume(comp(), "b", -1).layers[1].volume).toBe(0);
  });
  it("mutes a track", () => {
    const r = muteTrack(comp(), "audio", true);
    expect(r.tracks.find((t) => t.id === "audio")!.muted).toBe(true);
  });
});

describe("add/remove", () => {
  it("adds a unique layer and rejects duplicates", () => {
    const r = addLayer(comp(), mkLayer({ id: "c" }));
    expect(r.layers.map((l) => l.id)).toContain("c");
    expect(() => addLayer(r, mkLayer({ id: "a" }))).toThrow(OperationError);
  });
  it("removes and errors on unknown", () => {
    expect(removeLayer(comp(), "a").layers.map((l) => l.id)).toEqual(["b"]);
    expect(() => removeLayer(comp(), "zzz")).toThrow(OperationError);
  });
});

describe("selective regeneration — replaceAsset / revert / approval", () => {
  function withAsset(): CanonicalComposition {
    const c = comp();
    const a: CompositionAsset = {
      id: "asset_a",
      kind: "image",
      url: "/media/p1/v1.png",
      status: "approved",
      approved: true,
      version: 1,
    };
    c.assets = [a];
    c.layers[0].assetId = "asset_a";
    return c;
  }

  it("replaces media without changing the stable asset id and preserves approval", () => {
    const r = replaceAsset(withAsset(), "asset_a", {
      url: "/media/p1/v2.png",
      status: "ready",
      provenance: { provider: "flux", model: "flux-pro" },
    });
    const a = r.assets[0];
    expect(a.id).toBe("asset_a"); // stable id preserved
    expect(a.url).toBe("/media/p1/v2.png");
    expect(a.version).toBe(2);
    expect(a.approved).toBe(true); // approval preserved across regen
    // layer still points at the same asset (unrelated layers untouched)
    expect(r.layers[0].assetId).toBe("asset_a");
    expect(r.layers[1]).toEqual(withAsset().layers[1]);
  });

  it("can revert to a previous version (visual compare/revert)", () => {
    const v2 = replaceAsset(withAsset(), "asset_a", { url: "/media/p1/v2.png" });
    const back = revertAsset(v2, "asset_a", 1);
    expect(back.assets[0].url).toBe("/media/p1/v1.png");
    expect(back.assets[0].version).toBe(1);
  });

  it("toggles approval independently", () => {
    const r = setAssetApproval(withAsset(), "asset_a", false);
    expect(r.assets[0].approved).toBe(false);
  });
});

describe("reorderScene", () => {
  it("moves a scene and clamps the target index", () => {
    const c = comp();
    c.scenes = [
      { id: "s1", name: "One", startFrame: 0, durationFrames: 100 },
      { id: "s2", name: "Two", startFrame: 100, durationFrames: 100 },
      { id: "s3", name: "Three", startFrame: 200, durationFrames: 100 },
    ];
    expect(reorderScene(c, 0, 2).scenes.map((s) => s.id)).toEqual(["s2", "s3", "s1"]);
    expect(reorderScene(c, 2, 99).scenes.map((s) => s.id)).toEqual(["s1", "s2", "s3"]);
  });
});
