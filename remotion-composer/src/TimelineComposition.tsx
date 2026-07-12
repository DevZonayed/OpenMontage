import React from "react";
import {
  AbsoluteFill,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
  Easing,
  Audio,
  Img,
  OffthreadVideo,
} from "remotion";

// ─────────────────────────────────────────────────────────────────────────────
// CANONICAL PRODUCTION COMPOSITION
//
// This is the ONE Remotion composition that powers (a) the embedded @remotion/player
// live preview inside Backlot and (b) the final render via the pinned Remotion CLI.
// Both are fed the identical `{timeline, meta}` props (see composition/adapter.ts →
// renderProps), so the preview cannot diverge from the render — same component, same
// data, same pixels.
//
// It renders the persisted backend timeline layers as a real motion film: each layer
// becomes an animated <Sequence>; transforms/crops/opacity/fades/transitions and
// per-layer audio volume are honored; real media (absolute-URL `source`) is composited
// via <Img>/<OffthreadVideo>/<Audio>; project-local paths without a resolvable URL
// fall back to a designed, self-contained placeholder scene so ANY project renders.
// ─────────────────────────────────────────────────────────────────────────────

export type Transform = {
  x?: number;
  y?: number;
  scale?: number;
  rotation?: number;
  opacity?: number;
  crop?: { top: number; right: number; bottom: number; left: number };
};
export type Fade = { inFrames?: number; outFrames?: number };
export type TransitionKind = "none" | "fade" | "slide" | "wipe" | "zoom";
export type Transition = { kind: TransitionKind; durationFrames: number };

export type TimelineLayer = {
  id: string;
  type: string;
  start_frame: number;
  duration_frames: number;
  z?: number;
  enabled?: boolean;
  opacity?: number;
  text?: string;
  title?: string;
  subtitle?: string;
  source?: string | null;
  volume?: number;
  transform?: Transform;
  fade?: Fade;
  transitionIn?: Transition;
  transitionOut?: Transition;
};

export type TimelineMeta = {
  title?: string;
  targetFormatted?: string;
  pipeline?: string;
};

export type TimelineDoc = {
  fps?: number;
  total_frames?: number;
  width?: number;
  height?: number;
  layers?: TimelineLayer[];
};

export type TimelineFrameProps = { timeline: TimelineDoc; meta?: TimelineMeta };

// Cinematic palette (matches the Backlot board tokens).
const BONE = "#e9e4d6";
const AMBER = "#e8c07d";
const DIM = "#8a93a3";
const TYPE = {
  video: ["#2b3d63", "#6ea8fe"],
  image: ["#1f4d3e", "#63d2a4"],
  shape: ["#3a2b57", "#b48ef0"],
  text: ["#2a2620", "#e8c07d"],
  caption: ["#3a2330", "#e8a0c0"],
  narration: ["#3a2c1c", "#f0a868"],
  music: ["#183842", "#8ad0e0"],
  sfx: ["#3a3a18", "#d0d060"],
} as Record<string, [string, string]>;
const AUDIO = new Set(["narration", "music", "sfx"]);
const isText = (t: string) => t === "text" || t === "caption";
const accentOf = (t: string) => (TYPE[t] ? TYPE[t][1] : DIM);

// A source is "loadable media" only when it is an absolute URL a browser (and the
// headless render browser) can fetch. Project-local relative paths render as a
// designed placeholder — see BACKEND_CONTRACT.md for the media-resolution gap.
function isLoadableUrl(src?: string | null): src is string {
  if (!src || typeof src !== "string") return false;
  return /^(https?:)?\/\//.test(src) || src.startsWith("blob:");
}

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
        background: `radial-gradient(120% 90% at ${gx}% ${gy}%, #141a2e 0%, #0b1020 55%, #070a13 100%)`,
      }}
    >
      <AbsoluteFill
        style={{ boxShadow: "inset 0 0 320px 60px rgba(0,0,0,0.55)", opacity: 0.9 }}
      />
    </AbsoluteFill>
  );
};

