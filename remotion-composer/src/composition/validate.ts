// Validation for the canonical composition model.
//
// Two severities:
//   - errors  → the composition is malformed/unsafe and must not be saved/rendered
//   - warnings → renderable but not render-ready (e.g. a placeholder asset)
// `renderReady` is only true when there are no errors AND every enabled layer that
// references an asset resolves to a real, ready/approved, safely-URL'd asset.

import {
  AUDIO_LAYER_TYPES,
  CanonicalComposition,
  framesForDuration,
  isIntegerInRange,
  isValidId,
  Layer,
  LAYER_TYPES,
  MAX_DURATION_SECONDS,
  MIN_DURATION_SECONDS,
} from "./model";

export interface ValidationIssue {
  code: string;
  message: string;
  layerId?: string;
  assetId?: string;
}

export interface ValidationResult {
  ok: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  renderReady: boolean;
}

const LAYER_TYPE_SET = new Set<string>(LAYER_TYPES as readonly string[]);

// Schemes we consider safe to reference from a composition asset.
const SAFE_SCHEMES = new Set(["http:", "https:", "blob:"]);
// Explicitly dangerous schemes even if a parser accepts them.
const UNSAFE_SCHEME_RE = /^(javascript|vbscript|data|file):/i;

/**
 * Is this a safe asset URL? Accepts:
 *   - null (no asset yet)
 *   - http(s) / blob URLs
 *   - same-origin absolute paths (/media/..., /thumb/...)
 *   - project-local relative paths with no `..` traversal
 * Rejects javascript:/vbscript:/data:/file:, backslashes, control chars, and `..`.
 */
export function isSafeAssetUrl(url: string | null | undefined): boolean {
  if (url === null || url === undefined) return true;
  if (typeof url !== "string") return false;
  const s = url.trim();
  if (s === "") return false;
  // Control characters (incl. newlines) are never legitimate in a URL/path.
  if (/[\u0000-\u001f\u007f]/.test(s)) return false;
  if (s.includes("\\")) return false;
  if (UNSAFE_SCHEME_RE.test(s)) return false;
  // Absolute scheme URL: must be an allowed scheme and parseable.
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) {
    try {
      const u = new URL(s);
      return SAFE_SCHEMES.has(u.protocol);
    } catch {
      return false;
    }
  }
  // Path (absolute same-origin or relative). Reject traversal.
  const parts = s.split("/");
  if (parts.some((p) => p === "..")) return false;
  return true;
}

