// The dominant, single production-status card for the Remotion Studio.
//
// Driven by the SAME canonical /status view model the board uses, so the two
// surfaces always show the same NOW / NEXT / LAST, current stage, and one
// primary action. Network errors preserve the last known state and show a
// "reconnecting" indicator — they never fall back to fabricated data.

import React from "react";
import { StatusAction, StatusView } from "../composition/status";
import { StatusController } from "./useStatusView";
import { AgentPanel } from "./AgentPanel";

const OWNER_LABEL: Record<string, string> = { hermes: "Hermes", user: "You", system: "System" };
const STEP_GLYPH: Record<string, string> = {
  completed: "✓", current: "◉", blocked: "▲", awaiting: "◈", failed: "✕", skipped: "–", upcoming: "",
};
const STEP_COLOR: Record<string, { bg: string; bd: string; fg: string }> = {
  completed: { bg: "rgba(79,194,131,0.12)", bd: "#4fc283", fg: "#4fc283" },
  current: { bg: "#6aa1ff", bd: "#6aa1ff", fg: "#06122b" },
  awaiting: { bg: "rgba(240,168,60,0.14)", bd: "#f0a83c", fg: "#f0a83c" },
  blocked: { bg: "rgba(229,84,75,0.12)", bd: "#e5544b", fg: "#e5544b" },
  failed: { bg: "rgba(229,84,75,0.12)", bd: "#e5544b", fg: "#e5544b" },
  skipped: { bg: "#1c1c21", bd: "#232329", fg: "#5f5f68" },
  upcoming: { bg: "#1c1c21", bd: "#232329", fg: "#5f5f68" },
};

function fmtSecs(secs?: number | null): string {
  if (secs == null) return "";
  const s = Math.max(0, Math.round(secs));
  const m = Math.floor(s / 60), r = s % 60;
  return m ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
}

// Sanitized short id — never leak a full raw job/session handle in the UI.
function shortId(id?: string | null): string {
  if (!id) return "";
  return id.length > 10 ? id.slice(0, 8) : id;
}

export interface CommandCenterProps {
  status: StatusController;
}

