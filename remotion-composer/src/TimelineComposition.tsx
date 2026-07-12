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
import {
  audioRow,
  safeArea,
  truncateLabel,
  volumePercent,
  zoneRect,
} from "./layout";

// ─────────────────────────────────────────────────────────────────────────────
// CANONICAL PRODUCTION COMPOSITION
//
// The ONE Remotion composition that powers both the embedded @remotion/player live
// preview and the final pinned-CLI render (identical `{timeline, meta}` props → no
// preview/render drift). Every on-screen element is placed into a DISJOINT layout
// zone inside a title-safe area (see layout.ts), so scene titles, lower-thirds,
// captions, badges and the audio-presence strip can all be on screen simultaneously
// without ever colliding. Audio layers are consolidated into stacked, fixed-slot
// rows so multiple tracks never overpaint each other.
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
  sourceOffsetFrames?: number;
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

export function isLoadableUrl(src?: string | null): src is string {
  if (!src || typeof src !== "string") return false;
  const s = src.trim();
  if (s === "" || s.includes("\\")) return false;
  if (/^(https?:)?\/\//.test(s) || s.startsWith("blob:")) return true;
  return s.startsWith("/") && !s.startsWith("//") && !s.split("/").includes("..");
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
      <AbsoluteFill style={{ boxShadow: "inset 0 0 320px 60px rgba(0,0,0,0.55)", opacity: 0.9 }} />
    </AbsoluteFill>
  );
};

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

type TransitionStyle = { opacity: number; transform: string; clipPath?: string };
function transitionStyle(frame: number, dur: number, tin?: Transition, tout?: Transition): TransitionStyle {
  let opacity = 1;
  let tx = 0;
  const ty = 0;
  let scale = 1;
  let clip = "";
  const apply = (t: Transition, phase: "in" | "out", p: number) => {
    const k = phase === "in" ? p : 1 - p;
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
  return { opacity, transform: `translate(${tx}px, ${ty}px) scale(${scale})`, clipPath: clip || undefined };
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
  return { transform: parts.length ? parts.join(" ") : undefined, opacity: t.opacity, clipPath: clip };
}

// Absolute-positioned wrapper that places its children inside a named layout zone.
const ZoneBox: React.FC<{
  zone: "badge" | "title" | "lowerThird" | "caption" | "audio";
  style?: React.CSSProperties;
  children: React.ReactNode;
}> = ({ zone, style, children }) => {
  const { width, height } = useVideoConfig();
  const r = zoneRect(zone, width, height);
  return (
    <div style={{ position: "absolute", left: r.left, top: r.top, width: r.width, height: r.height, ...style }}>
      {children}
    </div>
  );
};

// Full-frame background (media or designed gradient) — never carries text into
// the lower bands; its label + badge are placed in their own zones.
const BackgroundFill: React.FC<{ layer: TimelineLayer; media: boolean }> = ({ layer, media }) => {
  const { frame, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const dur = Math.max(1, layer.duration_frames);
  const [c0, c1] = TYPE[layer.type] || ["#232a36", "#5a6472"];
  const zoom = interpolate(frame, [0, dur], [1.02, 1.1], { easing: Easing.inOut(Easing.ease) });
  const pan = interpolate(frame, [0, dur], [-2, 2]);
  const base = typeof layer.opacity === "number" ? layer.opacity : 1;
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  return (
    <AbsoluteFill style={{ opacity: appear * base * tstyle.opacity }}>
      <AbsoluteFill style={{ ...transformStyle(layer.transform), transform: tstyle.transform, clipPath: tstyle.clipPath }}>
        {media ? (
          <AbsoluteFill style={{ transform: `scale(${zoom})` }}>
            {layer.type === "video" ? (
              <OffthreadVideo
                src={layer.source as string}
                trimBefore={layer.sourceOffsetFrames ? Math.max(0, Math.round(layer.sourceOffsetFrames)) : undefined}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            ) : (
              <Img src={layer.source as string} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            )}
          </AbsoluteFill>
        ) : (
          <AbsoluteFill
            style={{
              transform: `scale(${zoom}) translateX(${pan}%)`,
              background: `linear-gradient(135deg, ${c1}44 0%, ${c0} 55%, #0a0e18 100%)`,
            }}
          />
        )}
      </AbsoluteFill>
      {/* legibility grade at the bottom so lower-band text stays readable */}
      <AbsoluteFill style={{ background: "linear-gradient(0deg, rgba(6,9,17,0.82) 0%, rgba(6,9,17,0) 42%)" }} />
    </AbsoluteFill>
  );
};

// Type badge in the top-left badge zone.
const Badge: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { appear } = useEnvelope(layer.duration_frames, layer.fade);
  return (
    <ZoneBox zone="badge" style={{ display: "flex", alignItems: "flex-start" }}>
      <span
        style={{
          opacity: appear,
          color: accentOf(layer.type),
          border: `1.5px solid ${accentOf(layer.type)}`,
          borderRadius: 999,
          padding: "8px 20px",
          fontFamily: "ui-monospace, monospace",
          fontSize: 24,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          background: "rgba(0,0,0,0.32)",
        }}
      >
        {layer.source && !isLoadableUrl(layer.source) ? `${layer.type} · placeholder` : layer.type}
      </span>
    </ZoneBox>
  );
};

// Lower-third label (from a visual layer's `text`) in the lowerThird zone.
const LowerThird: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { enter, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const x = interpolate(enter, [0, 1], [-36, 0]);
  const accent = accentOf(layer.type);
  return (
    <ZoneBox zone="lowerThird" style={{ display: "flex", alignItems: "center" }}>
      <div
        style={{
          opacity: appear,
          transform: `translateX(${x}px)`,
          borderLeft: `6px solid ${accent}`,
          background: "linear-gradient(90deg, rgba(10,12,20,0.82), rgba(10,12,20,0.15))",
          padding: "14px 30px",
          borderRadius: 10,
          maxWidth: "82%",
        }}
      >
        <div
          style={{
            color: BONE,
            fontFamily: "system-ui, sans-serif",
            fontWeight: 700,
            fontSize: 52,
            lineHeight: 1.05,
            letterSpacing: "-0.01em",
            textShadow: "0 6px 30px rgba(0,0,0,0.6)",
          }}
        >
          {truncateLabel(layer.text || "", 52)}
        </div>
      </div>
    </ZoneBox>
  );
};

// Kinetic title / on-screen text in the centered title zone.
const TitleScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, enter, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const y = interpolate(enter, [0, 1], [30, 0]);
  const scale = interpolate(enter, [0, 1], [0.95, 1]);
  const underline = interpolate(enter, [0, 1], [0, 1]);
  const accent = accentOf(layer.type);
  const dur = Math.max(1, layer.duration_frames);
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  return (
    <ZoneBox zone="title" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div
        style={{
          opacity: appear * tstyle.opacity,
          transform: `translateY(${y}px) scale(${scale})`,
          textAlign: "center",
          ...transformStyle(layer.transform),
          maxWidth: "100%",
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
            {truncateLabel(layer.subtitle, 44)}
          </div>
        ) : null}
        <div
          style={{
            color: BONE,
            fontFamily: "system-ui, -apple-system, sans-serif",
            fontWeight: 800,
            fontSize: 92,
            lineHeight: 1.05,
            letterSpacing: "-0.02em",
            textShadow: "0 8px 40px rgba(0,0,0,0.55)",
          }}
        >
          {layer.title || layer.text || "Untitled"}
        </div>
        <div
          style={{
            height: 6,
            marginTop: 26,
            borderRadius: 3,
            background: accent,
            width: `${underline * 42}%`,
            marginLeft: "auto",
            marginRight: "auto",
            boxShadow: `0 0 24px ${accent}`,
          }}
        />
      </div>
    </ZoneBox>
  );
};

