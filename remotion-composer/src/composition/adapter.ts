// Adapter between the backend `timeline.json` contract (Worker A owns it) and the
// rich canonical editing model. This is the seam that guarantees NO preview/render
// drift: the Remotion Player renders `renderProps(c)` and the pinned CLI renders
// the doc produced by `canonicalToBackendDoc(c)` — the SAME bytes for every field
// the renderer reads (id/type/start_frame/duration_frames/z/enabled/opacity/text/source).
//
// The UI never touches Python internals or the filesystem; it speaks only the
// documented HTTP contract through `BacklotClient`.

import {
  CanonicalComposition,
  CompositionAsset,
  createEmptyComposition,
  DEFAULT_FPS,
  DEFAULT_HEIGHT,
  DEFAULT_WIDTH,
  framesForDuration,
  Layer,
  LayerType,
  LAYER_TYPES,
  makeId,
  Track,
  trackKindForType,
} from "./model";

// ── Backend contract types (mirror lib/timeline.py + timeline_api.py) ──────────
export interface BackendLayer {
  id: string;
  type: string;
  track?: number;
  start_frame: number;
  duration_frames: number;
  z?: number;
  enabled?: boolean;
  locked?: boolean;
  opacity?: number;
  text?: string;
  title?: string;
  subtitle?: string;
  source?: string | null;
  provenance?: Record<string, unknown>;
}

export interface BackendTimelineDoc {
  version?: string;
  fps: number;
  target_duration_seconds: number;
  total_frames: number;
  width?: number;
  height?: number;
  layers: BackendLayer[];
}

export interface BackendTimelinePayload {
  timeline: BackendTimelineDoc;
  etag: string;
  persisted: boolean;
  fps: number;
  total_frames: number;
  target_duration_seconds: number;
  target_formatted: string;
  word_budget: number;
  measured_output_seconds: number | null;
  remotion_render_ready: boolean;
  remotion_reason: string;
  layer_types: string[];
}

const LAYER_TYPE_SET = new Set<string>(LAYER_TYPES as readonly string[]);
function coerceType(t: string): LayerType {
  return (LAYER_TYPE_SET.has(t) ? t : "shape") as LayerType;
}

// ── backend → canonical ────────────────────────────────────────────────────────
export function backendToCanonical(
  doc: BackendTimelineDoc,
  opts: { id?: string; title?: string; pipeline?: string; targetFormatted?: string } = {},
): CanonicalComposition {
  const fps = intOr(doc.fps, DEFAULT_FPS);
  const secs = intOr(doc.target_duration_seconds, 60);
  const c = createEmptyComposition({
    id: opts.id ?? "project",
    title: opts.title,
    pipeline: opts.pipeline,
    targetDurationSeconds: clampSecs(secs),
    fps,
    width: intOr(doc.width, DEFAULT_WIDTH),
    height: intOr(doc.height, DEFAULT_HEIGHT),
  });
  c.meta.targetFormatted = opts.targetFormatted;
  // Trust the backend's total_frames if consistent; otherwise recompute.
  c.totalFrames = intOr(doc.total_frames, framesForDuration(clampSecs(secs), fps));

  const assets: CompositionAsset[] = [];
  const layers: Layer[] = (doc.layers ?? []).map((bl, i) => {
    const type = coerceType(bl.type);
    let assetId: string | null = null;
    if (bl.source) {
      assetId = makeId(`${bl.id}a`, i);
      assets.push({
        id: assetId,
        kind: assetKindForType(type),
        url: bl.source,
        status: "ready",
        approved: false,
        version: 1,
        provenance: bl.provenance
          ? {
              provider: str(bl.provenance["provider"]),
              tool: str(bl.provenance["origin"]),
              jobId: str(bl.provenance["job_id"]),
            }
          : undefined,
      });
    }
    return {
      id: bl.id,
      type,
      trackId: trackKindForType(type),
      assetId,
      startFrame: intOr(bl.start_frame, 0),
      durationFrames: Math.max(1, intOr(bl.duration_frames, 1)),
      z: intOr(bl.z, i),
      enabled: bl.enabled !== false,
      locked: bl.locked === true,
      opacity: numOr(bl.opacity, 1),
      text: bl.text,
      title: bl.title,
      subtitle: bl.subtitle,
      source: bl.source ?? null,
    };
  });
  c.layers = layers;
  c.assets = assets;
  c.tracks = tracksFromLayers(layers);
  return c;
}

export function backendPayloadToCanonical(
  p: BackendTimelinePayload,
  opts: { id?: string; title?: string; pipeline?: string } = {},
): CanonicalComposition {
  return backendToCanonical(p.timeline, { ...opts, targetFormatted: p.target_formatted });
}

// ── canonical → backend ─────────────────────────────────────────────────────────
const TRACK_INDEX: Record<string, number> = { visual: 0, text: 1, audio: 2 };

export function canonicalToBackendDoc(c: CanonicalComposition): BackendTimelineDoc {
  const assetById = new Map(c.assets.map((a) => [a.id, a]));
  return {
    version: "1.0",
    fps: c.fps,
    target_duration_seconds: c.targetDurationSeconds,
    total_frames: c.totalFrames,
    width: c.width,
    height: c.height,
    layers: c.layers.map((l) => {
      const src = l.assetId ? assetById.get(l.assetId)?.url ?? l.source ?? null : l.source ?? null;
      const bl: BackendLayer = {
        id: l.id,
        type: l.type,
        track: TRACK_INDEX[trackKindForType(l.type)] ?? 0,
        start_frame: l.startFrame,
        duration_frames: l.durationFrames,
        z: l.z,
        enabled: l.enabled,
        locked: l.locked,
        opacity: l.opacity,
        source: src,
      };
      if (l.text !== undefined) bl.text = l.text;
      if (l.title !== undefined) bl.title = l.title;
      if (l.subtitle !== undefined) bl.subtitle = l.subtitle;
      return bl;
    }),
  };
}

// Props the composition receives — IDENTICAL for Player preview and CLI render.
export interface RenderProps {
  timeline: BackendTimelineDoc;
  meta?: { title?: string; pipeline?: string; targetFormatted?: string };
}

export function renderProps(c: CanonicalComposition): RenderProps {
  return {
    timeline: canonicalToBackendDoc(c),
    meta: {
      title: c.meta.title,
      pipeline: c.meta.pipeline,
      targetFormatted: c.meta.targetFormatted,
    },
  };
}

// ── helpers ──────────────────────────────────────────────────────────────────
function assetKindForType(t: LayerType): CompositionAsset["kind"] {
  if (t === "video") return "video";
  if (t === "image") return "image";
  if (t === "narration" || t === "music" || t === "sfx") return "audio";
  return "generated";
}

function tracksFromLayers(layers: Layer[]): Track[] {
  const base: Track[] = [
    { id: "visual", kind: "visual", label: "Visual", index: 0, muted: false },
    { id: "text", kind: "text", label: "Text / Captions", index: 1, muted: false },
    { id: "audio", kind: "audio", label: "Audio", index: 2, muted: false },
  ];
  return base.filter(
    (t) => t.kind === "visual" || layers.some((l) => trackKindForType(l.type) === t.kind),
  );
}

function intOr(v: unknown, d: number): number {
  return typeof v === "number" && Number.isFinite(v) ? Math.round(v) : d;
}
function numOr(v: unknown, d: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : d;
}
function clampSecs(s: number): number {
  return Math.min(300, Math.max(1, Math.round(s)));
}
function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}
