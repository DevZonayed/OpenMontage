// Typed contract for the read-only PROJECT OVERVIEW
// (lib/production_status/presenter.py::build_status_view, served by
// GET /api/project/<id>/status).
//
// This is a plain-language, on-disk summary the Board renders and the Studio
// header reads for a truthful duration + guidance line. It is NOT an automation
// controller: there is no run state machine, no agent connection, and no
// start/stop/approve surface. The Studio is a MANUAL-FIRST editor.

export type MilestoneStatus = "done" | "in_progress" | "needs_review" | "failed";

export interface OverviewMilestone {
  id: string;
  label: string;
  status: MilestoneStatus;
  ts?: string | null;
}

export interface OverviewTarget {
  available: boolean;
  duration_seconds?: number | null;
  formatted: string | null;
  frames: number | null;
  fps: number;
  source: string;
  is_target: boolean;
  label: string;
}

export interface OverviewRender {
  renderable: boolean;
  active: boolean;
  reason: string | null;
  layer_count: number;
}

export interface OverviewOutputs {
  renders: { path: string; label: string }[];
  render_count: number;
  latest_render: unknown;
  asset_count: number;
}

// The canonical read-only overview. Matches GET /api/project/<id>/status.
export interface ProjectOverview {
  version: string;
  kind: "project_overview";
  project_id: string;
  title: string | null;
  owner: "you";
  mode: string;
  headline: string;
  guidance: string;
  has_timeline: boolean;
  layer_count: number;
  milestones: OverviewMilestone[];
  milestone_progress: { completed: number; total: number };
  last_saved: { label: string; ts?: string | null } | null;
  blockers: { message: string; stage?: string | null }[];
  outputs: OverviewOutputs;
  target: OverviewTarget;
  render: OverviewRender;
  primary_action: { id: "open_studio"; label: string };
  diagnostics: unknown[];
  stale: boolean;
  is_demo: boolean;
  is_fixture: boolean;
}

// A CLEARLY-LABELLED demo overview — only ever shown when the user explicitly
// opts into demo mode, NEVER as an automatic fallback after a failed live fetch.
export function deterministicOverview(projectId: string): ProjectOverview {
  return {
    version: "2.0",
    kind: "project_overview",
    project_id: projectId,
    title: "Demo project",
    owner: "you",
    mode: "demo",
    headline: "Set up your first scene",
    guidance:
      "This is demo data. Open the Studio and add your first scene to start editing.",
    has_timeline: false,
    layer_count: 0,
    milestones: [],
    milestone_progress: { completed: 0, total: 0 },
    last_saved: null,
    blockers: [],
    outputs: { renders: [], render_count: 0, latest_render: null, asset_count: 0 },
    target: {
      available: false,
      duration_seconds: null,
      formatted: null,
      frames: null,
      fps: 30,
      source: "pending",
      is_target: true,
      label: "Duration set after first scene",
    },
    render: {
      renderable: false,
      active: false,
      reason: "Add scenes to the timeline in the Studio to enable rendering.",
      layer_count: 0,
    },
    primary_action: { id: "open_studio", label: "Open Production Studio" },
    diagnostics: [],
    stale: false,
    is_demo: true,
    is_fixture: true,
  };
}
