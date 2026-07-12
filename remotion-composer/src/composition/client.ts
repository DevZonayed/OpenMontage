// Typed client adapter for the Backlot runtime HTTP contract.
//
// - Read endpoints are plain GETs.
// - Mutations attach the CSRF token (`X-OpenMontage-CSRF`) exactly like the
//   existing vanilla UI, and are same-origin.
// - `fixtureFallback` makes the studio independently testable/demoable: when the
//   backend is unreachable (or `forceFixtures` is set), read calls resolve to
//   deterministic sample data instead of throwing. Mutations never silently
//   succeed against fixtures — they surface an explicit offline error.
//
// This is a small ADAPTER over Worker A's forthcoming/existing APIs. It does not
// implement any backend state machine; it only speaks the documented contract.

import { BackendTimelineDoc, BackendTimelinePayload } from "./adapter";
import { deterministicTimelinePayload } from "./fixtures";
import {
  BrainAssetsPayload,
  BrainEventsPage,
  BrainState,
  deterministicBrainState,
  PreferencesPayload,
} from "./brain";

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
  fixtureFallback?: boolean; // fall back to fixtures on network failure (read-only)
  forceFixtures?: boolean; // always use fixtures (offline demo / tests)
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
  private readonly fixtureFallback: boolean;
  private readonly forceFixtures: boolean;
  private csrf: string | null = null;
  usedFixture = false;

  constructor(opts: BacklotClientOptions) {
    this.projectId = opts.projectId;
    this.base = opts.baseUrl ?? "";
    this.fixtureFallback = opts.fixtureFallback ?? true;
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

  // ── Reads (fixture-tolerant) ──
  async getTimeline(): Promise<BackendTimelinePayload> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return deterministicTimelinePayload();
    }
    try {
      const p = await this.getJSON<BackendTimelinePayload>(
        `/api/project/${this.projectId}/timeline`,
      );
      this.usedFixture = false;
      return p;
    } catch (e) {
      if (this.fixtureFallback) {
        this.usedFixture = true;
        return deterministicTimelinePayload();
      }
      throw e;
    }
  }

  async getState(): Promise<Record<string, unknown>> {
    return this.tolerantGet(`/api/project/${this.projectId}/state`, {});
  }
  async getRun(): Promise<Record<string, unknown>> {
    return this.tolerantGet(`/api/project/${this.projectId}/run`, { state: "not_started" });
  }
  async getAgentInbox(): Promise<Record<string, unknown>> {
    return this.tolerantGet(`/api/project/${this.projectId}/agent-inbox`, { items: [] });
  }

  private async tolerantGet<T>(path: string, fixture: T): Promise<T> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return fixture;
    }
    try {
      return await this.getJSON<T>(path);
    } catch (e) {
      if (this.fixtureFallback) {
        this.usedFixture = true;
        return fixture;
      }
      throw e;
    }
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

  // ── Production brain (Hermes) — reads are fixture-tolerant ──
  async getBrain(): Promise<BrainState> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return deterministicBrainState(this.projectId);
    }
    try {
      const s = await this.getJSON<BrainState>(`/api/project/${this.projectId}/brain`);
      this.usedFixture = false;
      return s;
    } catch (e) {
      if (this.fixtureFallback) {
        this.usedFixture = true;
        return deterministicBrainState(this.projectId);
      }
      throw e;
    }
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
    return this.tolerantGet(
      `/api/project/${this.projectId}/brain/events?after=${after}&limit=${limit}`,
      empty,
    );
  }

  async getBrainAssets(): Promise<BrainAssetsPayload> {
    return this.tolerantGet(`/api/project/${this.projectId}/brain/assets`, {
      outputs: [],
      count: 0,
      run_id: null,
      actual_duration_seconds: null,
    });
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
    return this.tolerantGet(`/api/project/${this.projectId}/preferences${q}`, {
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
