import { describe, it, expect } from "vitest";
import { isLoadableUrl } from "../TimelineComposition";
import { isSafeAssetUrl } from "./validate";

// Regression: the composition must treat same-origin absolute paths (/media/...,
// /thumb/...) as loadable media, matching what validation deems safe/render-ready.
// Otherwise a valid project-local asset renders as a placeholder / silent audio.
describe("isLoadableUrl", () => {
  it("accepts absolute URLs, blob, and same-origin absolute paths", () => {
    expect(isLoadableUrl("https://cdn.example.com/x.mp4")).toBe(true);
    expect(isLoadableUrl("http://localhost:4750/media/p1/a.png")).toBe(true);
    expect(isLoadableUrl("//cdn.example.com/x.mp4")).toBe(true);
    expect(isLoadableUrl("blob:https://x/abc")).toBe(true);
    expect(isLoadableUrl("/media/p1/assets/images/a.png")).toBe(true);
    expect(isLoadableUrl("/thumb/p1/a.png")).toBe(true);
  });

  it("rejects bare relative paths, null, traversal, and backslashes", () => {
    expect(isLoadableUrl(null)).toBe(false);
    expect(isLoadableUrl(undefined)).toBe(false);
    expect(isLoadableUrl("assets/video/clip.mp4")).toBe(false); // relative → placeholder
    expect(isLoadableUrl("/media/../secret")).toBe(false);
    expect(isLoadableUrl("/a\\b")).toBe(false);
    expect(isLoadableUrl("")).toBe(false);
  });

  it("every same-origin /media path it accepts is also validation-safe (no contradiction)", () => {
    const urls = ["/media/p1/a.png", "/thumb/p1/b.jpg", "https://cdn/x.mp4", "blob:https://x/y"];
    for (const u of urls) {
      if (isLoadableUrl(u)) expect(isSafeAssetUrl(u)).toBe(true);
    }
  });
});
