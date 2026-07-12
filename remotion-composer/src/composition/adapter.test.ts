import { describe, it, expect } from "vitest";
import {
  backendPayloadToCanonical,
  backendToCanonical,
  BackendTimelineDoc,
  canonicalToBackendDoc,
  renderProps,
} from "./adapter";
import { deterministicTimelinePayload } from "./fixtures";
import { moveLayer, resizeLayer } from "./operations";
import { validateComposition } from "./validate";

// Fields the Remotion renderer actually reads for a layer. Round-trip stability of
// exactly these is what guarantees the Player preview equals the CLI render.
function renderRelevant(doc: BackendTimelineDoc) {
  return {
    fps: doc.fps,
    total_frames: doc.total_frames,
    target_duration_seconds: doc.target_duration_seconds,
    layers: doc.layers.map((l) => ({
      id: l.id,
      type: l.type,
      start_frame: l.start_frame,
      duration_frames: l.duration_frames,
      z: l.z,
      enabled: l.enabled,
      opacity: l.opacity,
      text: l.text,
      source: l.source ?? null,
    })),
  };
}

describe("adapter round-trip (preview/render parity)", () => {
  it("backend → canonical → backend preserves every render-relevant field", () => {
    const doc = deterministicTimelinePayload().timeline;
    const c = backendToCanonical(doc);
    const back = canonicalToBackendDoc(c);
    expect(renderRelevant(back)).toEqual(renderRelevant(doc));
  });

  it("renderProps carries the SAME timeline doc the CLI would render", () => {
    const c = backendPayloadToCanonical(deterministicTimelinePayload(), { title: "Demo" });
    const props = renderProps(c);
    // The Player renders props.timeline; the CLI renders canonicalToBackendDoc(c).
    // They must be byte-identical, or preview and render would diverge.
    expect(props.timeline).toEqual(canonicalToBackendDoc(c));
    expect(props.meta?.title).toBe("Demo");
  });

  it("an edit shows up identically in the props fed to preview and render", () => {
    const c0 = backendPayloadToCanonical(deterministicTimelinePayload());
    const c1 = resizeLayer(moveLayer(c0, "title1", 45), "title1", 200);
    const doc = canonicalToBackendDoc(c1);
    const layer = doc.layers.find((l) => l.id === "title1")!;
    expect(layer.start_frame).toBe(45);
    expect(layer.duration_frames).toBe(200);
    // and the exact same doc is what renderProps hands the Player
    expect(renderProps(c1).timeline).toEqual(doc);
  });

  it("keeps the 300s → 9000 frame invariant through the adapter", () => {
    const doc: BackendTimelineDoc = {
      version: "1.0",
      fps: 30,
      target_duration_seconds: 300,
      total_frames: 9000,
      layers: [],
    };
    const c = backendToCanonical(doc);
    expect(c.totalFrames).toBe(9000);
    expect(validateComposition(c).ok).toBe(true);
    expect(canonicalToBackendDoc(c).total_frames).toBe(9000);
  });

  it("coerces unknown backend layer types to the inert 'shape' type", () => {
    const doc: BackendTimelineDoc = {
      fps: 30,
      target_duration_seconds: 5,
      total_frames: 150,
      layers: [
        { id: "x", type: "wormhole", start_frame: 0, duration_frames: 30 },
      ],
    };
    const c = backendToCanonical(doc);
    expect(c.layers[0].type).toBe("shape");
  });

  it("synthesizes a stable asset for a layer that has a source", () => {
    const doc: BackendTimelineDoc = {
      fps: 30,
      target_duration_seconds: 5,
      total_frames: 150,
      layers: [
        { id: "clip", type: "video", start_frame: 0, duration_frames: 90, source: "assets/video/a.mp4" },
      ],
    };
    const c = backendToCanonical(doc);
    expect(c.layers[0].assetId).toBeTruthy();
    const a = c.assets.find((x) => x.id === c.layers[0].assetId)!;
    expect(a.url).toBe("assets/video/a.mp4");
    expect(a.kind).toBe("video");
    // and it survives the round-trip back to source
    expect(canonicalToBackendDoc(c).layers[0].source).toBe("assets/video/a.mp4");
  });
});
