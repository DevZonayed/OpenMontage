// Adapter between the backend `timeline.json` contract (Worker A owns it) and the
// rich canonical editing model. This is the seam that guarantees NO preview/render
// drift: the Remotion Player renders `renderProps(c)` and the pinned CLI renders
// the doc produced by `canonicalToBackendDoc(c)` — the SAME bytes for every field
// the renderer reads (id/type/start_frame/duration_frames/z/enabled/opacity/text/source).
//
// The UI never touches Python internals or the filesystem; it speaks only the
// documented HTTP contract through `BacklotClient`.

import {
  AUDIO_LAYER_TYPES,
  CanonicalComposition,
  CompositionAsset,
  createEmptyComposition,
  DEFAULT_FPS,
  DEFAULT_HEIGHT,
  DEFAULT_WIDTH,
  Fade,
  framesForDuration,
  Layer,
  LayerType,
  LAYER_TYPES,
  makeId,
  Track,
  trackKindForType,
  Transform,
  Transition,
} from "./model";

// ── Backend contract types (mirror lib/timeline.py + timeline_api.py) ──────────
// `validate_timeline` (Worker A) validates known fields but preserves unknown keys
// verbatim on save, so the richer edit fields below persist and reach the CLI render.
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
  volume?: number;
  sourceOffsetFrames?: number;
  transform?: Transform;
  fade?: Fade;
  transitionIn?: Transition;
  transitionOut?: Transition;
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
  let fps = intOr(doc.fps, DEFAULT_FPS);
  if (!(fps > 0)) fps = DEFAULT_FPS;
  const secs = clampSecs(intOr(doc.target_duration_seconds, 60));
  const c = createEmptyComposition({
    id: opts.id ?? "project",
    title: opts.title,
    pipeline: opts.pipeline,
    targetDurationSeconds: secs,
    fps,
    width: intOr(doc.width, DEFAULT_WIDTH),
    height: intOr(doc.height, DEFAULT_HEIGHT),
  });
  c.meta.targetFormatted = opts.targetFormatted;
  // Trust the backend's total_frames ONLY when it matches the frame invariant
  // (target_duration_seconds * fps); a stale/inconsistent value is recomputed so
  // the Player and CLI never disagree on duration.
  const expectedFrames = framesForDuration(secs, fps);
  const backendFrames = doc.total_frames;
  c.totalFrames =
    typeof backendFrames === "number" &&
    Number.isFinite(backendFrames) &&
    backendFrames === expectedFrames
      ? backendFrames
      : expectedFrames;

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
    const layer: Layer = {
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
    // Preserve richer edit fields across a reload so nothing is silently dropped.
    if (typeof bl.volume === "number") layer.volume = bl.volume;
    if (typeof bl.sourceOffsetFrames === "number") layer.sourceOffsetFrames = bl.sourceOffsetFrames;
    if (bl.transform) layer.transform = bl.transform;
    if (bl.fade) layer.fade = bl.fade;
    if (bl.transitionIn) layer.transitionIn = bl.transitionIn;
    if (bl.transitionOut) layer.transitionOut = bl.transitionOut;
    return layer;
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
  const mutedTracks = new Set(c.tracks.filter((t) => t.muted).map((t) => t.id));
  return {
    version: "1.0",
    fps: c.fps,
    target_duration_seconds: c.targetDurationSeconds,
    total_frames: c.totalFrames,
    width: c.width,
    height: c.height,
    layers: c.layers.map((l) => {
      const src = l.assetId ? assetById.get(l.assetId)?.url ?? l.source ?? null : l.source ?? null;
      const trackId = trackKindForType(l.type);
      const muted = mutedTracks.has(trackId);
      const isAudio = AUDIO_LAYER_TYPES.has(l.type);
      const bl: BackendLayer = {
        id: l.id,
        type: l.type,
        track: TRACK_INDEX[trackId] ?? 0,
        start_frame: l.startFrame,
        duration_frames: l.durationFrames,
        z: l.z,
        enabled: l.enabled,
        locked: l.locked,
        // A muted track silences its audio layers in preview AND render.
        opacity: l.opacity,
        source: src,
      };
      if (l.text !== undefined) bl.text = l.text;
      if (l.title !== undefined) bl.title = l.title;
      if (l.subtitle !== undefined) bl.subtitle = l.subtitle;
      // Serialize the richer edit fields so inspector edits actually affect the
      // Player preview, the pinned-CLI render, and persistence.
      if (isAudio) {
        const baseVol = typeof l.volume === "number" ? l.volume : 1;
        bl.volume = muted ? 0 : baseVol;
      } else if (typeof l.volume === "number") {
        bl.volume = l.volume;
      }
      if (l.sourceOffsetFrames !== undefined) bl.sourceOffsetFrames = l.sourceOffsetFrames;
      if (l.transform) bl.transform = l.transform;
      if (l.fade) bl.fade = l.fade;
      if (l.transitionIn) bl.transitionIn = l.transitionIn;
      if (l.transitionOut) bl.transitionOut = l.transitionOut;
      return bl;
    }),
  };
}

// Props the composition receives — IDENTICAL for Player preview and CLI render.
export interface RenderProps {
  timeline: BackendTimelineDoc;
  meta?: {
    title?: string;
    pipeline?: string;
    targetFormatted?: string;
    // Media-resolution parity: the SAME base+projectId the CLI is given, so a
    // project-local `source` resolves to the identical loadable URL in both.
    projectId?: string;
    assetBaseUrl?: string;
  };
}

export function renderProps(
  c: CanonicalComposition,
  opts: { assetBaseUrl?: string; projectId?: string } = {},
): RenderProps {
  return {
    timeline: canonicalToBackendDoc(c),
    meta: {
      title: c.meta.title,
      pipeline: c.meta.pipeline,
      targetFormatted: c.meta.targetFormatted,
      projectId: opts.projectId ?? c.id,
      assetBaseUrl: opts.assetBaseUrl,
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
