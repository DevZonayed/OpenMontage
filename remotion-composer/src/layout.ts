// Deterministic 16:9 layout system for the canonical composition.
//
// Everything the composition paints is placed into ONE of a set of DISJOINT
// vertical zones inside a title-safe area, so captions, lower-thirds, scene
// titles, badges and the audio-presence strip can all be on screen at once
// without ever colliding. Percentages and labels are bounded/legible.
//
// These functions are pure and unit-tested (see layout.test.ts) — the collision
// guarantees are asserted, not eyeballed.

export const SAFE_MARGIN_X = 0.055; // title-safe horizontal inset (5.5%)
export const SAFE_MARGIN_Y = 0.06; // title-safe vertical inset (6%)

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface SafeArea {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
  height: number;
}

export function safeArea(width: number, height: number): SafeArea {
  const left = Math.round(width * SAFE_MARGIN_X);
  const top = Math.round(height * SAFE_MARGIN_Y);
  return {
    left,
    right: width - left,
    top,
    bottom: height - top,
    width: width - 2 * left,
    height: height - 2 * top,
  };
}

// Zone bands as fractions of the FULL height. They are intentionally disjoint:
// each band's `bottom` <= the next band's `top`. Verified in layout.test.ts.
export type ZoneName = "badge" | "title" | "lowerThird" | "caption" | "audio";

// Fractions of full height. Disjoint AND fully inside the safe band [MARGIN_Y,
// 1-MARGIN_Y] = [0.06, 0.94]. Both invariants are asserted in layout.test.ts.
export const ZONES: Record<ZoneName, { top: number; bottom: number }> = {
  badge: { top: 0.06, bottom: 0.145 },
  title: { top: 0.3, bottom: 0.56 },
  lowerThird: { top: 0.64, bottom: 0.725 },
  caption: { top: 0.735, bottom: 0.83 },
  audio: { top: 0.84, bottom: 0.93 },
};

export const ZONE_ORDER: ZoneName[] = ["badge", "title", "lowerThird", "caption", "audio"];

/** Absolute pixel rect for a zone, horizontally inset to the safe area. */
export function zoneRect(zone: ZoneName, width: number, height: number): Rect {
  const s = safeArea(width, height);
  const band = ZONES[zone];
  const top = Math.round(band.top * height);
  const bottom = Math.round(band.bottom * height);
  return { left: s.left, top, width: s.width, height: Math.max(1, bottom - top) };
}

/** Clamp a raw percentage to a legible integer 0..100 string (never "4090%"). */
export function clampPercent(raw: number): string {
  if (!Number.isFinite(raw)) return "0%";
  const v = Math.min(100, Math.max(0, Math.round(raw)));
  return `${v}%`;
}

/** Volume (0..1) → clamped percent label. */
export function volumePercent(volume: number | undefined): string {
  return clampPercent((typeof volume === "number" ? volume : 1) * 100);
}

/** Truncate a label to a max character budget with an ellipsis, keeping it legible. */
export function truncateLabel(label: string, maxChars = 42): string {
  const s = (label ?? "").trim();
  if (s.length <= maxChars) return s;
  return s.slice(0, Math.max(1, maxChars - 1)).trimEnd() + "…";
}

export const MAX_AUDIO_ROWS = 3; // narration / music / sfx

export interface AudioRow {
  top: number;
  height: number;
  left: number;
  width: number;
}

/**
 * Stable, non-overlapping slot for one audio-presence row. `index` is the audio
 * layer's fixed position (0..MAX_AUDIO_ROWS-1), so simultaneously-playing rows
 * stack (bottom-up) inside the audio band and never overpaint each other. The
 * slot is CLAMPED (never wrapped) — a caller must cap the visible row count with
 * `audioRowsPlan`, so out-of-range indices can never land on top of slot 0.
 */
export function audioRow(index: number, width: number, height: number): AudioRow {
  const s = safeArea(width, height);
  const band = zoneRect("audio", width, height);
  const rows = MAX_AUDIO_ROWS;
  const gap = Math.round(band.height * 0.08);
  const rowHeight = Math.floor((band.height - gap * (rows - 1)) / rows);
  const slot = Math.min(Math.max(0, Math.trunc(index)), rows - 1); // clamp, never wrap
  // Stack from the bottom of the band upward: slot 0 lowest.
  const top = band.top + (rows - 1 - slot) * (rowHeight + gap);
  return { top, height: rowHeight, left: s.left, width: s.width };
}

/**
 * How many audio-presence rows to draw for `count` audio layers, and how many
 * overflow beyond the visible cap (shown as a "+N" indicator instead of a row
 * stacked on top of another). Guarantees visible rows fit the band without overlap.
 */
export function audioRowsPlan(count: number): { visible: number; overflow: number } {
  const n = Math.max(0, Math.trunc(count));
  const visible = Math.min(n, MAX_AUDIO_ROWS);
  return { visible, overflow: n - visible };
}

/**
 * Non-overlapping y positions for N stacked items within a zone (e.g. multiple
 * lower-thirds). Returns the top y for each row, top-aligned within the band.
 */
export function stackWithinZone(
  count: number,
  zone: ZoneName,
  width: number,
  height: number,
): number[] {
  const band = zoneRect(zone, width, height);
  const n = Math.max(1, count);
  const gap = Math.round(band.height * 0.12);
  const rowHeight = Math.floor((band.height - gap * (n - 1)) / n);
  return Array.from({ length: count }, (_, i) => band.top + i * (rowHeight + gap));
}
