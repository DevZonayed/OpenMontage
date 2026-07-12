import React from "react";
import {
  AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig,
  interpolate, spring, Easing,
} from "remotion";

// A GENERIC, CINEMATIC composition that renders the canonical timeline.json as a
// real Remotion film. Each layer becomes an animated <Sequence> so the render is
// a true motion piece — kinetic titles, Ken-Burns visual scenes, lower-third
// captions, an animated background, and an elegant title card when a timeline is
// still empty. Self-contained (no network/assets) so ANY project renders.

export type TimelineLayer = {
  id: string;
  type: string;
  start_frame: number;
  duration_frames: number;
  z?: number;
  enabled?: boolean;
  opacity?: number;
  text?: string;
};

export type TimelineMeta = { title?: string; targetFormatted?: string; pipeline?: string };

export type TimelineDoc = {
  fps?: number; total_frames?: number; width?: number; height?: number;
  layers?: TimelineLayer[];
};

export type TimelineFrameProps = { timeline: TimelineDoc; meta?: TimelineMeta };

// Cinematic palette (matches the Backlot board tokens).
const BONE = "#e9e4d6";
const AMBER = "#e8c07d";
const DIM = "#8a93a3";
const TYPE = {
  video: ["#2b3d63", "#6ea8fe"], image: ["#1f4d3e", "#63d2a4"],
  shape: ["#3a2b57", "#b48ef0"], text: ["#2a2620", "#e8c07d"],
  caption: ["#3a2330", "#e8a0c0"], narration: ["#3a2c1c", "#f0a868"],
  music: ["#183842", "#8ad0e0"], sfx: ["#3a3a18", "#d0d060"],
} as Record<string, [string, string]>;
const AUDIO = new Set(["narration", "music", "sfx"]);
const isText = (t: string) => t === "text" || t === "caption";

const accentOf = (t: string) => (TYPE[t] ? TYPE[t][1] : DIM);

// ── Animated cinematic backdrop ───────────────────────────────────────────────
const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const drift = interpolate(f, [0, Math.max(1, durationInFrames)], [0, 1]);
  const gx = 30 + Math.sin(drift * Math.PI * 2) * 12;
  const gy = 32 + Math.cos(drift * Math.PI * 2) * 10;
  return (
    <AbsoluteFill
      style={{
        background:
          `radial-gradient(120% 90% at ${gx}% ${gy}%, #141a2e 0%, #0b1020 55%, #070a13 100%)`,
      }}
    >
      <AbsoluteFill
        style={{ boxShadow: "inset 0 0 320px 60px rgba(0,0,0,0.55)", opacity: 0.9 }}
      />
    </AbsoluteFill>
  );
};

// Envelope: smooth spring-in, hold, gentle fade before the scene ends.
function useEnvelope(dur: number) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 200 } });
  const out = interpolate(frame, [Math.max(0, dur - 14), dur], [1, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return { frame, enter, appear: Math.min(enter, out) };
}

// ── Kinetic title / on-screen text ────────────────────────────────────────────
const TextScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { enter, appear } = useEnvelope(layer.duration_frames);
  const y = interpolate(enter, [0, 1], [34, 0]);
  const scale = interpolate(enter, [0, 1], [0.94, 1]);
  const underline = interpolate(enter, [0, 1], [0, 1]);
  const accent = accentOf(layer.type);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", padding: "0 8%" }}>
      <div style={{ opacity: appear, transform: `translateY(${y}px) scale(${scale})`, textAlign: "center" }}>
        <div
          style={{
            color: BONE, fontFamily: "system-ui, -apple-system, sans-serif",
            fontWeight: 800, fontSize: 96, lineHeight: 1.04, letterSpacing: "-0.02em",
            textShadow: "0 8px 40px rgba(0,0,0,0.55)",
          }}
        >
          {layer.text || "Untitled"}
        </div>
        <div
          style={{
            height: 6, marginTop: 28, borderRadius: 3, background: accent,
            width: `${underline * 42}%`, marginLeft: "auto", marginRight: "auto",
            boxShadow: `0 0 24px ${accent}`,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

// ── Lower-third caption ───────────────────────────────────────────────────────
const CaptionScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { enter, appear } = useEnvelope(layer.duration_frames);
  const x = interpolate(enter, [0, 1], [-40, 0]);
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "flex-start", padding: "0 0 9% 7%" }}>
      <div
        style={{
          opacity: appear, transform: `translateX(${x}px)`,
          background: "linear-gradient(90deg, rgba(10,12,20,0.86), rgba(10,12,20,0.4))",
          borderLeft: `5px solid ${accentOf(layer.type)}`,
          padding: "18px 30px 18px 24px", borderRadius: 10, maxWidth: "72%",
        }}
      >
        <div style={{ color: BONE, fontFamily: "system-ui, sans-serif", fontWeight: 600, fontSize: 46, letterSpacing: "-0.01em" }}>
          {layer.text || "Caption"}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Ken-Burns visual scene (video / image / shape) ────────────────────────────
const VisualScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, appear } = useEnvelope(layer.duration_frames);
  const dur = Math.max(1, layer.duration_frames);
  const [c0, c1] = TYPE[layer.type] || ["#232a36", "#5a6472"];
  const zoom = interpolate(frame, [0, dur], [1.0, 1.12], { easing: Easing.inOut(Easing.ease) });
  const pan = interpolate(frame, [0, dur], [-2.5, 2.5]);
  const op = typeof layer.opacity === "number" ? layer.opacity : 1;
  return (
    <AbsoluteFill style={{ opacity: appear * op }}>
      <AbsoluteFill
        style={{
          transform: `scale(${zoom}) translateX(${pan}%)`,
          background: `linear-gradient(135deg, ${c1}44 0%, ${c0} 55%, #0a0e18 100%)`,
        }}
      />
      {/* legibility + cinematic grade */}
      <AbsoluteFill style={{ background: "linear-gradient(0deg, rgba(6,9,17,0.72) 0%, rgba(6,9,17,0) 46%)" }} />
      <AbsoluteFill style={{ padding: 56, justifyContent: "space-between" }}>
        <span
          style={{
            alignSelf: "flex-start", color: accentOf(layer.type),
            border: `1.5px solid ${accentOf(layer.type)}`, borderRadius: 999,
            padding: "6px 16px", fontFamily: "ui-monospace, monospace", fontSize: 22,
            letterSpacing: "0.14em", textTransform: "uppercase", background: "rgba(0,0,0,0.25)",
          }}
        >
          {layer.type}
        </span>
        {layer.text ? (
          <div style={{ color: BONE, fontFamily: "system-ui, sans-serif", fontWeight: 700, fontSize: 62, letterSpacing: "-0.01em", textShadow: "0 6px 30px rgba(0,0,0,0.6)", maxWidth: "80%" }}>
            {layer.text}
          </div>
        ) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

// ── Audio presence: a subtle animated waveform strip at the very bottom ────────
const AudioScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const frame = useCurrentFrame();
  const accent = accentOf(layer.type);
  const bars = 48;
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "center", padding: "0 0 34px" }}>
      <div style={{ display: "flex", gap: 5, alignItems: "flex-end", height: 46, opacity: 0.85 }}>
        {Array.from({ length: bars }).map((_, i) => {
          const h = 8 + (Math.sin((i * 0.6) + frame * 0.22) * 0.5 + 0.5) * 38;
          return <div key={i} style={{ width: 5, height: h, borderRadius: 3, background: accent }} />;
        })}
      </div>
      <div style={{ marginTop: 10, color: accent, fontFamily: "ui-monospace, monospace", fontSize: 20, letterSpacing: "0.14em", textTransform: "uppercase" }}>
        {layer.type === "narration" ? "◗ narration" : layer.type === "music" ? "♪ music" : "♪ sfx"}
      </div>
    </AbsoluteFill>
  );
};

