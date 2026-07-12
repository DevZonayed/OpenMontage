import { describe, it, expect } from "vitest";
import { BacklotClient, FetchLike, OfflineError } from "./client";

function jsonRes(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe("BacklotClient", () => {
  it("reads the timeline over HTTP", async () => {
    const calls: string[] = [];
    const fetchImpl: FetchLike = async (url) => {
      calls.push(url);
      return jsonRes({ timeline: { fps: 30, target_duration_seconds: 5, total_frames: 150, layers: [] }, etag: "e1" });
    };
    const c = new BacklotClient({ projectId: "demo", fetchImpl, fixtureFallback: false });
    const p = await c.getTimeline();
    expect(p.etag).toBe("e1");
    expect(calls[0]).toBe("/api/project/demo/timeline");
    expect(c.usedFixture).toBe(false);
  });

  it("falls back to deterministic fixtures when the server is unreachable", async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error("ECONNREFUSED");
    };
    const c = new BacklotClient({ projectId: "demo", fetchImpl, fixtureFallback: true });
    const p = await c.getTimeline();
    expect(c.usedFixture).toBe(true);
    expect(p.timeline.total_frames).toBe(360);
  });

  it("attaches the CSRF token and if_match on save", async () => {
    const seen: { url: string; headers?: Record<string, string>; body?: string }[] = [];
    const fetchImpl: FetchLike = async (url, init) => {
      seen.push({ url, headers: init?.headers, body: init?.body });
      if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "TOK123" });
      return jsonRes({ ok: true, etag: "e2" });
    };
    const c = new BacklotClient({ projectId: "demo", fetchImpl });
    const r = await c.saveTimeline(
      { fps: 30, target_duration_seconds: 5, total_frames: 150, layers: [] },
      "e1",
    );
    expect(r.etag).toBe("e2");
    const save = seen.find((s) => s.url === "/api/project/demo/timeline")!;
    expect(save.headers?.["X-OpenMontage-CSRF"]).toBe("TOK123");
    expect(JSON.parse(save.body!).if_match).toBe("e1");
  });

  it("surfaces the server detail message on a 409 conflict", async () => {
    const fetchImpl: FetchLike = async (url) => {
      if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "TOK" });
      return jsonRes({ detail: "Timeline changed on disk" }, false, 409);
    };
    const c = new BacklotClient({ projectId: "demo", fetchImpl });
    await expect(
      c.saveTimeline({ fps: 30, target_duration_seconds: 5, total_frames: 150, layers: [] }, "stale"),
    ).rejects.toThrow("Timeline changed on disk");
  });

  it("queues a revision with the backend's `prompt` field (not `instructions`)", async () => {
    let body: Record<string, unknown> | null = null;
    const fetchImpl: FetchLike = async (url, init) => {
      if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "TOK" });
      body = JSON.parse(init!.body!) as Record<string, unknown>;
      return jsonRes({ ok: true });
    };
    const c = new BacklotClient({ projectId: "demo", fetchImpl });
    await c.queueRevision("hero1", "make it warmer");
    expect(body).toEqual({ layer_id: "hero1", prompt: "make it warmer", constraints: undefined });
  });

  it("refuses to fake a mutation in fixture/offline mode", async () => {
    const c = new BacklotClient({ projectId: "demo", forceFixtures: true });
    await expect(c.renderTimeline()).rejects.toBeInstanceOf(OfflineError);
    // but reads still work from fixtures
    const p = await c.getTimeline();
    expect(p.timeline.total_frames).toBe(360);
  });
});
