// Canonical, versioned composition/edit model for OpenMontage.
//
// This is the SINGLE source of truth the visual editor mutates and the Remotion
// Player previews. It is a strict superset of the backend `timeline.json`
// contract (see `adapter.ts`) so that the same edit that the editor shows is the
// exact composition the pinned Remotion CLI renders — no preview/render drift.
//
// Frame math is deliberately identical to `lib/duration.py::frames_for`
// (`seconds * fps`, seconds an integer in [1, 300]) so client and server agree
// to the frame: 60s@30 = 1800, 150s@30 = 4500, 300s@30 = 9000.

export const MODEL_VERSION = 2 as const;

export const MIN_DURATION_SECONDS = 1;
export const MAX_DURATION_SECONDS = 300; // 5:00 product contract
export const DEFAULT_FPS = 30;
export const DEFAULT_WIDTH = 1920;
export const DEFAULT_HEIGHT = 1080;

// Mirrors backend LAYER_TYPES exactly so the adapter round-trips losslessly.
export const LAYER_TYPES = [
  "video",
  "image",
  "text",
  "shape",
  "caption",
  "narration",
  "music",
  "sfx",
] as const;
export type LayerType = (typeof LAYER_TYPES)[number];

export const AUDIO_LAYER_TYPES: ReadonlySet<LayerType> = new Set([
  "narration",
  "music",
  "sfx",
]);
export const VISUAL_LAYER_TYPES: ReadonlySet<LayerType> = new Set([
  "video",
  "image",
  "shape",
]);
export const TEXT_LAYER_TYPES: ReadonlySet<LayerType> = new Set([
  "text",
  "caption",
]);

export type TrackKind = "visual" | "text" | "audio";

export function trackKindForType(t: LayerType): TrackKind {
  if (AUDIO_LAYER_TYPES.has(t)) return "audio";
  if (TEXT_LAYER_TYPES.has(t)) return "text";
  return "visual";
}

// ── Assets ────────────────────────────────────────────────────────────────────
export type AssetKind = "video" | "image" | "audio" | "generated";
// Lifecycle of a generated/production asset. `placeholder` renders a designed
// stand-in; `ready`/`approved` are render-eligible; `failed` surfaces a blocker.
export type AssetStatus =
  | "placeholder"
  | "generating"
  | "ready"
  | "approved"
  | "failed";

export interface AssetProvenance {
  provider?: string;
  model?: string;
  tool?: string;
  seed?: number | null;
  prompt?: string;
  createdAt?: string; // ISO-8601, injected by caller (never Date.now here)
  jobId?: string;
}

export interface CompositionAsset {
  id: string; // stable across regeneration
  kind: AssetKind;
  url: string | null; // null while a placeholder / generating
  status: AssetStatus;
  approved: boolean;
  version: number; // increments on each accepted regeneration
  provenance?: AssetProvenance;
  durationFrames?: number | null;
}

// ── Transforms / timing / transitions ──────────────────────────────────────────
export interface Crop {
  top: number;
  right: number;
  bottom: number;
  left: number;
}
export interface Transform {
  x?: number; // px offset
  y?: number;
  scale?: number; // 1 = 100%
  rotation?: number; // degrees
  opacity?: number; // 0..1
  crop?: Crop; // fractional 0..1 insets
}

export interface Fade {
  inFrames?: number;
  outFrames?: number;
}

export type TransitionKind = "none" | "fade" | "slide" | "wipe" | "zoom";
export interface Transition {
  kind: TransitionKind;
  durationFrames: number;
}

// ── Layers (each maps 1:1 to a Remotion <Sequence>) ────────────────────────────
export interface Layer {
  id: string; // stable id: ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$
  type: LayerType;
  trackId: string;
  sceneId?: string | null;
  assetId?: string | null;
  startFrame: number;
  durationFrames: number;
  z: number;
  enabled: boolean;
  locked: boolean;
  opacity: number; // 0..1 (base opacity, distinct from transform.opacity)
  text?: string; // caption/text content (never editorial rationale)
  title?: string;
  subtitle?: string;
  transform?: Transform;
  volume?: number; // 0..1 for audio layers
  fade?: Fade;
  transitionIn?: Transition;
  transitionOut?: Transition;
  // In-point into the underlying media asset (frames), used after a split/trim.
  sourceOffsetFrames?: number;
  // Free-form source path for backend round-trip (project-local relative path).
  source?: string | null;
}

