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
// This is a small ADAPTER over the Backlot HTTP contract. The Studio is a
// MANUAL-FIRST editor: this client speaks only the timeline/render/overview/
// preferences contract. There is NO agent connection and NO run/brain
// automation — those endpoints were removed with the Hermes/agent concept.

import { BackendTimelineDoc, BackendTimelinePayload } from "./adapter";
import { deterministicTimelinePayload } from "./fixtures";
import { PreferencesPayload } from "./preferences";
import { ProjectOverview, deterministicOverview } from "./status";

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

  // The read-only project overview (shared with the board): a truthful duration,
  // a guidance line, milestones and outputs. Never an automation controller.
  async getStatus(): Promise<ProjectOverview> {
    if (this.forceFixtures) {
      this.usedFixture = true;
      return deterministicOverview(this.projectId);
    }
    const v = await this.getJSON<ProjectOverview>(
      `/api/project/${this.projectId}/status`,
    );
    this.usedFixture = false;
    return v;
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

  // ── Preferences / learning (Style panel) ──
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