// Caption card in the caption zone (centered, above the audio strip).
const CaptionScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  const { frame, enter, appear } = useEnvelope(layer.duration_frames, layer.fade);
  const yy = interpolate(enter, [0, 1], [18, 0]);
  const dur = Math.max(1, layer.duration_frames);
  const tstyle = transitionStyle(frame, dur, layer.transitionIn, layer.transitionOut);
  return (
    <ZoneBox zone="caption" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div
        style={{
          opacity: appear * tstyle.opacity,
          transform: `translateY(${yy}px)`,
          background: "rgba(10,12,20,0.72)",
          border: `1px solid ${accentOf(layer.type)}55`,
          padding: "16px 34px",
          borderRadius: 12,
          maxWidth: "88%",
          textAlign: "center",
        }}
      >
        <div
          style={{
            color: BONE,
            fontFamily: "system-ui, sans-serif",
            fontWeight: 600,
            fontSize: 44,
            lineHeight: 1.12,
            letterSpacing: "-0.01em",
          }}
        >
          {truncateLabel(layer.text || "Caption", 96)}
        </div>
      </div>
    </ZoneBox>
  );
};

// ── Consolidated audio-presence: one fixed, non-overlapping row per audio layer ─
const AudioRowStrip: React.FC<{ layer: TimelineLayer; slot: number }> = ({ layer, slot }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const row = audioRow(slot, width, height);
  const accent = accentOf(layer.type);
  const vol = typeof layer.volume === "number" ? Math.min(1, Math.max(0, layer.volume)) : 1;
  const dur = Math.max(1, layer.duration_frames);
  const { appear } = useEnvelope(layer.duration_frames, layer.fade);
  const volAt = (f: number) => {
    let v = vol;
    if (layer.fade?.inFrames) v *= interpolate(f, [0, layer.fade.inFrames], [0, 1], { extrapolateRight: "clamp" });
    if (layer.fade?.outFrames)
      v *= interpolate(f, [dur - layer.fade.outFrames, dur], [1, 0], { extrapolateLeft: "clamp" });
    return Math.min(1, Math.max(0, v));
  };
  const name = layer.type === "narration" ? "◗ narration" : layer.type === "music" ? "♪ music" : "♪ sfx";
  const bars = 40;
  return (
    <div
      style={{
        position: "absolute",
        left: row.left,
        top: row.top,
        width: row.width,
        height: row.height,
        display: "flex",
        alignItems: "center",
        gap: 16,
        opacity: appear,
      }}
    >
      {isLoadableUrl(layer.source) ? <Audio src={layer.source} volume={volAt} /> : null}
      {/* label chip — fixed width, truncated, clamped percentage (never 4090%) */}
      <div
        style={{
          flex: "0 0 auto",
          display: "flex",
          alignItems: "center",
          gap: 10,
          color: accent,
          fontFamily: "ui-monospace, monospace",
          fontSize: 20,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          background: "rgba(0,0,0,0.35)",
          border: `1px solid ${accent}55`,
          borderRadius: 999,
          padding: "6px 16px",
          whiteSpace: "nowrap",
        }}
      >
        <span>{truncateLabel(name, 16)}</span>
        <span style={{ color: BONE, opacity: 0.85 }}>{volumePercent(layer.volume)}</span>
      </div>
      {/* waveform fills the rest of the row */}
      <div style={{ flex: 1, display: "flex", gap: 4, alignItems: "flex-end", height: Math.min(38, row.height - 8) }}>
        {Array.from({ length: bars }).map((_, i) => {
          const h = 6 + (Math.sin(i * 0.55 + frame * 0.22) * 0.5 + 0.5) * (row.height - 14) * (0.4 + 0.6 * vol);
          return <div key={i} style={{ flex: 1, height: Math.max(3, h), borderRadius: 2, background: accent, opacity: 0.9 }} />;
        })}
      </div>
    </div>
  );
};