export const CommandCenter: React.FC<CommandCenterProps> = ({ status }) => {
  const { view, coldError, busy, actionError, runAction } = status;

  if (!view) {
    return (
      <div style={s.loading}>
        {coldError
          ? "⟳ Production status unavailable — reconnecting…"
          : "Loading production status…"}
      </div>
    );
  }

  const stageTag = view.stage_number
    ? `Stage ${view.stage_number} of ${view.stage_count} · ${view.current_stage_label ?? ""}`
    : "";
  const pa = view.primary_action;

  return (
    <section style={s.card} aria-live="polite" data-testid="command-center" data-state={view.overall_state}>
      {/* ── DOMAIN A · PRODUCTION AGENT ──
          The native Hermes Agent connection + the run state / stage / owner below.
          Kept visually distinct from Timeline/Assets (domain B) and the Renderer
          (domain C) so the three are never conflated. */}
      <div style={s.domainLabel} data-testid="domain-agent">Production Agent</div>
      <AgentPanel status={status} />

      {(view.diagnostics || []).map((d, i) =>
        d.kind === "stale" ? (
          <div key={i} style={s.diagStale} role="status">⟳ {d.message}</div>
        ) : d.kind === "source_conflict" ? (
          <div key={i} style={s.diagConflict} role="status">⚠ {d.message}</div>
        ) : null,
      )}
      {actionError ? <div style={s.diagConflict}>⚠ {actionError}</div> : null}

      <div style={s.grid}>
        {/* NOW */}
        <div>
          <div style={s.eyebrow}>NOW</div>
          {stageTag ? <div style={s.stageTag}>{stageTag}</div> : null}
          <h2 style={s.headline} data-testid="cc-headline">{view.headline}</h2>
          {view.active_task ? <p style={s.task}>{view.active_task}</p> : null}
          <div style={s.meta}>
            {`Owner: ${OWNER_LABEL[view.owner] ?? view.owner}`}
            {typeof view.elapsed_seconds === "number" ? ` · Elapsed ${fmtSecs(view.elapsed_seconds)}` : ""}
          </div>
        </div>

        {/* NEXT — single primary action */}
        <div>
          <div style={s.eyebrow}>NEXT</div>
          <div style={s.ownerLine}>
            {pa.owner === "user" ? "Your move" : pa.owner === "hermes" ? "Hermes' move" : "In progress"}
          </div>
          {renderPrimary(view, pa, runAction, busy)}
          {view.why_waiting ? <p style={s.why}>{view.why_waiting}</p> : null}
          {(view.secondary_actions.length > 0 || view.stop_available) ? (
            <div style={s.secondaryRow}>
              {view.secondary_actions.map((a) => (
                <button key={a.id} style={s.btnGhost} disabled={busy} onClick={() => void runAction(a)}
                  title={a.hint ?? undefined}>
                  {a.label}
                </button>
              ))}
              {view.stop_available ? (
                <button style={s.btnDanger} disabled={busy}
                  onClick={() => void runAction({ id: "stop", label: "Stop", owner: "user", kind: "cancel", advances_production: false })}>
                  ■ Stop production
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* LATEST */}
        <div>
          <div style={s.eyebrow}>LATEST</div>
          {view.latest_event?.label ? (
            <p style={s.lastMsg}>{view.latest_event.label}</p>
          ) : (
            <p style={{ ...s.lastMsg, color: "#5f5f68" }}>No activity yet.</p>
          )}
          {view.is_live && (view.identity.tool || view.identity.provider || view.identity.job) ? (
            <div style={s.idChips}>
              {view.identity.tool ? <span style={s.idChip}>{view.identity.tool}</span> : null}
              {view.identity.provider ? <span style={s.idChip}>{view.identity.provider}</span> : null}
              {view.identity.job ? <span style={s.idChip} title={view.identity.job}>job {shortId(view.identity.job)}</span> : null}
            </div>
          ) : null}
          {view.is_fixture ? <div style={s.fixture} data-testid="cc-fixture">◐ Demo data — no live run</div> : null}
          {view.is_live ? <div style={{ ...s.fixture, color: "#4fc283" }} data-testid="cc-live">● LIVE</div> : null}
        </div>
      </div>

      {/* 11-stage stepper */}
      <nav style={s.stepper} aria-label="Production stages">
        {view.stages.map((st) => {
          const c = STEP_COLOR[st.status] || STEP_COLOR.upcoming;
          return (
            <div key={st.id} style={s.step} title={`${st.label} — ${st.status}`}>
              <span style={{ ...s.stepDot, background: c.bg, borderColor: c.bd, color: c.fg }}>
                {STEP_GLYPH[st.status] || String(st.index + 1)}
              </span>
              <span style={{ ...s.stepLabel, color: st.status === "current" ? "#ececef" : "#5f5f68" }}>
                {st.label}
              </span>
            </div>
          );
        })}
      </nav>

      {!view.render.renderable && view.render.reason
        && ["ready_to_produce", "producing", "awaiting_plan_approval"].includes(view.overall_state) ? (
        <div style={s.renderNote}>▤ {view.render.reason}</div>
      ) : null}
    </section>
  );
};

function renderPrimary(
  view: StatusView,
  pa: StatusAction,
  run: (a: StatusAction) => void,
  busy: boolean,
): React.ReactNode {
  const passive = pa.advances_production === false && pa.kind === "status";
  if (passive) {
    return <div style={s.primaryPassive} role="status">⟳ {pa.label}</div>;
  }
  const isHermes = pa.owner === "hermes";
  return (
    <button
      style={{ ...s.primary, ...(isHermes ? s.primaryHermes : {}) }}
      disabled={busy}
      data-testid="cc-primary"
      onClick={() => run(pa)}
    >
      {pa.label}
    </button>
  );
}

const s: Record<string, React.CSSProperties> = {
  card: {
    margin: "10px 14px", padding: "16px 18px", borderRadius: 12,
    background: "linear-gradient(180deg,#16161a,#101013)", border: "1px solid #232329",
  },
  loading: { margin: "10px 14px", padding: 14, color: "#5f5f68", fontSize: 12 },
  grid: { display: "grid", gridTemplateColumns: "2fr 1.3fr 1.1fr", gap: 22, alignItems: "start" },
  eyebrow: { fontFamily: "ui-monospace, monospace", fontSize: 10, letterSpacing: "0.22em", color: "#5f5f68", textTransform: "uppercase", marginBottom: 6 },
  stageTag: { display: "inline-block", fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#6aa1ff", background: "rgba(106,161,255,0.1)", border: "1px solid rgba(106,161,255,0.3)", borderRadius: 999, padding: "2px 10px", marginBottom: 8 },
  headline: { fontSize: 20, fontWeight: 650, lineHeight: 1.2, margin: "0 0 5px", color: "#ececef" },
  task: { fontSize: 13, color: "#a0a0a9", margin: "0 0 6px", lineHeight: 1.4 },
  meta: { fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#5f5f68" },
  ownerLine: { fontFamily: "ui-monospace, monospace", fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase", color: "#a0a0a9", marginBottom: 8 },
  primary: { width: "100%", background: "#f0a83c", color: "#1a1206", border: "none", borderRadius: 9, padding: "10px 16px", fontSize: 13, fontWeight: 650, cursor: "pointer" },
  primaryHermes: { background: "#6aa1ff", color: "#06122b" },
  primaryPassive: { display: "flex", alignItems: "center", gap: 8, background: "rgba(106,161,255,0.12)", color: "#6aa1ff", border: "1px solid rgba(106,161,255,0.3)", borderRadius: 9, padding: "10px 14px", fontSize: 12.5, fontWeight: 600 },
  why: { fontSize: 12, color: "#5f5f68", margin: "9px 0 0", lineHeight: 1.4 },
  secondaryRow: { display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 },
  btnGhost: { background: "transparent", color: "#a0a0a9", border: "1px solid #232329", borderRadius: 8, padding: "7px 11px", fontSize: 12, cursor: "pointer" },
  btnDanger: { background: "transparent", color: "#e5544b", border: "1px solid rgba(229,84,75,0.5)", borderRadius: 8, padding: "7px 11px", fontSize: 12, cursor: "pointer" },
  btnSmall: { background: "#1c1c21", color: "#ececef", border: "1px solid #232329", borderRadius: 8, padding: "6px 11px", fontSize: 12, cursor: "pointer" },
  lastMsg: { fontSize: 12.5, color: "#a0a0a9", margin: "0 0 5px", lineHeight: 1.4 },
  idChips: { display: "flex", flexWrap: "wrap", gap: 5, marginTop: 6 },
  idChip: { fontFamily: "ui-monospace, monospace", fontSize: 10, color: "#a0a0a9", background: "#1c1c21", border: "1px solid #232329", borderRadius: 5, padding: "2px 7px" },
  fixture: { fontFamily: "ui-monospace, monospace", fontSize: 10.5, color: "#5f5f68", marginTop: 6 },
  stepper: { display: "grid", gridTemplateColumns: "repeat(11,1fr)", gap: 5, marginTop: 16, paddingTop: 14, borderTop: "1px solid #1a1a1f" },
  step: { display: "flex", flexDirection: "column", alignItems: "center", gap: 5, textAlign: "center" },
  stepDot: { width: 24, height: 24, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "ui-monospace, monospace", fontSize: 10, fontWeight: 600, border: "1.5px solid #232329" },
  stepLabel: { fontSize: 9, lineHeight: 1.2 },
  renderNote: { marginTop: 12, fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#5f5f68", padding: "7px 11px", background: "#101013", border: "1px dashed #232329", borderRadius: 7 },
  domainLabel: { fontFamily: "ui-monospace, monospace", fontSize: 10, letterSpacing: "0.22em", textTransform: "uppercase", color: "#6aa1ff", marginBottom: 8 },
  diagStale: { display: "flex", alignItems: "center", gap: 8, marginBottom: 10, padding: "8px 12px", borderRadius: 8, fontSize: 12.5, background: "rgba(106,161,255,0.08)", border: "1px solid rgba(106,161,255,0.24)", color: "#a0a0a9" },
  diagConflict: { marginBottom: 10, padding: "8px 12px", borderRadius: 8, fontSize: 12.5, background: "rgba(229,84,75,0.12)", border: "1px solid rgba(229,84,75,0.4)", color: "#f0a89f" },
};
