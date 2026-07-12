import { describe, it, expect } from "vitest";
import { isResolvableMedia, resolveAssetSrc } from "./media";
import { backendToCanonical, canonicalToBackendDoc, renderProps } from "./adapter";

describe("resolveAssetSrc — path safety", () => {
  it("passes absolute http/https/blob URLs through unchanged", () => {
    expect(resolveAssetSrc("https://cdn/x.mp4", {})).toBe("https://cdn/x.mp4");
    expect(resolveAssetSrc("http://h/x.png", {})).toBe("http://h/x.png");
    expect(resolveAssetSrc("//cdn/x.mp4", {})).toBe("//cdn/x.mp4");
    expect(resolveAssetSrc("blob:https://x/y", {})).toBe("blob:https://x/y");
  });
  it("rejects unsafe schemes, traversal, backslashes, and empty", () => {
    expect(resolveAssetSrc("javascript:alert(1)", { projectId: "p", assetBaseUrl: "" })).toBeNull();
    expect(resolveAssetSrc("file:///etc/passwd", { projectId: "p" })).toBeNull();
    expect(resolveAssetSrc("assets/../../secret", { projectId: "p" })).toBeNull();
    expect(resolveAssetSrc("/media/../secret", { projectId: "p" })).toBeNull();
    expect(resolveAssetSrc("a\\b", { projectId: "p" })).toBeNull();
    expect(resolveAssetSrc("   ", { projectId: "p" })).toBeNull();
    expect(resolveAssetSrc(null, {})).toBeNull();
  });
  it("maps a project-local relative path to /media/{projectId}/{path}", () => {
    expect(resolveAssetSrc("assets/images/a.png", { projectId: "proj1", assetBaseUrl: "http://127.0.0.1:4750" })).toBe(
      "http://127.0.0.1:4750/media/proj1/assets/images/a.png",
    );
    // same-origin (no base): a root-relative /media path is prefixed by base only
    expect(resolveAssetSrc("/media/proj1/a.png", { assetBaseUrl: "" })).toBe("/media/proj1/a.png");
  });
  it("cannot resolve a relative path without a projectId (→ placeholder)", () => {
    expect(resolveAssetSrc("assets/a.png", { assetBaseUrl: "http://x" })).toBeNull();
    expect(isResolvableMedia("assets/a.png", {})).toBe(false);
  });
});

// The parity guarantee: the Player and the pinned CLI both receive the SAME
// timeline doc (relative source) AND the SAME meta (projectId + assetBaseUrl) from
// renderProps, then apply the SAME resolveAssetSrc — so a saved project-local
// source becomes the identical loadable URL for both. It is NEVER real in preview
// but a placeholder in the render.
describe("Player/CLI media resolution parity", () => {
  it("a saved project-local source resolves to the same URL for preview and render", () => {
    const doc = {
      version: "1.0",
      fps: 30,
      target_duration_seconds: 5,
      total_frames: 150,
      layers: [
        { id: "bg", type: "video", start_frame: 0, duration_frames: 150, source: "assets/video/clip.mp4" },
      ],
    };
    const c = backendToCanonical(doc, { id: "proj1" });

    // The doc that BOTH the Player and the CLI render keeps the project-local path.
    const back = canonicalToBackendDoc(c);
    expect(back.layers[0].source).toBe("assets/video/clip.mp4");

    // renderProps carries the base + projectId the composition resolves against —
    // identical for the Player and for a CLI render given the same base.
    const base = "http://127.0.0.1:4750";
    const props = renderProps(c, { assetBaseUrl: base, projectId: "proj1" });
    expect(props.meta?.projectId).toBe("proj1");
    expect(props.meta?.assetBaseUrl).toBe(base);

    const src = props.timeline.layers[0].source;
    const playerUrl = resolveAssetSrc(src, { projectId: props.meta!.projectId, assetBaseUrl: props.meta!.assetBaseUrl });
    const cliUrl = resolveAssetSrc(src, { projectId: props.meta!.projectId, assetBaseUrl: props.meta!.assetBaseUrl });

    expect(playerUrl).toBe("http://127.0.0.1:4750/media/proj1/assets/video/clip.mp4");
    expect(cliUrl).toBe(playerUrl); // identical for both — no preview/render drift
    expect(playerUrl).not.toBeNull(); // and NOT a placeholder
  });
});