const AudioPresence: React.FC<{ layers: TimelineLayer[] }> = ({ layers }) => (
  <>
    {layers.map((l, i) => (
      <Sequence
        key={l.id}
        from={Math.max(0, l.start_frame)}
        durationInFrames={Math.max(1, l.duration_frames)}
        name={`audio:${l.type}:${l.id}`}
      >
        <AudioRowStrip layer={l} slot={i} />
      </Sequence>
    ))}
  </>
);

// Elegant title card when the timeline has no visible layers yet.
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
          {truncateLabel(meta?.title || "Untitled project", 40)}
        </div>
        <div style={{ height: 4, background: AMBER, width: `${line * 180}px`, margin: "26px auto 0", borderRadius: 2, boxShadow: `0 0 22px ${AMBER}` }} />
        <div style={{ color: DIM, fontFamily: "ui-monospace, monospace", fontSize: 26, marginTop: 26, letterSpacing: "0.06em" }}>
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
      { id: "ttl", type: "text", start_frame: 20, duration_frames: 200, z: 2, title: "Timeline Preview" },
    ],
  },
  meta: { title: "Timeline Preview", targetFormatted: "0:08", pipeline: "animation" },
};

// One non-audio layer → its zone-placed scene(s).
const VisualScene: React.FC<{ layer: TimelineLayer }> = ({ layer }) => {
  if (layer.type === "caption") return <CaptionScene layer={layer} />;
  if (isText(layer.type)) return <TitleScene layer={layer} />;
  // Visual (video/image/shape): full-frame background + badge + optional lower-third.
  const media = (layer.type === "video" || layer.type === "image") && isLoadableUrl(layer.source);
  return (
    <>
      <BackgroundFill layer={layer} media={media} />
      <Badge layer={layer} />
      {layer.text ? <LowerThird layer={layer} /> : null}
    </>
  );
};

export const TimelineFrame: React.FC<TimelineFrameProps> = ({ timeline, meta }) => {
  const all = (timeline?.layers || []).filter((l) => l && l.enabled !== false);
  const visual = all.filter((l) => !AUDIO.has(l.type)).slice().sort((a, b) => (a.z || 0) - (b.z || 0));
  const audio = all.filter((l) => AUDIO.has(l.type));
  const hasVisible = visual.length > 0;

  return (
    <AbsoluteFill>
      <Backdrop />
      {visual.map((l) => (
        <Sequence
          key={l.id}
          from={Math.max(0, l.start_frame)}
          durationInFrames={Math.max(1, l.duration_frames)}
          name={`${l.type}:${l.id}`}
        >
          <VisualScene layer={l} />
        </Sequence>
      ))}
      <AudioPresence layers={audio} />
      {!hasVisible ? <TitleCard meta={meta} /> : null}
    </AbsoluteFill>
  );
};