// ── Elegant title card when the timeline has no visible layers yet ─────────────
const TitleCard: React.FC<{ meta?: TimelineMeta }> = ({ meta }) => {
  const { enter } = useEnvelope(9999);
  const y = interpolate(enter, [0, 1], [24, 0]);
  const line = interpolate(enter, [0, 1], [0, 1]);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ opacity: enter, transform: `translateY(${y}px)`, textAlign: "center" }}>
        <div style={{ color: AMBER, fontFamily: "ui-monospace, monospace", fontSize: 24, letterSpacing: "0.32em", textTransform: "uppercase", marginBottom: 22 }}>
          Backlot
        </div>
        <div style={{ color: BONE, fontFamily: "system-ui, sans-serif", fontWeight: 800, fontSize: 92, letterSpacing: "-0.02em", textShadow: "0 8px 40px rgba(0,0,0,0.5)" }}>
          {meta?.title || "Untitled project"}
        </div>
        <div style={{ height: 4, background: AMBER, width: `${line * 180}px`, margin: "26px auto 0", borderRadius: 2, boxShadow: `0 0 22px ${AMBER}` }} />
        <div style={{ color: DIM, fontFamily: "ui-monospace, monospace", fontSize: 26, marginTop: 26, letterSpacing: "0.06em" }}>
          {(meta?.pipeline || "animation")} · {meta?.targetFormatted || "0:00"} · timeline is empty — add layers to compose
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const timelineFrameDefaults: TimelineFrameProps = {
  timeline: {
    fps: 30, total_frames: 240, width: 1920, height: 1080,
    layers: [
      { id: "bg", type: "video", start_frame: 0, duration_frames: 240, z: 0 },
      { id: "ttl", type: "text", start_frame: 20, duration_frames: 200, z: 2, text: "Timeline Preview" },
    ],
  },
  meta: { title: "Timeline Preview", targetFormatted: "0:08", pipeline: "animation" },
};

const Scene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  if (AUDIO.has(layer.type)) return <AudioScene layer={layer} />;
  if (layer.type === "caption") return <CaptionScene layer={layer} />;
  if (isText(layer.type)) return <TextScene layer={layer} />;
  return <VisualScene layer={layer} />;
};

export const TimelineFrame: React.FC<TimelineFrameProps> = ({ timeline, meta }) => {
  const layers = (timeline?.layers || [])
    .filter((l) => l && l.enabled !== false)
    .slice()
    .sort((a, b) => (a.z || 0) - (b.z || 0));
  const hasVisible = layers.some((l) => !AUDIO.has(l.type));

  return (
    <AbsoluteFill>
      <Backdrop />
      {layers.map((l) => (
        <Sequence key={l.id} from={Math.max(0, l.start_frame)} durationInFrames={Math.max(1, l.duration_frames)}>
          <Scene layer={l} />
        </Sequence>
      ))}
      {!hasVisible ? <TitleCard meta={meta} /> : null}
    </AbsoluteFill>
  );
};
