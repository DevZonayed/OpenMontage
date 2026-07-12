// Typed contract for the merged Hermes production brain (lib/production_brain +
// backlot/brain_api.py). The studio's production panel consumes exactly these
// shapes — nothing is guessed; field names mirror schema.py / brain_api.py.

export const BRAIN_STAGE_IDS = [
  "research",
  "proposal",
  "script",
  "scene_plan",
  "assets",
  "narration",
  "edit",
  "render",
  "review",
  "approval",
  "complete",
] as const;
export type BrainStageId = (typeof BRAIN_STAGE_IDS)[number];

export type RunState =
  | "not_started"
  | "running"
  | "awaiting_approval"
  | "blocked"
  | "cancelling"
  | "cancelled"
  | "failed"
  | "completed";

export const TERMINAL_RUN_STATES: ReadonlySet<RunState> = new Set([
  "cancelled",
  "failed",
  "completed",
]);
export const ACTIVE_RUN_STATES: ReadonlySet<RunState> = new Set([
  "running",
  "awaiting_approval",
  "blocked",
  "cancelling",
]);

export type StageStatus =
  | "pending"
  | "active"
  | "blocked"
  | "awaiting_approval"
  | "done"
  | "failed"
  | "skipped";

export interface BrainStage {
  id: BrainStageId;
  title: string;
  status: StageStatus;
  progress: number;
  started_at: string | null;
  ended_at: string | null;
  elapsed_seconds: number | null;
  tool: string | null;
  provider: string | null;
  job_id: string | null;
  latest_event_seq: number | null;
  latest_activity: string | null;
  outputs: Array<{ kind: string; path?: string | null; label?: string | null }>;
  error: string | null;
}

export interface BrainApproval {
  approval_id: string;
  stage: string | null;
  status: "pending" | "approved" | "rejected";
  requested_at: string | null;
  decided_at: string | null;
  by: string | null;
  note: string | null;
  prompt: string | null;
}

export interface BrainBlocker {
  blocker_id: string;
  stage: string | null;
  kind: string;
  message: string;
  options: string[];
  created_at: string | null;
  resolved: boolean;
  resolved_at: string | null;
}

export interface BrainOutput {
  kind: string;
  path: string | null;
  label: string | null;
  stage: string | null;
  seq: number | null;
}

// Non-secret orchestrator identity (adapter.BrainIdentity.to_brain_block + provisioned handles).
export interface BrainIdentity {
  name?: string;
  adapter?: string;
  available?: boolean;
  agent_id?: string | null;
  session_id?: string | null;
  engine?: string | null;
  orchestration?: "external_job" | "fake_driver";
  external?: boolean;
  job_id?: string | null;
}

export interface BrainState {
  version: string;
  kind: "production_run_state";
  run_id: string | null;
  project_id: string;
  state: RunState;
  terminal: boolean;
  brain: BrainIdentity;
  requested_duration_seconds: number | null;
  actual_duration_seconds: number | null;
  current_stage: string | null;
  stages: BrainStage[];
  approvals: BrainApproval[];
  blockers: BrainBlocker[];
  outputs: BrainOutput[];
  error: string | null;
  activity: string;
  counts: { events: number; tool_calls: number; decisions: number; outputs: number };
  cursor: number;
  created_at: string | null;
  started_at: string | null;
  updated_at: string | null;
  ended_at: string | null;
}

export interface BrainEvent {
  seq: number;
  type: string;
  stage?: string | null;
  ts?: string | null;
  message?: string | null;
  tool?: string | null;
  provider?: string | null;
  job_id?: string | null;
  data?: Record<string, unknown> | null;
  redacted?: boolean;
}

export interface BrainEventsPage {
  events: BrainEvent[];
  cursor: number;
  next_cursor: number;
  latest_seq: number;
  count: number;
  has_more: boolean;
}

export interface BrainAssetsPayload {
  outputs: BrainOutput[];
  count: number;
  run_id: string | null;
  actual_duration_seconds: number | null;
}

// ── Preferences / learning (brain_api.read_preferences / update_preference) ────
export interface StylePreference {
  id?: string;
  pref_id?: string;
  category: string;
  key: string;
  value: unknown;
  confidence?: number;
  status?: "applied" | "rejected" | "deleted" | string;
  provenance?: {
    source?: string; // approval | correction | promotion
    verified?: boolean;
    run_id?: string | null;
    stage?: string | null;
    decision_ref?: string | null;
    from_pref?: string | null;
    note?: string | null;
  };
}

export interface PreferenceScopeBlock {
  opted_out: boolean;
  preferences: StylePreference[];
}

export interface PreferencesPayload {
  categories: string[];
  global?: PreferenceScopeBlock;
  project?: PreferenceScopeBlock;
}

// ── Derived helpers ────────────────────────────────────────────────────────────
export function orchestrationKind(s: BrainState | null): "external_job" | "fake_driver" | "unknown" {
  const o = s?.brain?.orchestration;
  return o === "external_job" || o === "fake_driver" ? o : "unknown";
}

/** LIVE only when a real external orchestrator job drives the run and we're not on a fixture. */
export function isLive(s: BrainState | null, usedFixture: boolean): boolean {
  return !usedFixture && orchestrationKind(s) === "external_job";
}

export function prefId(p: StylePreference): string {
  return String(p.pref_id ?? p.id ?? "");
}

// Deterministic, clearly-labelled fixture used only offline / unconfigured demo.
export function deterministicBrainState(projectId: string): BrainState {
  const stage = (id: BrainStageId, title: string, status: StageStatus, progress = 0): BrainStage => ({
    id,
    title,
    status,
    progress,
    started_at: null,
    ended_at: null,
    elapsed_seconds: null,
    tool: null,
    provider: null,
    job_id: null,
    latest_event_seq: null,
    latest_activity: null,
    outputs: [],
    error: null,
  });
  return {
    version: "1.0",
    kind: "production_run_state",
    run_id: null,
    project_id: projectId,
    state: "not_started",
    terminal: false,
    brain: { name: "hermes", adapter: "fake", available: true, orchestration: "fake_driver", engine: "fake" },
    requested_duration_seconds: null,
    actual_duration_seconds: null,
    current_stage: null,
    stages: [
      stage("research", "Research", "pending"),
      stage("proposal", "Proposal", "pending"),
      stage("script", "Script", "pending"),
      stage("scene_plan", "Scene planning", "pending"),
      stage("assets", "Asset generation", "pending"),
      stage("narration", "Narration & music", "pending"),
      stage("edit", "Editing", "pending"),
      stage("render", "Rendering", "pending"),
      stage("review", "Validation & review", "pending"),
      stage("approval", "Approval", "pending"),
      stage("complete", "Completion", "pending"),
    ],
    approvals: [],
    blockers: [],
    outputs: [],
    error: null,
    activity: "Deterministic fixture — no live production run (offline/unconfigured demo).",
    counts: { events: 0, tool_calls: 0, decisions: 0, outputs: 0 },
    cursor: 0,
    created_at: null,
    started_at: null,
    updated_at: null,
    ended_at: null,
  };
}
