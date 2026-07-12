// The Studio "Production" tab — a COMPACT view of the SAME canonical /status the
// command center shows. It never renders independent brain/run labels, driver
// fixture names, or a second Start/Connect button; the one primary action lives
// in the command center (this panel links to it). Diagnostics are behind an
// explicit disclosure and sanitized.

import React from "react";
import { StatusController } from "./useStatusView";

const OWNER_LABEL: Record<string, string> = { hermes: "Hermes", user: "You", system: "System" };
const C = { text: "#ececef", dim: "#a0a0a9", faint: "#5f5f68", green: "#4fc283", blue: "#6aa1ff", amber: "#e8c07d", line: "#232329" };

function shortId(id?: string | null): string {
  if (!id) return "";
  return id.length > 10 ? id.slice(0, 8) : id;
}

export const ProductionInspector: React.FC<{ status: StatusController }> = ({ status }) => {
  const { view, coldError } = status;
  if (!view) {
    return <div style={{ color: C.faint, fontSize: 12 }}>{coldError ? "⟳ Reconnecting to production…" : "Loading production…"}</div>;
  }

  const stageTag = view.stage_number ? `Stage ${view.stage_number} of ${view.stage_count}` : "";
  const conn = view.connection || {};
  const pa = view.primary_action;
  const owner = OWNER_LABEL[view.owner] ?? view.owner;
  const id = view.identity || {};

  return (
    <div style={{ fontSize: 12, color: C.text, display: "flex", flexDirection: "column", gap: 4 }} data-testid="production-inspector">
      {/* mode chip — canonical + sanitized. Only shown for a real live run or an
          explicit demo; never a raw driver/fixture label. */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {view.is_live ? <span style={chip(C.green)} data-testid="pi-live">● LIVE</span> : null}
        {(view.is_demo || view.is_fixture) ? <span style={chip(C.amber)}>◐ DEMO</span> : null}
        {view.stale ? <span style={chip(C.blue)}>⟳ RECONNECTING</span> : null}
      </div>

      {/* NOW — the canonical current stage/headline/owner (same as command center) */}
      {stageTag ? <div style={{ ...lbl }}>{stageTag}</div> : null}
      <div style={{ fontSize: 14, fontWeight: 600, lineHeight: 1.3 }} data-testid="pi-stage">
        {view.current_stage_label || "Not started"}
      </div>
      <div style={{ color: C.dim, lineHeight: 1.4 }} data-testid="pi-headline">{view.headline}</div>
      <div style={{ color: C.faint, fontFamily: "ui-monospace, monospace", fontSize: 11 }} data-testid="pi-owner">
        Owner: {owner}
        {view.target?.available ? ` · ${view.target.label}` : view.target ? ` · ${view.target.label}` : ""}
      </div>

      {/* latest activity (canonical, sanitized) */}
      <div style={{ ...lbl, marginTop: 10 }}>LATEST</div>
      <div style={{ color: C.dim, lineHeight: 1.4 }} data-testid="pi-latest">
        {view.latest_event?.label || view.active_task || "No activity yet."}
      </div>

      {/* real, sanitized handles only when a live run exists */}
      {view.is_live && (id.job || id.tool || id.provider) ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 6 }}>
          {id.tool ? <span style={idchip}>{id.tool}</span> : null}
          {id.provider ? <span style={idchip}>{id.provider}</span> : null}
          {id.job ? <span style={idchip} title={id.job}>job {shortId(id.job)}</span> : null}
        </div>
      ) : null}

      {/* artifacts produced so far (canonical outputs via stepper details) */}
      {view.stages.some((st) => st.status === "current" && st.detail) ? (
        <div style={{ color: C.faint, fontSize: 11, marginTop: 4 }}>
          {view.stages.find((st) => st.status === "current")?.detail}
        </div>
      ) : null}

      {/* connection status text (no button — the one action is in the command center) */}
      {!conn.available && conn.status && conn.status !== "unknown" && !view.is_live ? (
        <>
          <div style={{ ...lbl, marginTop: 10 }}>CONNECTION</div>
          <div style={{ color: C.amber, lineHeight: 1.4 }} data-testid="pi-conn">{conn.headline}</div>
          {conn.detail ? <div style={{ color: C.faint, fontSize: 11 }}>{conn.detail}</div> : null}
        </>
      ) : null}

      {/* Static reference to the page's ONE action (in the command center above).
          NOT a button — the inspector is status-only, exactly one production-next
          action exists across the page. */}
      {pa && pa.advances_production !== false ? (
        <div style={{ marginTop: 12, color: C.faint, fontSize: 11 }} data-testid="pi-next-ref">
          Next step: “{pa.label}” — use the action in the production status above.
        </div>
      ) : null}

      {/* diagnostics behind an explicit disclosure, sanitized */}
      <details style={{ marginTop: 12 }}>
        <summary style={{ cursor: "pointer", color: C.faint, fontSize: 11, letterSpacing: "0.06em" }}>
          Technical details
        </summary>
        <div style={{ marginTop: 6, color: C.faint, fontFamily: "ui-monospace, monospace", fontSize: 10.5, lineHeight: 1.6 }}>
          <div>state: {view.overall_state}</div>
          <div>source: {view.authoritative_source}</div>
          {view.run_id ? <div>run: {shortId(view.run_id)}</div> : null}
          {id.session ? <div>session: {shortId(id.session)}</div> : null}
          {id.engine && view.is_live ? <div>engine: {id.engine}</div> : null}
          {(view.diagnostics || []).map((d, i) => (
            <div key={i} style={{ color: C.amber }}>{d.kind}: {d.message}</div>
          ))}
        </div>
      </details>
    </div>
  );
};

const lbl: React.CSSProperties = { fontSize: 10, color: C.faint, letterSpacing: "0.14em", textTransform: "uppercase" };
const idchip: React.CSSProperties = { fontFamily: "ui-monospace, monospace", fontSize: 10, color: C.dim, background: "#1c1c21", border: `1px solid ${C.line}`, borderRadius: 5, padding: "2px 7px" };
function chip(color: string): React.CSSProperties {
  return { fontFamily: "ui-monospace, monospace", fontSize: 10.5, letterSpacing: "0.08em", color, border: `1px solid ${color}`, borderRadius: 5, padding: "2px 7px" };
}
