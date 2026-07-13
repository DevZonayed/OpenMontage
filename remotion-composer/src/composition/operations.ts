// Pure, immutable edit operations over the canonical composition.
//
// Every operation returns a NEW composition (deep-cloned) and never mutates its
// input — this is what makes undo/redo (see history.ts) trivially correct. The
// operations are the vocabulary the visual editor speaks; the Remotion Player
// re-renders from the result, so an edit is visible identically in preview and
// in the final render.

import {
  CanonicalComposition,
  cloneComposition,
  CompositionAsset,
  Layer,
  makeId,
  Scene,
  trackKindForType,
} from "./model";

export class OperationError extends Error {}

function findLayer(c: CanonicalComposition, id: string): Layer {
  const l = c.layers.find((x) => x.id === id);
  if (!l) throw new OperationError(`no layer "${id}"`);
  return l;
}

function clampStart(v: number): number {
  return Math.max(0, Math.round(v));
}
function clampDur(v: number): number {
  return Math.max(1, Math.round(v));
}

/** Add a fully-formed layer (id must be unique). */
export function addLayer(c: CanonicalComposition, layer: Layer): CanonicalComposition {
  if (c.layers.some((l) => l.id === layer.id)) {
    throw new OperationError(`duplicate layer id "${layer.id}"`);
  }
  const next = cloneComposition(c);
  next.layers.push({ ...layer });
  return next;
}

/** Set a text/title layer's on-screen content (Content / Scene title / subtitle).
 *  A direct, manual model edit — no automation. Trims to a sane bound. */
export function setLayerText(
  c: CanonicalComposition,
  id: string,
  patch: { text?: string; title?: string; subtitle?: string },
): CanonicalComposition {
  const next = cloneComposition(c);
  const layer = next.layers.find((l) => l.id === id);
  if (!layer) throw new OperationError(`no layer "${id}"`);
  const clamp = (s: string) => s.slice(0, 500);
  if (patch.text !== undefined) layer.text = clamp(patch.text);
  if (patch.title !== undefined) layer.title = clamp(patch.title);
  if (patch.subtitle !== undefined) layer.subtitle = clamp(patch.subtitle);
  return next;
}

/** Remove a layer by id. */
export function removeLayer(c: CanonicalComposition, id: string): CanonicalComposition {
  const next = cloneComposition(c);
  const before = next.layers.length;
  next.layers = next.layers.filter((l) => l.id !== id);
  if (next.layers.length === before) throw new OperationError(`no layer "${id}"`);
  return next;
}

/** Move a layer in time (start frame), preserving duration; clamps to >= 0. */
export function moveLayer(
  c: CanonicalComposition,
  id: string,
  newStartFrame: number,
): CanonicalComposition {
  const next = cloneComposition(c);
  const l = findLayer(next, id);
  if (l.locked) throw new OperationError(`layer "${id}" is locked`);
  l.startFrame = clampStart(newStartFrame);
  return next;
}

/** Resize a layer's duration; clamps to >= 1. */
export function resizeLayer(
  c: CanonicalComposition,
  id: string,
  newDurationFrames: number,
): CanonicalComposition {
  const next = cloneComposition(c);
  const l = findLayer(next, id);
  if (l.locked) throw new OperationError(`layer "${id}" is locked`);
  l.durationFrames = clampDur(newDurationFrames);
  return next;
}

/** Set start and/or duration together (a trim). */
export function trimLayer(
  c: CanonicalComposition,
  id: string,
  edit: { startFrame?: number; durationFrames?: number },
): CanonicalComposition {
  const next = cloneComposition(c);
  const l = findLayer(next, id);
  if (l.locked) throw new OperationError(`layer "${id}" is locked`);
  if (edit.startFrame !== undefined) l.startFrame = clampStart(edit.startFrame);
  if (edit.durationFrames !== undefined) l.durationFrames = clampDur(edit.durationFrames);
  return next;
}

/** Explicit timing setter (both fields). */
export function setTiming(
  c: CanonicalComposition,
  id: string,
  startFrame: number,
  durationFrames: number,
): CanonicalComposition {
  return trimLayer(c, id, { startFrame, durationFrames });
}

/**
 * Split a layer at an ABSOLUTE frame. Returns the composition with the original
 * shortened to end at `atFrame` and a new layer covering the remainder. The new
 * layer keeps the same asset + approval; for media it advances `sourceOffsetFrames`
 * so the two halves are continuous. Stable id for the new part is derived
 * deterministically from the original id + split frame.
 */
export function splitLayer(
  c: CanonicalComposition,
  id: string,
  atFrame: number,
): { composition: CanonicalComposition; newLayerId: string } {
  const l = findLayer(c, id);
  const start = l.startFrame;
  const end = l.startFrame + l.durationFrames;
  const at = Math.round(atFrame);
  if (at <= start || at >= end) {
    throw new OperationError(`split frame ${at} not strictly inside [${start}, ${end})`);
  }
  const next = cloneComposition(c);
  const orig = findLayer(next, id);
  const firstDur = at - start;
  const secondDur = end - at;
  orig.durationFrames = firstDur;

  let newId = makeId(`${orig.id}s`, at);
  while (next.layers.some((x) => x.id === newId)) newId = makeId(`${newId}x`, at);

  const second: Layer = {
    ...cloneLayer(orig),
    id: newId,
    startFrame: at,
    durationFrames: secondDur,
    sourceOffsetFrames: (orig.sourceOffsetFrames ?? 0) + firstDur,
  };
  const idx = next.layers.findIndex((x) => x.id === id);
  next.layers.splice(idx + 1, 0, second);
  return { composition: next, newLayerId: newId };
}