export interface Track {
  id: string;
  kind: TrackKind;
  label: string;
  index: number;
  muted: boolean;
}

export interface Scene {
  id: string;
  name: string;
  startFrame: number;
  durationFrames: number;
}

export type ApprovalState = "draft" | "approved" | "changes_requested";

export interface CanonicalComposition {
  version: number;
  id: string;
  meta: {
    title?: string;
    pipeline?: string;
    targetFormatted?: string;
  };
  fps: number;
  width: number;
  height: number;
  targetDurationSeconds: number;
  totalFrames: number;
  scenes: Scene[];
  tracks: Track[];
  layers: Layer[];
  assets: CompositionAsset[];
  approval: ApprovalState;
}

// ── Frame math (parity with lib/duration.py) ───────────────────────────────────

export function isIntegerInRange(v: unknown, lo: number, hi: number): boolean {
  return (
    typeof v === "number" &&
    Number.isFinite(v) &&
    Number.isInteger(v) &&
    v >= lo &&
    v <= hi
  );
}

/**
 * Exact integer frame count for a validated target duration.
 * Throws on invalid input, mirroring `frames_for` on the server.
 */
export function framesForDuration(seconds: number, fps: number = DEFAULT_FPS): number {
  if (!isIntegerInRange(seconds, MIN_DURATION_SECONDS, MAX_DURATION_SECONDS)) {
    throw new RangeError(
      `target_duration_seconds must be an integer in [${MIN_DURATION_SECONDS}, ${MAX_DURATION_SECONDS}]`,
    );
  }
  if (!Number.isInteger(fps) || fps <= 0) {
    throw new RangeError("fps must be a positive integer");
  }
  return seconds * fps;
}

// ── Id helpers ─────────────────────────────────────────────────────────────────
export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export function isValidId(id: unknown): id is string {
  return typeof id === "string" && ID_RE.test(id);
}

/** Deterministic id from a prefix + numeric seed (no Math.random for repro). */
export function makeId(prefix: string, seed: number): string {
  const clean = prefix.replace(/[^A-Za-z0-9]/g, "").slice(0, 24) || "id";
  return `${clean}_${Math.trunc(Math.abs(seed))}`;
}

// ── Constructors ───────────────────────────────────────────────────────────────
export function defaultTracks(): Track[] {
  return [
    { id: "visual", kind: "visual", label: "Visual", index: 0, muted: false },
    { id: "text", kind: "text", label: "Text / Captions", index: 1, muted: false },
    { id: "audio", kind: "audio", label: "Audio", index: 2, muted: false },
  ];
}

export function createEmptyComposition(opts: {
  id: string;
  title?: string;
  pipeline?: string;
  targetDurationSeconds?: number;
  fps?: number;
  width?: number;
  height?: number;
}): CanonicalComposition {
  const fps = opts.fps ?? DEFAULT_FPS;
  const secs = opts.targetDurationSeconds ?? 60;
  return {
    version: MODEL_VERSION,
    id: opts.id,
    meta: { title: opts.title, pipeline: opts.pipeline },
    fps,
    width: opts.width ?? DEFAULT_WIDTH,
    height: opts.height ?? DEFAULT_HEIGHT,
    targetDurationSeconds: secs,
    totalFrames: framesForDuration(secs, fps),
    scenes: [],
    tracks: defaultTracks(),
    layers: [],
    assets: [],
    approval: "draft",
  };
}

/** Deep clone (structuredClone where available; JSON fallback for old runtimes). */
export function cloneComposition(c: CanonicalComposition): CanonicalComposition {
  if (typeof structuredClone === "function") return structuredClone(c);
  return JSON.parse(JSON.stringify(c)) as CanonicalComposition;
}