// Envelope: spring-in, hold, plus explicit fade in/out (frames) when provided.
function useEnvelope(dur: number, fade?: Fade) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 200 } });
  const fadeIn = fade?.inFrames
    ? interpolate(frame, [0, fade.inFrames], [0, 1], { extrapolateRight: "clamp" })
    : 1;
  const outStart = fade?.outFrames ? dur - fade.outFrames : Math.max(0, dur - 14);
  const out = interpolate(frame, [outStart, dur], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return { frame, enter, appear: Math.min(enter, out, fadeIn) };
}

// Per-layer transition (applied on top of the base envelope).
type TransitionStyle = { opacity: number; transform: string; clipPath?: string };
function transitionStyle(
  frame: number,
  dur: number,
  tin?: Transition,
  tout?: Transition,
): TransitionStyle {
  let opacity = 1;
  let tx = 0;
  let ty = 0;
  let scale = 1;
  let clip = "";
  const apply = (t: Transition, phase: "in" | "out", p: number) => {
    const k = phase === "in" ? p : 1 - p; // 0→1 progress of the effect
    switch (t.kind) {
      case "fade":
        opacity *= k;
        break;
      case "slide":
        tx += (1 - k) * (phase === "in" ? -80 : 80);
        break;
      case "zoom":
        scale *= 0.85 + k * 0.15;
        opacity *= k;
        break;
      case "wipe":
        clip = `inset(0 ${(1 - k) * 100}% 0 0)`;
        break;
      default:
        break;
    }
  };
  if (tin && tin.kind !== "none" && frame < tin.durationFrames) {
    apply(tin, "in", frame / Math.max(1, tin.durationFrames));
  }
  if (tout && tout.kind !== "none" && frame > dur - tout.durationFrames) {
    apply(tout, "out", (dur - frame) / Math.max(1, tout.durationFrames));
  }
  return {
    opacity,
    transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
    clipPath: clip || undefined,
  };
}

function transformStyle(t?: Transform): React.CSSProperties {
  if (!t) return {};
  const parts: string[] = [];
  if (t.x || t.y) parts.push(`translate(${t.x || 0}px, ${t.y || 0}px)`);
  if (t.scale !== undefined) parts.push(`scale(${t.scale})`);
  if (t.rotation) parts.push(`rotate(${t.rotation}deg)`);
  const clip = t.crop
    ? `inset(${t.crop.top * 100}% ${t.crop.right * 100}% ${t.crop.bottom * 100}% ${t.crop.left * 100}%)`
    : undefined;
  return {
    transform: parts.length ? parts.join(" ") : undefined,
    opacity: t.opacity,
    clipPath: clip,
  };
}

// ── Real media scene (image / video) ──────────────────────────────────────────
const MediaScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const dur = Math.max(1, layer.duration_frames);
  const zoom = interpolate(frame, [0, dur], [1.02, 1.1], {
    easing: Easing.inOut(Easing.ease),
  });
  const base = typeof layer.opacity === "number" ? layer.opacity : 1;
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  const src = layer.source as string;
  return (
    <AbsoluteFill style={{ opacity: appear * base * (tstyle.opacity ?? 1) }}>
      <AbsoluteFill style={{ ...transformStyle(layer.transform), ...tstyle, opacity: undefined }}>
        <AbsoluteFill style={{ transform: `scale(${zoom})` }}>
          {layer.type === "video" ? (
            <OffthreadVideo src={src} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          ) : (
            <Img src={src} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          )}
        </AbsoluteFill>
      </AbsoluteFill>
      {layer.text ? <LowerLabel text={layer.text} accent={accentOf(layer.type)} /> : null}
    </AbsoluteFill>
  );
};

const LowerLabel: React.FC<{ text: string; accent: string }> = ({ text, accent }) => (
  <AbsoluteFill style={{ justifyContent: "flex-end", padding: 56 }}>
    <div
      style={{
        color: BONE,
        fontFamily: "system-ui, sans-serif",
        fontWeight: 700,
        fontSize: 54,
        letterSpacing: "-0.01em",
        textShadow: "0 6px 30px rgba(0,0,0,0.6)",
        maxWidth: "80%",
        borderLeft: `5px solid ${accent}`,
        paddingLeft: 22,
      }}
    >
      {text}
    </div>
  </AbsoluteFill>
);

