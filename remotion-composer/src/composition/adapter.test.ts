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

  it("serializes richer edit fields (volume/transform/fade/transitions/offset) into the backend doc and back", () => {
    const c = backendPayloadToCanonical(deterministicTimelinePayload());
    const music = c.layers.find((l) => l.id === "music1")!;
    music.volume = 0.25;
    music.fade = { inFrames: 15, outFrames: 20 };
    const title = c.layers.find((l) => l.id === "title1")!;
    title.transform = { x: 10, scale: 1.2, opacity: 0.8 };
    title.transitionIn = { kind: "slide", durationFrames: 12 };
    title.sourceOffsetFrames = 30;

    const doc = canonicalToBackendDoc(c);
    const bMusic = doc.layers.find((l) => l.id === "music1")!;
    const bTitle = doc.layers.find((l) => l.id === "title1")!;
    expect(bMusic.volume).toBe(0.25);
    expect(bMusic.fade).toEqual({ inFrames: 15, outFrames: 20 });
    expect(bTitle.transform).toEqual({ x: 10, scale: 1.2, opacity: 0.8 });
    expect(bTitle.transitionIn).toEqual({ kind: "slide", durationFrames: 12 });
    expect(bTitle.sourceOffsetFrames).toBe(30);

    // Reloading the saved doc must not silently drop them.
    const c2 = backendToCanonical(doc);
    const m2 = c2.layers.find((l) => l.id === "music1")!;
    const t2 = c2.layers.find((l) => l.id === "title1")!;
    expect(m2.volume).toBe(0.25);
    expect(m2.fade).toEqual({ inFrames: 15, outFrames: 20 });
    expect(t2.transform).toEqual({ x: 10, scale: 1.2, opacity: 0.8 });
    expect(t2.transitionIn).toEqual({ kind: "slide", durationFrames: 12 });
    expect(t2.sourceOffsetFrames).toBe(30);
  });

  it("a muted audio track silences its audio layers (volume 0) in the render doc", () => {
    const c = backendPayloadToCanonical(deterministicTimelinePayload());
    const audio = c.tracks.find((t) => t.id === "audio")!;
    audio.muted = true;
    const doc = canonicalToBackendDoc(c);
    for (const id of ["narr1", "music1"]) {
      expect(doc.layers.find((l) => l.id === id)!.volume).toBe(0);
    }
    // visual/text layers are unaffected
    expect(doc.layers.find((l) => l.id === "title1")!.volume).toBeUndefined();
  });

  it("recomputes an inconsistent backend total_frames instead of trusting it", () => {
    const doc: BackendTimelineDoc = {
      version: "1.0",
      fps: 30,
      target_duration_seconds: 10,
      total_frames: 999, // WRONG — should be 300
      layers: [],
    };
    const c = backendToCanonical(doc);
    expect(c.totalFrames).toBe(300);
    // a consistent value is preserved as-is
    const good = backendToCanonical({ ...doc, total_frames: 300 });
    expect(good.totalFrames).toBe(300);
  });

  it("falls back to a safe fps when the backend fps is non-positive", () => {
    const doc: BackendTimelineDoc = {
      fps: 0,
      target_duration_seconds: 5,
      total_frames: 0,
      layers: [],
    };
    const c = backendToCanonical(doc);
    expect(c.fps).toBe(30);
    expect(c.totalFrames).toBe(150);
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
