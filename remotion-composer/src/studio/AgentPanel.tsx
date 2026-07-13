// The in-Studio NATIVE Hermes Agent setup / connection panel.
//
// This is the ONLY connection surface. The agent is auto-detected on this
// machine — there is deliberately NO endpoint, token, project, or credential
// field anywhere. Everything renders from the canonical AgentConnection carried
// on /status (`connection`). All actions dispatch through the shared
// StatusController.runAction so there is a single busy/error path:
//   - not_installed → install guidance + "Re-check for Hermes"  (POST /api/agent/connect re-probes)
//   - detected / ready (available:false) → "Connect Hermes Agent" (POST /api/agent/connect)
//   - connected (available:true)         → "Hermes Agent connected" + "Disconnect"
//   - unknown / not configured           → clean "integration not configured" state
// Manual editing (timeline + render) stays fully available in every state.

import React from "react";
import { StatusAction } from "../composition/status";
import { StatusController } from "./useStatusView";

const C = {
  text: "#ececef",
  dim: "#a0a0a9",
  faint: "#5f5f68",
  green: "#4fc283",
  amber: "#e8c07d",
  blue: "#6aa1ff",
  red: "#e5544b",
  line: "#232329",
};

const CONNECT: StatusAction = { id: "connect_agent", label: "Connect Hermes Agent", owner: "user", kind: "connect", advances_production: false };
const RECHECK: StatusAction = { id: "connect_agent", label: "Re-check for Hermes", owner: "user", kind: "connect", advances_production: false };
const DISCONNECT: StatusAction = { id: "disconnect_agent", label: "Disconnect", owner: "user", kind: "disconnect", advances_production: false };

export const AgentPanel: React.FC<{ status: StatusController }> = ({ status }) => {
  const { view, busy, runAction } = status;
  const conn = view?.connection;

  if (!conn) {
    return (
      <div style={box} data-testid="agent-panel">
        <Dot color={C.faint} />
        <span style={{ color: C.faint }}>Checking this machine for the Hermes Agent…</span>
      </div>
    );
  }

  const version = conn.version ? ` · v${conn.version}` : "";
  let dot = C.faint;
  let headline = conn.headline || conn.server_name;
  let detail = conn.detail;
  let action: StatusAction | null = null;

  switch (conn.status) {
    case "connected":
      dot = C.green;
      headline = conn.headline || "Hermes Agent connected";
      action = DISCONNECT;
      break;
    case "detected":
    case "ready":
      dot = C.amber;
      headline = conn.headline || "Hermes Agent detected on this machine";
      action = CONNECT;
      break;
    case "not_installed":
      dot = C.red;
      headline = conn.headline || "Hermes Agent is not installed";
      detail = conn.detail || "Install the Hermes Agent on this machine to run production. You can keep editing the timeline and rendering locally without it.";
      action = RECHECK;
      break;
    default: // "unknown" — integration not configured
      dot = C.faint;
      headline = "Hermes Agent integration not configured";
      detail = conn.detail || "No production agent is configured. Manual timeline editing and local rendering are fully available.";
      action = RECHECK;
      break;
  }

  return (
    <div style={wrap} data-testid="agent-panel" data-agent-status={conn.status}>
      <div style={row}>
        <Dot color={dot} />
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: C.text }} data-testid="agent-headline">
            {headline}
            <span style={{ color: C.faint, fontWeight: 400, fontFamily: "ui-monospace, monospace", fontSize: 11 }}>{version}</span>
          </div>
          {detail ? <div style={{ fontSize: 11, color: C.dim, marginTop: 2, lineHeight: 1.4 }}>{detail}</div> : null}
        </div>
        {action ? (
          <button
            style={conn.status === "connected" ? btnGhost : btnConnect}
            disabled={busy}
            data-testid={action.id === "disconnect_agent" ? "agent-disconnect" : conn.status === "not_installed" || conn.status === "unknown" ? "agent-recheck" : "agent-connect"}
            onClick={() => void runAction(action!)}
          >
            {action.label}
          </button>
        ) : null}
      </div>
    </div>
  );
};

const Dot: React.FC<{ color: string }> = ({ color }) => (
  <span style={{ width: 9, height: 9, borderRadius: 9, background: color, flex: "0 0 auto", marginTop: 4 }} />
);

const wrap: React.CSSProperties = {
  marginBottom: 12,
  padding: "10px 13px",
  borderRadius: 9,
  background: "#101013",
  border: `1px solid ${C.line}`,
};
const box: React.CSSProperties = { ...wrap, display: "flex", alignItems: "center", gap: 8, fontSize: 12 };
const row: React.CSSProperties = { display: "flex", alignItems: "flex-start", gap: 10 };
const btnConnect: React.CSSProperties = {
  marginLeft: "auto", flex: "0 0 auto", alignSelf: "center",
  background: C.blue, color: "#06122b", border: "none", borderRadius: 8,
  padding: "7px 13px", fontSize: 12, fontWeight: 650, cursor: "pointer",
};
const btnGhost: React.CSSProperties = {
  marginLeft: "auto", flex: "0 0 auto", alignSelf: "center",
  background: "transparent", color: C.dim, border: `1px solid ${C.line}`,
  borderRadius: 8, padding: "7px 13px", fontSize: 12, cursor: "pointer",
};