// ── Kinetic title / on-screen text ────────────────────────────────────────────
const TextScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, enter, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const y = interpolate(enter, [0, 1], [34, 0]);
  const scale = interpolate(enter, [0, 1], [0.94, 1]);
  const underline = interpolate(enter, [0, 1], [0, 1]);
  const accent = accentOf(layer.type);
  const dur = Math.max(1, layer.duration_frames);
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", padding: "0 8%" }}>
      <div
        style={{
          opacity: appear * (tstyle.opacity ?? 1),
          transform: `translateY(${y}px) scale(${scale}) ${tstyle.transform ?? ""}`,
          textAlign: "center",
          ...transformStyle(layer.transform),
        }}
      >
        {layer.subtitle ? (
          <div
            style={{
              color: accent,
              fontFamily: "ui-monospace, monospace",
              fontSize: 26,
              letterSpacing: "0.28em",
              textTransform: "uppercase",
              marginBottom: 18,
            }}
          >
            {layer.subtitle}
          </div>
        ) : null}
        <div
          style={{
            color: BONE,
            fontFamily: "system-ui, -apple-system, sans-serif",
            fontWeight: 800,
            fontSize: 96,
            lineHeight: 1.04,
            letterSpacing: "-0.02em",
            textShadow: "0 8px 40px rgba(0,0,0,0.55)",
          }}
        >
          {layer.title || layer.text || "Untitled"}
        </div>
        <div
          style={{
            height: 6,
            marginTop: 28,
            borderRadius: 3,
            background: accent,
            width: `${underline * 42}%`,
            marginLeft: "auto",
            marginRight: "auto",
            boxShadow: `0 0 24px ${accent}`,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

// ── Lower-third caption ───────────────────────────────────────────────────────
const CaptionScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, enter, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const x = interpolate(enter, [0, 1], [-40, 0]);
  const dur = Math.max(1, layer.duration_frames);
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "flex-start", padding: "0 0 9% 7%" }}>
      <div
        style={{
          opacity: appear * (tstyle.opacity ?? 1),
          transform: `translateX(${x}px)`,
          background: "linear-gradient(90deg, rgba(10,12,20,0.86), rgba(10,12,20,0.4))",
          borderLeft: `5px solid ${accentOf(layer.type)}`,
          padding: "18px 30px 18px 24px",
          borderRadius: 10,
          maxWidth: "72%",
        }}
      >
        <div
          style={{
            color: BONE,
            fontFamily: "system-ui, sans-serif",
            fontWeight: 600,
            fontSize: 46,
            letterSpacing: "-0.01em",
          }}
        >
          {layer.text || "Caption"}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Designed placeholder visual (no loadable media) ───────────────────────────
const VisualScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const dur = Math.max(1, layer.duration_frames);
  const [c0, c1] = TYPE[layer.type] || ["#232a36", "#5a6472"];
  const zoom = interpolate(frame, [0, dur], [1.0, 1.12], { easing: Easing.inOut(Easing.ease) });
  const pan = interpolate(frame, [0, dur], [-2.5, 2.5]);
  const op = typeof layer.opacity === "number" ? layer.opacity : 1;
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  return (
    <AbsoluteFill style={{ opacity: appear * op * (tstyle.opacity ?? 1) }}>
      <AbsoluteFill style={{ ...transformStyle(layer.transform), ...tstyle, opacity: undefined }}>
        <AbsoluteFill
          style={{
            transform: `scale(${zoom}) translateX(${pan}%)`,
            background: `linear-gradient(135deg, ${c1}44 0%, ${c0} 55%, #0a0e18 100%)`,
          }}
        />
      </AbsoluteFill>
      <AbsoluteFill style={{ background: "linear-gradient(0deg, rgba(6,9,17,0.72) 0%, rgba(6,9,17,0) 46%)" }} />
      <AbsoluteFill style={{ padding: 56, justifyContent: "space-between" }}>
        <span
          style={{
            alignSelf: "flex-start",
            color: accentOf(layer.type),
            border: `1.5px solid ${accentOf(layer.type)}`,
            borderRadius: 999,
            padding: "6px 16px",
            fontFamily: "ui-monospace, monospace",
            fontSize: 22,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            background: "rgba(0,0,0,0.25)",
          }}
        >
          {layer.source ? `${layer.type} · placeholder` : layer.type}
        </span>
        {layer.text ? (
          <div
            style={{
              color: BONE,
              fontFamily: "system-ui, sans-serif",
              fontWeight: 700,
              fontSize: 62,
              letterSpacing: "-0.01em",
              textShadow: "0 6px 30px rgba(0,0,0,0.6)",
              maxWidth: "80%",
            }}
          >
            {layer.text}
          </div>
        ) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

// ── Audio: real <Audio> when a media url is present, else a presence strip ─────
const AudioScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const frame = useCurrentFrame();
  const accent = accentOf(layer.type);
  const bars = 48;
  const vol = typeof layer.volume === "number" ? Math.min(1, Math.max(0, layer.volume)) : 1;
  const dur = Math.max(1, layer.duration_frames);
  // Volume envelope with optional fades.
  const volAt = (f: number) => {
    let v = vol;
    if (layer.fade?.inFrames) v *= interpolate(f, [0, layer.fade.inFrames], [0, 1], { extrapolateRight: "clamp" });
    if (layer.fade?.outFrames)
      v *= interpolate(f, [dur - layer.fade.outFrames, dur], [1, 0], { extrapolateLeft: "clamp" });
    return Math.min(1, Math.max(0, v));
  };
  // Stagger the presence strips so stacked audio layers don't collide visually.
  const bottomPad = layer.type === "narration" ? 34 : layer.type === "music" ? 104 : 174;
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "center", padding: `0 0 ${bottomPad}px` }}>
      {isLoadableUrl(layer.source) ? <Audio src={layer.source} volume={volAt} /> : null}
      <div style={{ display: "flex", gap: 5, alignItems: "flex-end", height: 46, opacity: 0.85 }}>
        {Array.from({ length: bars }).map((_, i) => {
          const h = 8 + (Math.sin(i * 0.6 + frame * 0.22) * 0.5 + 0.5) * 38 * (0.4 + 0.6 * vol);
          return <div key={i} style={{ width: 5, height: h, borderRadius: 3, background: accent }} />;
        })}
      </div>
      <div
        style={{
          marginTop: 10,
          color: accent,
          fontFamily: "ui-monospace, monospace",
          fontSize: 20,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
        }}
      >
        {layer.type === "narration" ? "◗ narration" : layer.type === "music" ? "♪ music" : "♪ sfx"}
        {vol < 1 ? ` · ${Math.round(vol * 100)}%` : ""}
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
        <div
          style={{
            color: AMBER,
            fontFamily: "ui-monospace, monospace",
            fontSize: 24,
            letterSpacing: "0.32em",
            textTransform: "uppercase",
            marginBottom: 22,
          }}
        >
          Backlot
        </div>
        <div
          style={{
            color: BONE,
            fontFamily: "system-ui, sans-serif",
            fontWeight: 800,
            fontSize: 92,
            letterSpacing: "-0.02em",
            textShadow: "0 8px 40px rgba(0,0,0,0.5)",
          }}
        >
          {meta?.title || "Untitled project"}
        </div>
        <div
          style={{
            height: 4,
            background: AMBER,
            width: `${line * 180}px`,
            margin: "26px auto 0",
            borderRadius: 2,
            boxShadow: `0 0 22px ${AMBER}`,
          }}
        />
        <div
          style={{
            color: DIM,
            fontFamily: "ui-monospace, monospace",
            fontSize: 26,
            marginTop: 26,
            letterSpacing: "0.06em",
          }}
        >
          {meta?.pipeline || "animation"} · {meta?.targetFormatted || "0:00"} · timeline is empty — add layers to compose
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const timelineFrameDefaults: TimelineFrameProps = {
  timeline: {
    fps: 30,
    total_frames: 240,
    width: 1920,
    height: 1080,
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
  if ((layer.type === "video" || layer.type === "image") && isLoadableUrl(layer.source)) {
    return <MediaScene layer={layer} />;
  }
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
        <Sequence
          key={l.id}
          from={Math.max(0, l.start_frame)}
          durationInFrames={Math.max(1, l.duration_frames)}
          name={`${l.type}:${l.id}`}
        >
          <Scene layer={l} />
        </Sequence>
      ))}
      {!hasVisible ? <TitleCard meta={meta} /> : null}
    </AbsoluteFill>
  );
};
