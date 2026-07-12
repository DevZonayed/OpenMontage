import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig, Sequence } from "remotion";

// A FREE, self-contained preview animatic. It summarizes an approved production
// plan (title, target duration, frame budget, pipeline, provider readiness,
// section markers) as a short motion card sequence. No external assets, no
// network, no paid media — it is honestly a PREVIEW of the plan, not the final
// generated film (which is agent-driven).

export interface PreviewAnimaticProps {
  title: string;
  pipeline: string;
  targetFormatted: string;
  totalFrames: number;
  wordBudget: number;
  sections: string[];
  providersConfigured: number;
  providersTotal: number;
  runtimes: string[];
}

const BG = "#070B16";
const AMBER = "#F2A73B";
const BONE = "#E9E4D6";
const DIM = "#93A0B4";
const MONO = "'IBM Plex Mono', ui-monospace, monospace";
const DISP = "'Space Grotesk', 'Inter', system-ui, sans-serif";

const fadeUp = (frame: number, start: number, fps: number, dist = 24) => {
  const p = spring({ frame: frame - start, fps, config: { damping: 200 } });
  return { opacity: p, transform: `translateY(${interpolate(p, [0, 1], [dist, 0])}px)` };
};

export const PreviewAnimatic: React.FC<PreviewAnimaticProps> = ({
  title, pipeline, targetFormatted, totalFrames, wordBudget, sections,
  providersConfigured, providersTotal, runtimes,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const outroStart = durationInFrames - fps * 3;

  return (
    <AbsoluteFill style={{ backgroundColor: BG, fontFamily: DISP, color: BONE,
      justifyContent: "center", alignItems: "center", padding: 96 }}>
      {/* persistent watermark */}
      <div style={{ position: "absolute", top: 54, left: 64, fontFamily: MONO, fontSize: 20,
        letterSpacing: "0.28em", textTransform: "uppercase", color: DIM }}>
        Backlot · preview animatic
      </div>

      {/* Scene 1 — title */}
      <Sequence durationInFrames={Math.round(fps * 3.4)}>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", textAlign: "center" }}>
          <div style={{ ...fadeUp(frame, 4, fps), fontFamily: MONO, fontSize: 24, letterSpacing: "0.4em",
            textTransform: "uppercase", color: AMBER, marginBottom: 24 }}>Production plan</div>
          <div style={{ ...fadeUp(frame, 10, fps), fontWeight: 700, fontSize: 92, maxWidth: 1500, lineHeight: 1.05 }}>{title}</div>
          <div style={{ ...fadeUp(frame, 18, fps), marginTop: 22, fontFamily: MONO, fontSize: 26, color: DIM,
            letterSpacing: "0.12em" }}>{pipeline} · {targetFormatted}</div>
        </AbsoluteFill>
      </Sequence>

      {/* Scene 2 — plan stats */}
      <Sequence from={Math.round(fps * 3.2)} durationInFrames={Math.round(fps * 5.2)}>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
          <div style={{ display: "flex", gap: 64, flexWrap: "wrap", justifyContent: "center", maxWidth: 1500 }}>
            {[
              ["Target length", targetFormatted],
              ["Total frames", `${totalFrames}`],
              ["Narration budget", `≈ ${wordBudget} words`],
              ["Providers", `${providersConfigured} / ${providersTotal}`],
            ].map(([k, v], i) => (
              <div key={k} style={{ ...fadeUp(frame, Math.round(fps * 3.2) + 6 + i * 6, fps),
                display: "flex", flexDirection: "column", gap: 8, alignItems: "center" }}>
                <span style={{ fontFamily: MONO, fontSize: 18, color: DIM, textTransform: "uppercase", letterSpacing: "0.1em" }}>{k}</span>
                <b style={{ fontSize: 52, color: BONE }}>{v}</b>
              </div>
            ))}
          </div>
          <div style={{ ...fadeUp(frame, Math.round(fps * 5.0), fps), marginTop: 46, display: "flex", gap: 12, flexWrap: "wrap", justifyContent: "center" }}>
            {runtimes.map((r) => (
              <span key={r} style={{ fontFamily: MONO, fontSize: 20, padding: "6px 16px", borderRadius: 999,
                border: `1px solid ${AMBER}`, color: AMBER }}>{r}</span>
            ))}
          </div>
        </AbsoluteFill>
      </Sequence>

      {/* Scene 3 — section markers */}
      <Sequence from={Math.round(fps * 8.2)} durationInFrames={Math.max(1, outroStart - Math.round(fps * 8.2))}>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
          <div style={{ ...fadeUp(frame, Math.round(fps * 8.2) + 4, fps), fontFamily: MONO, fontSize: 22,
            letterSpacing: "0.3em", textTransform: "uppercase", color: DIM, marginBottom: 30 }}>Planned sections</div>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", justifyContent: "center", maxWidth: 1400 }}>
            {(sections.length ? sections : ["research", "proposal", "script", "scene_plan", "assets", "edit", "compose", "publish"]).slice(0, 8).map((sec, i) => (
              <span key={sec + i} style={{ ...fadeUp(frame, Math.round(fps * 8.2) + 10 + i * 4, fps),
                fontSize: 28, padding: "12px 22px", borderRadius: 12, background: "#12151C",
                border: "1px solid #232A36", color: BONE }}>{i + 1}. {sec}</span>
            ))}
          </div>
        </AbsoluteFill>
      </Sequence>

      {/* Scene 4 — honest outro */}
      <Sequence from={outroStart}>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", textAlign: "center" }}>
          <div style={{ ...fadeUp(frame, outroStart + 4, fps), fontSize: 40, maxWidth: 1300, lineHeight: 1.35, color: BONE }}>
            This is a free preview of your plan.
          </div>
          <div style={{ ...fadeUp(frame, outroStart + 12, fps), marginTop: 20, fontFamily: MONO, fontSize: 22,
            color: DIM, letterSpacing: "0.06em", maxWidth: 1200 }}>
            The full film is generated by your agent — approve providers &amp; the proposal to begin.
          </div>
        </AbsoluteFill>
      </Sequence>
    </AbsoluteFill>
  );
};

export const previewAnimaticDefaults: PreviewAnimaticProps = {
  title: "Untitled project",
  pipeline: "animation",
  targetFormatted: "1:00",
  totalFrames: 1800,
  wordBudget: 150,
  sections: [],
  providersConfigured: 0,
  providersTotal: 0,
  runtimes: [],
};
