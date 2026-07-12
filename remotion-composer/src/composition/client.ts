// Typed client adapter for the Backlot runtime HTTP contract.
//
// - Read endpoints are plain GETs.
// - Mutations attach the CSRF token (`X-OpenMontage-CSRF`) exactly like the
//   existing vanilla UI, and are same-origin.
// - `forceFixtures` is the ONLY way sample/demo data ever appears: it is the
//   explicit "Try demo mode" switch. There is deliberately NO automatic fixture
//   fallback — a failed live fetch THROWS so the caller can preserve the last
//   known live state and show a "reconnecting" indicator, never silently replace
//   real state with fabricated data. Mutations never succeed against fixtures.
//
// This is a small ADAPTER over the Backlot HTTP contract. It does not implement
// any backend state machine; it only speaks the documented contract.

import { BackendTimelineDoc, BackendTimelinePayload } from "./adapter";
import { deterministicTimelinePayload } from "./fixtures";
import {
  BrainAssetsPayload,
  BrainEventsPage,
  BrainState,
  deterministicBrainState,
  PreferencesPayload,
} from "./brain";
import {
  ConnectionView,
  StatusView,
  deterministicStatusView,
} from "./status";

export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal;
  },
) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}>;

export interface BacklotClientOptions {
  projectId: string;
  baseUrl?: string; // default "" (same origin)
  fetchImpl?: FetchLike;
  forceFixtures?: boolean; // EXPLICIT demo mode — the only path that shows sample data
}

export interface SaveResult {
  ok: boolean;
  etag: string;
}
export interface RenderResult {
  ok: boolean;
  url?: string;
  measured_seconds?: number;
  frames_rendered?: number;
  total_frames?: number;
  truncated?: boolean;
  fps?: number;
  size_bytes?: number;
  reason?: string;
}

export class OfflineError extends Error {}

export class BacklotClient {
  readonly projectId: string;
  private readonly base: string;
  private readonly fetchImpl: FetchLike;
  readonly forceFixtures: boolean;
  private csrf: string | null = null;
  usedFixture = false;

  constructor(opts: BacklotClientOptions) {
    this.projectId = opts.projectId;
    this.base = opts.baseUrl ?? "";
    this.forceFixtures = opts.forceFixtures ?? false;
    const g = globalThis as unknown as { fetch?: FetchLike };
    this.fetchImpl = opts.fetchImpl ?? g.fetch?.bind(globalThis) ?? notConfigured;
  }

  private url(path: string): string {
    return `${this.base}${path}`;
  }

  private async getJSON<T>(path: string): Promise<T> {
    const res = await this.fetchImpl(this.url(path));
    if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
    return (await res.json()) as T;
  }

  private async getCsrf(): Promise<string> {
    if (this.csrf) return this.csrf;
    const data = await this.getJSON<{ csrf: string }>("/api/csrf");
    this.csrf = data.csrf;
    return this.csrf;
  }

  private async postJSON<T>(path: string, body: unknown): Promise<T> {
    const csrf = await this.getCsrf();
    const res = await this.fetchImpl(this.url(path), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-OpenMontage-CSRF": csrf },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = `${res.status}`;
      try {
        const j = (await res.json()) as { detail?: string };
        if (j?.detail) detail = j.detail;
      } catch {
        /* ignore */
      }
      const err = new Error(detail) as Error & { status?: number };
      err.status = res.status;
      throw err;
    }
    return (await res.json()) as T;
  }

