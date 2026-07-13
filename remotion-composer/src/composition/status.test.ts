import { describe, it, expect } from "vitest";
import { BacklotClient, FetchLike } from "./client";
import { deterministicStatusView } from "./status";

function jsonRes(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) };
}

describe("canonical status view", () => {
  it("demo fixture is clearly labelled, never live, and carries a NATIVE agent connection", () => {
    const v = deterministicStatusView("demo");
    expect(v.is_demo).toBe(true);
    expect(v.is_live).toBe(false);
    expect(v.stage_count).toBe(11);
    expect(v.stages.length).toBe(11);
    expect(v.stages.filter((s) => s.status === "current").length).toBe(1);
    // native Hermes Agent connection — no endpoint/token/project fields exist on it
    expect(v.connection.kind).toBe("hermes_agent");
    expect(v.connection.server_name).toBe("Hermes Agent");
    expect(v.connection).not.toHaveProperty("endpoint");
    expect(v.connection).not.toHaveProperty("token_configured");
    expect(v.connection).not.toHaveProperty("projects");
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

  it("getAgentConnection reads the native /api/agent/connection endpoint (no credentials)", async () => {
    const calls: string[] = [];
    const fetchImpl: FetchLike = async (url) => {
      calls.push(url);
      return jsonRes({
        kind: "hermes_agent", status: "detected", available: false, server_name: "Hermes Agent",
        headline: "Hermes Agent detected", detail: "Connect to start.", actions: [{ id: "connect_agent", label: "Connect Hermes Agent" }],
        enabled: false, installed: true, ready: true, version: "1.4.0",
      });
    };
    const c = new BacklotClient({ projectId: "eb", fetchImpl });
    const conn = await c.getAgentConnection();
    expect(calls[0]).toBe("/api/agent/connection");
    expect(conn.kind).toBe("hermes_agent");
    expect(conn.status).toBe("detected");
    expect(conn.version).toBe("1.4.0");
  });

  it("connectAgent POSTs an EMPTY body to /api/agent/connect with CSRF (no url/token/project)", async () => {
    const seen: Array<{ url: string; body: unknown; csrf?: string }> = [];
    const fetchImpl: FetchLike = async (url, init) => {
      if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "TOK" });
      seen.push({ url, body: JSON.parse(init!.body!), csrf: init?.headers?.["X-OpenMontage-CSRF"] });
      return jsonRes({ kind: "hermes_agent", status: "connected", available: true, server_name: "Hermes Agent", headline: "Connected", detail: "", actions: [], enabled: true, installed: true, ready: true, version: "1.4.0" });
    };
    const c = new BacklotClient({ projectId: "eb", fetchImpl });
    const res = await c.connectAgent();
    const call = seen.find((s) => s.url.endsWith("/api/agent/connect"))!;
    expect(call.csrf).toBe("TOK");
    expect(call.body).toEqual({});                 // empty body — no endpoint/token/project
    expect(res.available).toBe(true);
    expect(res.status).toBe("connected");
  });

  it("disconnectAgent POSTs an empty body to /api/agent/disconnect with CSRF", async () => {
    const seen: Array<{ url: string; body: unknown; csrf?: string }> = [];
    const fetchImpl: FetchLike = async (url, init) => {
      if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "TOK" });
      seen.push({ url, body: JSON.parse(init!.body!), csrf: init?.headers?.["X-OpenMontage-CSRF"] });
      return jsonRes({ kind: "hermes_agent", status: "detected", available: false, server_name: "Hermes Agent", headline: "Disconnected", detail: "", actions: [], enabled: false, installed: true, ready: true, version: "1.4.0" });
    };
    const c = new BacklotClient({ projectId: "eb", fetchImpl });
    const res = await c.disconnectAgent();
    const call = seen.find((s) => s.url.endsWith("/api/agent/disconnect"))!;
    expect(call.csrf).toBe("TOK");
    expect(call.body).toEqual({});
    expect(res.available).toBe(false);
  });
});