function num(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function inUnit(v: unknown): boolean {
  return num(v) && v >= 0 && v <= 1;
}

export function validateComposition(
  c: CanonicalComposition,
): ValidationResult {
  const errors: ValidationIssue[] = [];
  const warnings: ValidationIssue[] = [];
  const err = (code: string, message: string, extra: Partial<ValidationIssue> = {}) =>
    errors.push({ code, message, ...extra });
  const warn = (code: string, message: string, extra: Partial<ValidationIssue> = {}) =>
    warnings.push({ code, message, ...extra });

  // ── Composition-level invariants ──
  if (!Number.isInteger(c.fps) || c.fps <= 0) {
    err("fps", "fps must be a positive integer");
  }
  if (!Number.isInteger(c.width) || c.width <= 0) err("width", "width must be a positive integer");
  if (!Number.isInteger(c.height) || c.height <= 0) err("height", "height must be a positive integer");

  if (!isIntegerInRange(c.targetDurationSeconds, MIN_DURATION_SECONDS, MAX_DURATION_SECONDS)) {
    err(
      "target_duration",
      `target_duration_seconds must be an integer in [${MIN_DURATION_SECONDS}, ${MAX_DURATION_SECONDS}]`,
    );
  } else if (Number.isInteger(c.fps) && c.fps > 0) {
    const expected = framesForDuration(c.targetDurationSeconds, c.fps);
    if (c.totalFrames !== expected) {
      err(
        "total_frames",
        `total_frames (${c.totalFrames}) must equal target_duration_seconds*fps (${expected})`,
      );
    }
  }

  // ── Assets ──
  const assetById = new Map<string, (typeof c.assets)[number]>();
  for (const a of c.assets) {
    if (!isValidId(a.id)) {
      err("asset_id", `invalid asset id: ${JSON.stringify(a.id)}`, { assetId: String(a.id) });
      continue;
    }
    if (assetById.has(a.id)) {
      err("asset_dup", `duplicate asset id: ${a.id}`, { assetId: a.id });
      continue;
    }
    assetById.set(a.id, a);
    if (!isSafeAssetUrl(a.url)) {
      err("asset_url", `unsafe or malformed asset url on ${a.id}`, { assetId: a.id });
    }
    if ((a.status === "ready" || a.status === "approved") && !a.url) {
      warn("asset_no_url", `asset ${a.id} is ${a.status} but has no url`, { assetId: a.id });
    }
    if (a.status === "failed") {
      warn("asset_failed", `asset ${a.id} failed generation`, { assetId: a.id });
    }
  }

  // ── Layers ──
  const seen = new Set<string>();
  const total = c.totalFrames;
  for (const l of c.layers) {
    if (!isValidId(l.id)) {
      err("layer_id", `invalid layer id: ${JSON.stringify(l.id)}`);
      continue;
    }
    if (seen.has(l.id)) {
      err("layer_dup", `duplicate layer id: ${l.id}`, { layerId: l.id });
      continue;
    }
    seen.add(l.id);

    if (!LAYER_TYPE_SET.has(l.type)) {
      err("layer_type", `unknown layer type "${l.type}"`, { layerId: l.id });
    }
    if (!Number.isInteger(l.startFrame) || l.startFrame < 0) {
      err("layer_start", `startFrame must be an integer >= 0`, { layerId: l.id });
    }
    if (!Number.isInteger(l.durationFrames) || l.durationFrames < 1) {
      err("layer_dur", `durationFrames must be an integer >= 1`, { layerId: l.id });
    }
    if (!inUnit(l.opacity)) err("layer_opacity", `opacity must be in [0,1]`, { layerId: l.id });
    if (l.volume !== undefined && !inUnit(l.volume)) {
      err("layer_volume", `volume must be in [0,1]`, { layerId: l.id });
    }
    if (l.transform?.scale !== undefined && (!num(l.transform.scale) || l.transform.scale < 0)) {
      err("layer_scale", `transform.scale must be >= 0`, { layerId: l.id });
    }
    if (l.transform?.opacity !== undefined && !inUnit(l.transform.opacity)) {
      err("layer_txopacity", `transform.opacity must be in [0,1]`, { layerId: l.id });
    }
    if (l.source !== undefined && l.source !== null && !isSafeAssetUrl(l.source)) {
      err("layer_source", `unsafe or malformed layer source`, { layerId: l.id });
    }

    // Timing beyond total is a warning (render clamps), not a hard error.
    if (
      Number.isInteger(l.startFrame) &&
      Number.isInteger(l.durationFrames) &&
      Number.isInteger(total) &&
      l.startFrame + l.durationFrames > total
    ) {
      warn("layer_overflow", `layer ${l.id} extends past totalFrames (${total})`, { layerId: l.id });
    }

    // Dangling asset reference is an error.
    if (l.assetId) {
      if (!assetById.has(l.assetId)) {
        err("layer_asset_missing", `layer ${l.id} references unknown asset ${l.assetId}`, {
          layerId: l.id,
          assetId: l.assetId,
        });
      }
    }
  }

  // ── Render-readiness ──
  let renderReady = errors.length === 0;
  if (renderReady) {
    for (const l of c.layers) {
      if (!l.enabled) continue;
      if (!l.assetId) continue; // text/shape/generated-inline layers need no asset
      const a = assetById.get(l.assetId);
      if (!a) {
        renderReady = false;
        break;
      }
      const usable = (a.status === "ready" || a.status === "approved") && !!a.url && isSafeAssetUrl(a.url);
      if (!usable) {
        renderReady = false;
        warn("layer_not_ready", `layer ${l.id} asset ${a.id} is not render-ready (${a.status})`, {
          layerId: l.id,
          assetId: a.id,
        });
      }
    }
  }

  return { ok: errors.length === 0, errors, warnings, renderReady };
}

// Convenience for adapters/tests.
export function isAudioLayer(l: Layer): boolean {
  return AUDIO_LAYER_TYPES.has(l.type);
}