function cloneLayer(l: Layer): Layer {
  return JSON.parse(JSON.stringify(l)) as Layer;
}

/** Set an audio layer's volume (0..1). */
export function setVolume(
  c: CanonicalComposition,
  id: string,
  volume: number,
): CanonicalComposition {
  const next = cloneComposition(c);
  const l = findLayer(next, id);
  l.volume = Math.min(1, Math.max(0, volume));
  return next;
}

/** Mute / unmute a whole track. */
export function muteTrack(
  c: CanonicalComposition,
  trackId: string,
  muted: boolean,
): CanonicalComposition {
  const next = cloneComposition(c);
  const t = next.tracks.find((x) => x.id === trackId);
  if (!t) throw new OperationError(`no track "${trackId}"`);
  t.muted = muted;
  return next;
}

/** Reorder scenes by moving one from index → index (stable, clamped). */
export function reorderScene(
  c: CanonicalComposition,
  fromIndex: number,
  toIndex: number,
): CanonicalComposition {
  const next = cloneComposition(c);
  const n = next.scenes.length;
  if (fromIndex < 0 || fromIndex >= n) throw new OperationError(`scene index ${fromIndex} out of range`);
  const to = Math.min(Math.max(0, toIndex), n - 1);
  const [moved] = next.scenes.splice(fromIndex, 1);
  next.scenes.splice(to, 0, moved);
  return next;
}

export function addScene(c: CanonicalComposition, scene: Scene): CanonicalComposition {
  if (c.scenes.some((s) => s.id === scene.id)) throw new OperationError(`duplicate scene "${scene.id}"`);
  const next = cloneComposition(c);
  next.scenes.push({ ...scene });
  return next;
}

/**
 * Direct asset replacement (manual): swap the media backing a layer WITHOUT
 * rebuilding anything else and WITHOUT changing the stable asset id. The prior
 * url/version are archived on `previousVersions` so the editor can visually
 * compare/revert. The `approved` flag is preserved by default (caller may reset).
 */
export interface ReplaceAssetPatch {
  url: string | null;
  status?: CompositionAsset["status"];
  provenance?: CompositionAsset["provenance"];
  approved?: boolean;
}

export function replaceAsset(
  c: CanonicalComposition,
  assetId: string,
  patch: ReplaceAssetPatch,
): CanonicalComposition {
  const next = cloneComposition(c);
  const a = next.assets.find((x) => x.id === assetId);
  if (!a) throw new OperationError(`no asset "${assetId}"`);
  const archive = (a as unknown as { previousVersions?: unknown[] }).previousVersions ?? [];
  archive.push({ url: a.url, version: a.version, status: a.status, provenance: a.provenance });
  a.version = a.version + 1;
  a.url = patch.url;
  a.status = patch.status ?? (patch.url ? "ready" : "generating");
  if (patch.provenance) a.provenance = patch.provenance;
  if (patch.approved !== undefined) a.approved = patch.approved;
  (a as unknown as { previousVersions?: unknown[] }).previousVersions = archive;
  return next;
}

/** Revert an asset to a prior archived version (visual compare/revert support). */
export function revertAsset(
  c: CanonicalComposition,
  assetId: string,
  toVersion: number,
): CanonicalComposition {
  const next = cloneComposition(c);
  const a = next.assets.find((x) => x.id === assetId) as
    | (CompositionAsset & { previousVersions?: Array<{ url: string | null; version: number; status: CompositionAsset["status"]; provenance?: CompositionAsset["provenance"] }> })
    | undefined;
  if (!a) throw new OperationError(`no asset "${assetId}"`);
  const prev = (a.previousVersions ?? []).find((p) => p.version === toVersion);
  if (!prev) throw new OperationError(`asset "${assetId}" has no version ${toVersion}`);
  a.url = prev.url;
  a.status = prev.status;
  a.provenance = prev.provenance;
  a.version = toVersion;
  return next;
}

/** Approve / unapprove an asset (approval state is preserved across regen). */
export function setAssetApproval(
  c: CanonicalComposition,
  assetId: string,
  approved: boolean,
): CanonicalComposition {
  const next = cloneComposition(c);
  const a = next.assets.find((x) => x.id === assetId);
  if (!a) throw new OperationError(`no asset "${assetId}"`);
  a.approved = approved;
  return next;
}

/** Normalize a layer's trackId to match its type (used after type changes). */
export function retrackLayer(c: CanonicalComposition, id: string): CanonicalComposition {
  const next = cloneComposition(c);
  const l = findLayer(next, id);
  l.trackId = trackKindForType(l.type);
  return next;
}
