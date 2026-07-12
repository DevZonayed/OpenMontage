import { describe, it, expect } from "vitest";
import { History } from "./history";
import { createEmptyComposition } from "./model";
import { addLayer, moveLayer } from "./operations";

function mk() {
  return createEmptyComposition({ id: "p1", targetDurationSeconds: 10 });
}

describe("History", () => {
  it("starts with nothing to undo/redo", () => {
    const h = new History(mk());
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);
  });

  it("undo/redo walks the timeline of edits", () => {
    const h = new History(mk());
    const v1 = addLayer(h.present, {
      id: "a",
      type: "image",
      trackId: "visual",
      startFrame: 0,
      durationFrames: 30,
      z: 0,
      enabled: true,
      locked: false,
      opacity: 1,
    });
    h.commit(v1);
    const v2 = moveLayer(h.present, "a", 15);
    h.commit(v2);
    expect(h.present.layers[0].startFrame).toBe(15);

    h.undo();
    expect(h.present.layers[0].startFrame).toBe(0);
    h.undo();
    expect(h.present.layers.length).toBe(0);
    expect(h.canUndo).toBe(false);

    h.redo();
    expect(h.present.layers.length).toBe(1);
    h.redo();
    expect(h.present.layers[0].startFrame).toBe(15);
    expect(h.canRedo).toBe(false);
  });

  it("a new commit clears the redo stack", () => {
    const h = new History(mk());
    h.commit(addLayer(h.present, {
      id: "a", type: "image", trackId: "visual", startFrame: 0, durationFrames: 30,
      z: 0, enabled: true, locked: false, opacity: 1,
    }));
    h.undo();
    expect(h.canRedo).toBe(true);
    h.commit(addLayer(h.present, {
      id: "b", type: "text", trackId: "text", startFrame: 0, durationFrames: 30,
      z: 1, enabled: true, locked: false, opacity: 1,
    }));
    expect(h.canRedo).toBe(false);
  });

  it("reset clears both stacks", () => {
    const h = new History(mk());
    h.commit(mk());
    h.reset(mk());
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);
  });

  it("is bounded", () => {
    const h = new History(mk(), 3);
    for (let i = 0; i < 10; i++) h.commit(mk());
    expect(h.snapshot().undoDepth).toBe(3);
  });
});
