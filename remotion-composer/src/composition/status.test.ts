import { describe, it, expect } from "vitest";
import { BacklotClient, FetchLike } from "./client";
import { deterministicStatusView } from "./status";

function jsonRes(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) };
}

describe("canonical status view", () => {
  it("demo fixture is clearly labelled and never live", () => {
    const v = deterministicStatusView("demo");
    expect(v.is_demo).toBe(true);
    expect(v.is_live).toBe(false);
    expect(v.stage_count).toBe(11);
    expect(v.stages.length).toBe(11);
    expect(v.stages.filter((s) => s.status === "current").length).toBe(1);
  });

  it("getStatus reads the canonical /status endpoint", async () => {
    const calls: string[] = [];
    const fetchImpl: FetchLike = async (url) => {
      calls.push(url);
      return jsonRes({
        kind: "production_status_view",
        overall_state: "ready_to_produce",
        stage_count: 11,
        primary_action: { id: "continue_hermes", label: "Continue production with Hermes", owner: "hermes", kind: "start", advances_production: true },
        stages: [],
        is_live: true,
        is_demo: false,
        is_fixture: false,
      });
    };
    const c = new BacklotClient({ projectId: "eb", fetchImpl });
    const v = await c.getStatus();
    expect(calls[0]).toBe("/api/project/eb/status");
    expect(v.primary_action.id).toBe("continue_hermes");
    expect(c.usedFixture).toBe(false);
  });

  it("connectHermes posts url+token with CSRF and never echoes the token back", async () => {
    const seen: Array<{ url: string; body: unknown; csrf?: string }> = [];
    const fetchImpl: FetchLike = async (url, init) => {
      if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "TOK" });
      seen.push({ url, body: JSON.parse(init!.body!), csrf: init?.headers?.["X-OpenMontage-CSRF"] });
      return jsonRes({ status: "connected", available: true, headline: "Connected", token_configured: true });
    };
    const c = new BacklotClient({ projectId: "eb", fetchImpl });
    const res = await c.connectHermes({ url: "http://127.0.0.1:9235", token: "secret" });
    const call = seen.find((s) => s.url.endsWith("/api/hermes/connect"))!;
    expect(call.csrf).toBe("TOK");
    expect((call.body as { url: string }).url).toBe("http://127.0.0.1:9235");
    expect(res.available).toBe(true);
    // the server response never carries the raw token
    expect(JSON.stringify(res)).not.toContain("secret");
  });
});
