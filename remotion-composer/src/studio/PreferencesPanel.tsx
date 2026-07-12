// Learned-style preferences panel (Studio "Style" tab) + blocker-option intent.
//
// Extracted from the former BrainPanel: production RUN status now lives entirely
// in the canonical <ProductionInspector> / <CommandCenter> (fed by /status). This
// file carries ONLY the style-learning UI and the blocker-intent classifier — no
// independent brain/run state labels or driver fixture names.

import React, { useCallback, useEffect, useState } from "react";
import { BacklotClient } from "../composition/client";
import { prefId, PreferencesPayload, StylePreference } from "../composition/brain";

const C = {
  border: "#232329",
  text: "#ececef",
  dim: "#a0a0a9",
  faint: "#5f5f68",
  green: "#4fc283",
  amber: "#e8c07d",
  red: "#e5544b",
  blue: "#6aa1ff",
};
const btn: React.CSSProperties = {
  background: "#16161a", color: C.text, border: `1px solid ${C.border}`,
  borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer",
};
const mini: React.CSSProperties = { ...btn, padding: "3px 8px", fontSize: 11 };
const chip = (color: string): React.CSSProperties => ({
  fontSize: 10, color, border: `1px solid ${color}`, borderRadius: 999,
  padding: "2px 8px", textTransform: "uppercase", letterSpacing: "0.06em",
});
const label: React.CSSProperties = { fontSize: 11, color: C.dim, letterSpacing: "0.06em", margin: "10px 0 6px" };

// Classify a blocker option label into the control it should invoke. `cancel`
// MUST take precedence over `retry` so a "Retry cancellation" option re-attempts
// the CANCEL, never a stage retry. Unknown labels → null (no automated action).
export function blockerOptionIntent(opt: string): "cancel" | "resume" | "retry" | null {
  if (/cancel/i.test(opt)) return "cancel";
  if (/resume/i.test(opt)) return "resume";
  if (/retry/i.test(opt)) return "retry";
  return null;
}

export const PreferencesPanel: React.FC<{ client: BacklotClient }> = ({ client }) => {
  const [payload, setPayload] = useState<PreferencesPayload | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setPayload(await client.getPreferences("all"));
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    }
  }, [client]);
  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (fn: () => Promise<unknown>, ok: string) => {
      setBusy(true);
      setNotice(null);
      try {
        await fn();
        setNotice(ok);
        await load();
      } catch (e) {
        setNotice(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  if (!payload) return <div style={{ color: C.faint, fontSize: 12 }}>Loading learned preferences…</div>;

  const renderScope = (scope: "global" | "project", block?: { opted_out: boolean; preferences: StylePreference[] }) => {
    if (!block) return null;
    return (
      <div style={{ marginBottom: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={label}>{scope.toUpperCase()} PREFERENCES</div>
          <span style={{ fontSize: 10, color: block.opted_out ? C.red : C.faint }}>
            {block.opted_out ? "learning OFF" : "learning on"}
          </span>
          <button style={mini} disabled={busy} onClick={() => act(() => client.updatePreference({ action: "opt_out", scope, opted_out: !block.opted_out }), "Opt-out updated.")}>
            {block.opted_out ? "Enable" : "Opt out"}
          </button>
          <button style={mini} disabled={busy} onClick={() => act(() => client.resetPreferences(scope), "Reset.")}>
            Reset
          </button>
        </div>
        {block.preferences.length === 0 ? (
          <div style={{ fontSize: 10, color: C.faint }}>No {scope} preferences learned.</div>
        ) : (
          block.preferences.map((p) => {
            const id = prefId(p);
            const prov = p.provenance || {};
            const verified = !!prov.verified;
            return (
              <div key={id} style={{ border: `1px solid ${C.border}`, borderRadius: 6, padding: 8, marginBottom: 6 }}>
                <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={chip(C.blue)}>{p.category}</span>
                  <b style={{ fontSize: 11 }}>{p.key}</b>
                  <span style={{ fontSize: 11, color: C.text }}>= {JSON.stringify(p.value)}</span>
                  <span style={{ fontSize: 9, color: C.faint }}>conf {Math.round((p.confidence ?? 0) * 100)}%</span>
                  <span style={chip(p.status === "applied" ? C.green : C.faint)}>{p.status ?? "applied"}</span>
                  {verified ? <span style={chip(C.green)}>verified</span> : <span style={chip(C.amber)}>unverified</span>}
                </div>
                <div style={{ fontSize: 9, color: C.faint, marginTop: 3 }}>
                  source {prov.source ?? "—"} · run {prov.run_id ?? "—"} · stage {prov.stage ?? "—"} · decision {prov.decision_ref ?? "—"}
                </div>
                <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                  <button
                    style={mini}
                    disabled={busy}
                    onClick={() => {
                      const v = window.prompt("Corrected value (JSON or text):", JSON.stringify(p.value));
                      if (v !== null)
                        void act(() => client.updatePreference({ action: "correct", scope, pref_id: id, value: parseVal(v) }), "Correction recorded.");
                    }}
                  >
                    Correct
                  </button>
                  <button style={{ ...mini, color: C.amber }} disabled={busy} onClick={() => act(() => client.updatePreference({ action: "reject", scope, pref_id: id }), "Rejected.")}>
                    Reject
                  </button>
                  <button style={{ ...mini, color: C.red }} disabled={busy} onClick={() => act(() => client.updatePreference({ action: "delete", scope, pref_id: id }), "Deleted.")}>
                    Delete
                  </button>
                  {scope === "project" && p.status === "applied" && verified ? (
                    <button style={{ ...mini, color: C.green }} disabled={busy} onClick={() => act(() => client.updatePreference({ action: "promote", scope: "project", pref_id: id }), "Promoted to global.")}>
                      Promote → global
                    </button>
                  ) : null}
                </div>
              </div>
            );
          })
        )}
      </div>
    );
  };

  return (
    <div style={{ fontSize: 12, color: C.text }}>
      <div style={{ fontSize: 10, color: C.faint, marginBottom: 4 }}>
        Learned style is auditable and reversible. `learn` is only recorded from a real approval/correction with verified
        anchors; global promotion only from a verified project preference.
      </div>
      {notice ? <div style={{ fontSize: 11, color: C.amber, marginBottom: 4 }}>{notice}</div> : null}
      {renderScope("project", payload.project)}
      {renderScope("global", payload.global)}
    </div>
  );
};

function parseVal(v: string): unknown {
  try {
    return JSON.parse(v);
  } catch {
    return v;
  }
}