  // ── Reads ──
  // Demo mode (forceFixtures) returns clearly-labelled sample data. Otherwise a
  // real GET is performed and a network failure THROWS — the caller preserves the
  // last known state and shows "reconnecting", never fabricated data.
  async getTimeline(): Promise<BackendTimelinePayload> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return deterministicTimelinePayload();
    }
    const p = await this.getJSON<BackendTimelinePayload>(
      `/api/project/${this.projectId}/timeline`,
    );
    this.usedFixture = false;
    return p;
  }

  // The canonical, reconciled command-center view (shared with the board).
  async getStatus(): Promise<StatusView> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return deterministicStatusView(this.projectId);
    }
    const v = await this.getJSON<StatusView>(
      `/api/project/${this.projectId}/status`,
    );
    this.usedFixture = false;
    return v;
  }

  async getState(): Promise<Record<string, unknown>> {
    return this.demoOrGet(`/api/project/${this.projectId}/state`, {});
  }
  async getRun(): Promise<Record<string, unknown>> {
    return this.demoOrGet(`/api/project/${this.projectId}/run`, { state: "not_started" });
  }
  async getAgentInbox(): Promise<Record<string, unknown>> {
    return this.demoOrGet(`/api/project/${this.projectId}/agent-inbox`, { items: [] });
  }

  // In demo mode return the neutral placeholder; otherwise a real GET that
  // THROWS on failure (no automatic fixture substitution).
  private async demoOrGet<T>(path: string, demo: T): Promise<T> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return demo;
    }
    return this.getJSON<T>(path);
  }

  // ── Mutations (never faked) ──
  async saveTimeline(doc: BackendTimelineDoc, ifMatch?: string): Promise<SaveResult> {
    this.assertOnline("save the timeline");
    return this.postJSON<SaveResult>(`/api/project/${this.projectId}/timeline`, {
      timeline: doc,
      if_match: ifMatch,
    });
  }

  async renderTimeline(): Promise<RenderResult> {
    this.assertOnline("render the timeline");
    return this.postJSON<RenderResult>(
      `/api/project/${this.projectId}/timeline/render`,
      {},
    );
  }

  async renderFrame(frame: number): Promise<RenderResult> {
    this.assertOnline("render a frame");
    return this.postJSON<RenderResult>(`/api/project/${this.projectId}/frame`, { frame });
  }

  async queueRevision(
    layerId: string,
    prompt: string,
    constraints?: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    this.assertOnline("queue a revision");
    // Backend contract (backlot/server.py::project_layer_revision) reads `layer_id`
    // and `prompt` (+ optional `constraints`).
    return this.postJSON(`/api/project/${this.projectId}/timeline/revision`, {
      layer_id: layerId,
      prompt,
      constraints,
    });
  }

  async setDuration(seconds: number, strategy?: string): Promise<Record<string, unknown>> {
    this.assertOnline("change duration");
    return this.postJSON(`/api/project/${this.projectId}/duration`, {
      duration: seconds,
      strategy,
    });
  }

  // ── Production brain (Hermes) ──
  // Demo mode → labelled fixture. Otherwise a real GET that THROWS on failure.
  async getBrain(): Promise<BrainState> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return deterministicBrainState(this.projectId);
    }
    const s = await this.getJSON<BrainState>(`/api/project/${this.projectId}/brain`);
    this.usedFixture = false;
    return s;
  }

  async getBrainEvents(after = 0, limit = 200): Promise<BrainEventsPage> {
    const empty: BrainEventsPage = {
      events: [],
      cursor: after,
      next_cursor: after,
      latest_seq: 0,
      count: 0,
      has_more: false,
    };
    return this.demoOrGet(
      `/api/project/${this.projectId}/brain/events?after=${after}&limit=${limit}`,
      empty,
    );
  }

  async getBrainAssets(): Promise<BrainAssetsPayload> {
    return this.demoOrGet(`/api/project/${this.projectId}/brain/assets`, {
      outputs: [],
      count: 0,
      run_id: null,
      actual_duration_seconds: null,
    });
  }

  // ── Hermes / Mochlet connection (guided, secure) ──
  async getHermesConnection(): Promise<ConnectionView> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return { status: "demo", available: false, headline: "Demo mode" };
    }
    return this.getJSON<ConnectionView>(`/api/hermes/connection`);
  }
  async connectHermes(body: { url?: string; token?: string }): Promise<ConnectionView> {
    this.assertOnline("connect Hermes");
    return this.postJSON<ConnectionView>(`/api/hermes/connect`, body);
  }

  // ── Coarse preflight/planning run control (the plan-approval gate) ──
  async approvePlan(runId: string): Promise<Record<string, unknown>> {
    this.assertOnline("approve the plan");
    return this.postJSON(`/api/project/${this.projectId}/run/approve`, { run_id: runId });
  }
  async cancelCoarseRun(runId: string): Promise<Record<string, unknown>> {
    this.assertOnline("stop the run");
    return this.postJSON(`/api/project/${this.projectId}/run/cancel`, { run_id: runId });
  }
  async previewRun(): Promise<Record<string, unknown>> {
    this.assertOnline("render a preview");
    return this.postJSON(`/api/project/${this.projectId}/run/preview`, {});
  }

  // ── Brain control (CSRF; never faked). `run_id`/`job_id` are sent verbatim. ──
  async startRun(): Promise<BrainState> {
    this.assertOnline("start a production run");
    return this.postJSON<BrainState>(`/api/project/${this.projectId}/brain/start`, {});
  }
  async approveRun(
    runId: string,
    opts: { approvalId?: string; stage?: string; note?: string } = {},
  ): Promise<BrainState> {
    this.assertOnline("approve");
    return this.postJSON<BrainState>(`/api/project/${this.projectId}/brain/approve`, {
      run_id: runId,
      approval_id: opts.approvalId,
      stage: opts.stage,
      note: opts.note,
    });
  }
  async rejectRun(
    runId: string,
    opts: { approvalId?: string; stage?: string; note?: string } = {},
  ): Promise<BrainState> {
    this.assertOnline("reject");
    return this.postJSON<BrainState>(`/api/project/${this.projectId}/brain/reject`, {
      run_id: runId,
      approval_id: opts.approvalId,
      stage: opts.stage,
      note: opts.note,
    });
  }
  async cancelRun(runId: string): Promise<BrainState> {
    this.assertOnline("cancel the run");
    return this.postJSON<BrainState>(`/api/project/${this.projectId}/brain/cancel`, {
      run_id: runId,
    });
  }
  async retryStage(stage: string, runId?: string | null, jobId?: string | null): Promise<BrainState> {
    this.assertOnline("retry a stage");
    return this.postJSON<BrainState>(`/api/project/${this.projectId}/brain/retry`, {
      stage,
      run_id: runId ?? undefined,
      job_id: jobId ?? undefined,
    });
  }
  async resumeRun(runId?: string | null, jobId?: string | null): Promise<BrainState> {
    this.assertOnline("resume the run");
    return this.postJSON<BrainState>(`/api/project/${this.projectId}/brain/resume`, {
      run_id: runId ?? undefined,
      job_id: jobId ?? undefined,
    });
  }

  // ── Preferences / learning ──
  async getPreferences(scope: "all" | "global" | "project" = "all", category?: string): Promise<PreferencesPayload> {
    const q = category ? `?scope=${scope}&category=${encodeURIComponent(category)}` : `?scope=${scope}`;
    return this.demoOrGet(`/api/project/${this.projectId}/preferences${q}`, {
      categories: [],
    });
  }
  async updatePreference(body: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.assertOnline("update a preference");
    return this.postJSON(`/api/project/${this.projectId}/preferences`, body);
  }
  async resetPreferences(scope: "global" | "project"): Promise<Record<string, unknown>> {
    this.assertOnline("reset preferences");
    return this.postJSON(`/api/project/${this.projectId}/preferences/reset`, { scope });
  }

  private assertOnline(action: string): void {
    if (this.forceFixtures) {
      throw new OfflineError(`Cannot ${action} in fixture/offline mode.`);
    }
  }
}

const notConfigured: FetchLike = () => {
  throw new Error("BacklotClient: no fetch implementation available");
};
