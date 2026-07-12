// Typed contract for the CANONICAL production-status view model
// (lib/production_status/presenter.py, served by /api/project/<id>/status).
//
// The board and the studio consume EXACTLY this shape, so the two surfaces can
// never disagree about where the production is or what to do next.

export type Owner = "hermes" | "user" | "system";

export type OverallState =
  | "not_started"
  | "planning"
  | "awaiting_plan_approval"
  | "ready_to_produce"
  | "producing"
  | "awaiting_approval"
  | "blocked"
  | "cancelling"
  | "cancelled"
  | "failed"
  | "completed"
  | "reconciling";

export type StatusMode = "live" | "fixture" | "demo" | "local" | "idle";

export type StepStatus =
  | "completed"
  | "current"
  | "blocked"
  | "awaiting"
  | "failed"
  | "skipped"
  | "upcoming";

export interface StatusStep {
  id: string;
  index: number;
  label: string;
  status: StepStatus;
  progress: number;
  detail?: string | null;
}

export interface StatusAction {
  id: string;
  label: string;
  owner: Owner;
  kind: string;
  advances_production: boolean;
  approval_id?: string | null;
  stage?: string | null;
  hint?: string | null;
}

export interface MochletProject {
  id: string;
  name?: string | null;
  path?: string | null;
}

export interface ConnectionView {
  status: string;
  available: boolean;
  endpoint?: string | null;
  endpoint_kind?: string;
  suggested_endpoint?: string;
  loopback?: boolean;
  token_configured?: boolean;
  server_name?: string | null;
  project?: string | null;
  projects?: MochletProject[];
  headline?: string;
  detail?: string;
  actions?: Array<{ id: string; label: string }>;
}

export interface StatusDiagnostic {
  kind: string;
  message: string;
  sources?: Record<string, unknown>;
}

export interface StatusRender {
  renderable: boolean;
  active: boolean;
  reason?: string | null;
  layer_count: number;
}

export interface StatusTarget {
  available: boolean;
  duration_seconds?: number | null;
  formatted?: string | null;
  frames?: number | null;
  fps: number;
  source?: string | null;
  is_target: boolean;
  label: string;
}

export interface StatusIdentity {
  agent?: string | null;
  job?: string | null;
  session?: string | null;
  engine?: string | null;
  tool?: string | null;
  provider?: string | null;
}

export interface StatusView {
  version: string;
  kind: "production_status_view";
  project_id?: string | null;
  mode: StatusMode;
  authoritative_source: string;
  overall_state: OverallState;
  current_stage?: string | null;
  current_stage_label?: string | null;
  stage_index?: number | null;
  stage_number?: number | null;
  stage_count: number;
  headline: string;
  active_task?: string | null;
  owner: Owner;
  why_waiting?: string | null;
  primary_action: StatusAction;
  secondary_actions: StatusAction[];
  latest_event?: { label?: string; ts?: string | number; seq?: number } | null;
  elapsed_seconds?: number | null;
  progress: number;
  completed_stages: number;
  stages: StatusStep[];
  identity: StatusIdentity;
  run_id?: string | null;
  stop_available: boolean;
  render: StatusRender;
  target: StatusTarget;
  connection: ConnectionView;
  diagnostics: StatusDiagnostic[];
  sources: {
    brain_state?: string | null;
    brain_run_id?: string | null;
    run_state?: string | null;
    plan_approved?: boolean;
    has_checkpoints?: boolean;
  };
  stale: boolean;
  is_demo: boolean;
  is_live: boolean;
  is_fixture: boolean;
}

const CANON_STAGES: Array<[string, string]> = [
  ["research", "Research"],
  ["proposal", "Proposal"],
  ["script", "Script"],
  ["scene_plan", "Scene planning"],
  ["assets", "Asset generation"],
  ["narration", "Narration & music"],
  ["edit", "Editing"],
  ["render", "Rendering"],
  ["review", "Validation & review"],
  ["approval", "Approval"],
  ["complete", "Completion"],
];

// A CLEARLY-LABELLED demo view — only ever shown when the user explicitly opts
// into demo mode, NEVER as an automatic fallback after a failed live fetch.
export function deterministicStatusView(projectId: string): StatusView {
  const stages: StatusStep[] = CANON_STAGES.map(([id, label], i) => ({
    id,
    index: i,
    label,
    status: i < 4 ? "completed" : i === 4 ? "current" : "upcoming",
    progress: i < 4 ? 1 : i === 4 ? 0.5 : 0,
  }));
  return {
    version: "1.0",
    kind: "production_status_view",
    project_id: projectId,
    mode: "demo",
    authoritative_source: "brain",
    overall_state: "producing",
    current_stage: "assets",
    current_stage_label: "Asset generation",
    stage_index: 4,
    stage_number: 5,
    stage_count: 11,
    headline: "Hermes is working on Asset generation",
    active_task: "Demo data — generating scene 2 of 5.",
    owner: "hermes",
    why_waiting: null,
    primary_action: {
      id: "monitor",
      label: "Hermes is producing your video",
      owner: "hermes",
      kind: "status",
      advances_production: false,
    },
    secondary_actions: [],
    latest_event: { label: "Demo: produced Scene 1 still.", seq: 12 },
    elapsed_seconds: 14,
    progress: 4 / 11,
    completed_stages: 4,
    stages,
    identity: { tool: "image_selector", provider: "demo", job: "demo-job" },
    run_id: "demo-run",
    stop_available: true,
    render: { renderable: false, active: false, reason: "Demo — no timeline layers yet.", layer_count: 0 },
    target: { available: true, duration_seconds: 150, formatted: "2:30", frames: 4500, fps: 30, source: "requested", is_target: true, label: "target 2:30 · 4500 target frames" },
    connection: { status: "demo", available: false, headline: "Demo mode" },
    diagnostics: [],
    sources: { brain_state: "running", brain_run_id: "demo-run" },
    stale: false,
    is_demo: true,
    is_live: false,
    is_fixture: true,
  };
}
