import React, { useCallback, useEffect, useRef, useState } from "react";
import { BacklotClient } from "../composition/client";
import {
  BrainEvent,
  BrainOutput,
  BrainState,
  isLive,
  orchestrationKind,
  prefId,
  PreferencesPayload,
  StylePreference,
  TERMINAL_RUN_STATES,
} from "../composition/brain";

// ── shared style tokens (match StudioApp) ──
const C = {
  panel: "#0c0c0f",
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
  background: "#16161a",
  color: C.text,
  border: `1px solid ${C.border}`,
  borderRadius: 6,
  padding: "5px 10px",
  fontSize: 12,
  cursor: "pointer",
};
const mini: React.CSSProperties = { ...btn, padding: "3px 8px", fontSize: 11 };
const chip = (color: string): React.CSSProperties => ({
  fontSize: 10,
  color,
  border: `1px solid ${color}`,
  borderRadius: 999,
  padding: "2px 8px",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
});
const label: React.CSSProperties = { fontSize: 11, color: C.dim, letterSpacing: "0.06em", margin: "10px 0 6px" };

const STAGE_STATUS_COLOR: Record<string, string> = {
  pending: C.faint,
  active: C.blue,
  blocked: C.red,
  awaiting_approval: C.amber,
  done: C.green,
  failed: C.red,
  skipped: C.faint,
};
const RUN_STATE_LABEL: Record<string, string> = {
  not_started: "NOT STARTED",
  running: "RUNNING",
  awaiting_approval: "AWAITING APPROVAL",
  blocked: "BLOCKED",
  cancelling: "CANCELLATION PENDING",
  cancelled: "CANCELLED",
  failed: "FAILED",
  completed: "COMPLETED",
};
const RUN_STATE_COLOR: Record<string, string> = {
  not_started: C.faint,
  running: C.green,
  awaiting_approval: C.amber,
  blocked: C.red,
  cancelling: C.amber,
  cancelled: C.faint,
  failed: C.red,
  completed: C.green,
};

// Classify a blocker option label into the control it should invoke. `cancel`
// MUST take precedence over `retry` so a "Retry cancellation" option re-attempts
// the CANCEL, never a stage retry. Unknown labels → null (no automated action).
export function blockerOptionIntent(opt: string): "cancel" | "resume" | "retry" | null {
  if (/cancel/i.test(opt)) return "cancel";
  if (/resume/i.test(opt)) return "resume";
  if (/retry/i.test(opt)) return "retry";
  return null;
}

function fmtElapsed(sec: number | null | undefined): string {
  if (typeof sec !== "number") return "—";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

// ── live brain hook: cursor-paged events + state + assets, SSE-triggered poll ──
export function useBrain(client: BacklotClient) {
  const [state, setState] = useState<BrainState | null>(null);
  const [events, setEvents] = useState<BrainEvent[]>([]);
  const [assets, setAssets] = useState<BrainOutput[]>([]);
  const [usedFixture, setUsedFixture] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cursorRef = useRef(0);

  const tick = useCallback(async () => {
    try {
      const s = await client.getBrain();
      setState(s);
      setUsedFixture(client.usedFixture);
      const page = await client.getBrainEvents(cursorRef.current, 200);
      if (page.events.length) {
        setEvents((prev) => [...prev, ...page.events].slice(-400));
        cursorRef.current = page.next_cursor;
      }
      const a = await client.getBrainAssets();
      setAssets(a.outputs || []);
    } catch {
      /* tolerant — reads never throw fatally in fixture mode */
    }
  }, [client]);

  useEffect(() => {
    void tick();
    const id = setInterval(() => void tick(), 3000);
    let es: EventSource | null = null;
    if (typeof EventSource !== "undefined") {
      try {
        es = new EventSource(`/api/project/${client.projectId}/events`);
        es.onmessage = () => void tick();
      } catch {
        /* offline */
      }
    }
    return () => {
      clearInterval(id);
      es?.close();
    };
  }, [client.projectId, tick]);

  const control = useCallback(
    async (fn: () => Promise<BrainState>, ok: string) => {
      setBusy(true);
      setNotice(null);
      try {
        const s = await fn();
        setState(s);
        setNotice(ok);
        await tick();
      } catch (e) {
        setNotice(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [tick],
  );

  return { state, events, assets, usedFixture, notice, setNotice, busy, control, tick };
}

// ── The observable production panel ──
export const BrainPanel: React.FC<{ client: BacklotClient }> = ({ client }) => {
  const { state, events, assets, usedFixture, notice, busy, control } = useBrain(client);
  if (!state) return <div style={{ color: C.faint, fontSize: 12 }}>Loading production brain…</div>;

  const live = isLive(state, usedFixture);
  const kind = orchestrationKind(state);
  const runId = state.run_id;
  const jobId = state.brain?.job_id ?? null;
  const terminal = TERMINAL_RUN_STATES.has(state.state);
  const pendingApprovals = state.approvals.filter((a) => a.status === "pending");
  const openBlockers = state.blockers.filter((b) => !b.resolved);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: C.text }}>
      {/* LIVE vs FIXTURE + run state */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={chip(live ? C.green : C.amber)} data-testid="live-badge">
          {live ? "● LIVE" : "◐ DETERMINISTIC FIXTURE"}
        </span>
        <span style={chip(RUN_STATE_COLOR[state.state] || C.faint)} data-testid="run-state">
          {RUN_STATE_LABEL[state.state] || state.state}
        </span>
        <span style={{ fontSize: 10, color: C.faint }}>{kind === "external_job" ? "external_job" : "fake_driver"}</span>
      </div>
      {state.state === "cancelling" ? (
        <div style={{ fontSize: 11, color: C.amber }}>
          Cancellation requested — awaiting external orchestrator acknowledgment. Not cancelled yet; retry available.
        </div>
      ) : null}

      {/* brain identity — real IDs, never fabricated */}
      <div style={{ fontSize: 10, color: C.dim, marginTop: 2, lineHeight: 1.5 }}>
        <div>brain: {state.brain?.name ?? "—"} · engine {state.brain?.engine ?? "—"}</div>
        <div>agent {state.brain?.agent_id ?? "—"} · session {state.brain?.session_id ?? "—"}</div>
        <div>run {runId ?? "—"} · job {jobId ?? "—"}</div>
        <div>
          activity: <span style={{ color: C.text }}>{state.activity}</span>
        </div>
        <div>
          events {state.counts.events} · tools {state.counts.tool_calls} · decisions {state.counts.decisions} · outputs{" "}
          {state.counts.outputs}
        </div>
      </div>

      {notice ? <div style={{ fontSize: 11, color: C.amber, marginTop: 4 }}>{notice}</div> : null}

      {/* Controls — Start fail-closed; Cancel/Retry/Resume use exact handles */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
        {state.state === "not_started" ? (
          <button style={{ ...btn, borderColor: C.green, color: C.green }} disabled={busy} onClick={() => control(() => client.startRun(), "Production run started.")}>
            ▶ Start production
          </button>
        ) : null}
        {!terminal && state.state !== "not_started" ? (
          <button style={{ ...btn, borderColor: C.red, color: C.red }} disabled={busy} onClick={() => runId && control(() => client.cancelRun(runId), "Cancellation requested.")}>
            ✕ Cancel
          </button>
        ) : null}
        {(state.state === "blocked" || state.state === "cancelling") && state.current_stage ? (
          <button style={mini} disabled={busy} onClick={() => control(() => client.retryStage(state.current_stage as string, runId, jobId), "Retry requested.")}>
            ↻ Retry {state.current_stage}
          </button>
        ) : null}
        {state.state === "blocked" ? (
          <button style={mini} disabled={busy} onClick={() => control(() => client.resumeRun(runId, jobId), "Resume requested.")}>
            ▸ Resume
          </button>
        ) : null}
      </div>

      {/* Pending approvals */}
      {pendingApprovals.length ? (
        <>
          <div style={label}>PENDING APPROVALS</div>
          {pendingApprovals.map((a) => (
            <div key={a.approval_id} style={{ border: `1px solid ${C.amber}55`, borderRadius: 6, padding: 8, marginBottom: 6 }}>
              <div style={{ fontSize: 11 }}>{a.prompt || `Approve ${a.stage}?`}</div>
              <div style={{ fontSize: 10, color: C.faint }}>stage: {a.stage} · id: {a.approval_id}</div>
              <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                <button style={{ ...mini, color: C.green }} disabled={busy} onClick={() => runId && control(() => client.approveRun(runId, { approvalId: a.approval_id, stage: a.stage ?? undefined }), "Approved.")}>
                  Approve
                </button>
                <button style={{ ...mini, color: C.red }} disabled={busy} onClick={() => runId && control(() => client.rejectRun(runId, { approvalId: a.approval_id, stage: a.stage ?? undefined }), "Rejected.")}>
                  Reject
                </button>
              </div>
            </div>
          ))}
        </>
      ) : null}

      {/* Open blockers with actions */}
      {openBlockers.length ? (
        <>
          <div style={label}>BLOCKERS</div>
          {openBlockers.map((b) => (
            <div key={b.blocker_id} style={{ border: `1px solid ${C.red}55`, borderRadius: 6, padding: 8, marginBottom: 6 }}>
              <div style={{ fontSize: 11, color: C.red }}>{b.kind}</div>
              <div style={{ fontSize: 11 }}>{b.message}</div>
              {b.options?.length ? (
                <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                  {b.options.map((opt, i) => {
                    // Route by intent. `cancel` MUST win over `retry` because a
                    // "Retry cancellation" option is a cancel re-attempt, not a
                    // stage retry. Unsupported options are shown disabled (no-op).
                    const runnable = runId !== null;
                    const intent = blockerOptionIntent(opt);
                    let handler: (() => void) | null = null;
                    if (intent === "cancel" && runId) {
                      handler = () => control(() => client.cancelRun(runId), "Cancellation re-requested.");
                    } else if (intent === "resume" && runId) {
                      handler = () => control(() => client.resumeRun(runId, jobId), "Resume requested.");
                    } else if (intent === "retry" && b.stage) {
                      handler = () => control(() => client.retryStage(b.stage as string, runId, jobId), "Retry requested.");
                    }
                    const supported = handler !== null && runnable;
                    return (
                      <button
                        key={i}
                        style={{ ...mini, opacity: supported ? 1 : 0.5, cursor: supported ? "pointer" : "not-allowed" }}
                        disabled={busy || !supported}
                        title={supported ? undefined : "This blocker option has no automated action."}
                        onClick={() => handler?.()}
                      >
                        {opt}
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>
          ))}
        </>
      ) : null}

      {/* 11 stages */}
      <div style={label}>STAGES (11)</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {state.stages.map((s) => {
          const isCur = state.current_stage === s.id;
          return (
            <div
              key={s.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "3px 6px",
                borderRadius: 5,
                background: isCur ? "#16161a" : "transparent",
                border: `1px solid ${isCur ? C.border : "transparent"}`,
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: 8, background: STAGE_STATUS_COLOR[s.status] || C.faint, flex: "0 0 auto" }} />
              <span style={{ flex: 1, fontSize: 11, color: isCur ? C.text : C.dim }}>{s.title}</span>
              <span style={{ fontSize: 9, color: C.faint }}>{Math.round((s.progress || 0) * 100)}%</span>
              <span style={{ fontSize: 9, color: C.faint, minWidth: 34, textAlign: "right" }}>{s.status}</span>
            </div>
          );
        })}
      </div>

      {/* current task/tool/provider (from active stage) */}
      {(() => {
        const cur = state.stages.find((s) => s.id === state.current_stage);
        if (!cur) return null;
        return (
          <div style={{ fontSize: 10, color: C.dim, marginTop: 4 }}>
            current: <b style={{ color: C.text }}>{cur.title}</b>
            {cur.tool ? ` · tool ${cur.tool}` : ""}
            {cur.provider ? ` · provider ${cur.provider}` : ""}
            {cur.job_id ? ` · job ${cur.job_id}` : ""}
            {cur.elapsed_seconds != null ? ` · ${fmtElapsed(cur.elapsed_seconds)}` : ""}
            {cur.latest_activity ? <div style={{ color: C.faint }}>{cur.latest_activity}</div> : null}
          </div>
        );
      })()}

      {/* assets/outputs arriving */}
      <div style={label}>ASSETS / OUTPUTS ({assets.length})</div>
      {assets.length ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 2, maxHeight: 120, overflowY: "auto" }}>
          {assets.slice(-12).map((o, i) => (
            <div key={i} style={{ fontSize: 10, color: C.dim, display: "flex", gap: 6 }}>
              <span style={chip(C.blue)}>{o.kind}</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {o.label || o.path || "output"}
              </span>
              <span style={{ color: C.faint }}>{o.stage ?? ""}</span>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ fontSize: 10, color: C.faint }}>No assets produced yet.</div>
      )}

      {/* append-only activity history (cursor) */}
      <div style={label}>ACTIVITY (cursor {events.length ? events[events.length - 1].seq : 0})</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1, maxHeight: 140, overflowY: "auto" }}>
        {events.slice(-24).map((e) => (
          <div key={e.seq} style={{ fontSize: 10, color: C.faint, display: "flex", gap: 6 }}>
            <span style={{ color: C.faint, minWidth: 34 }}>#{e.seq}</span>
            <span style={{ color: C.dim, minWidth: 90 }}>{e.type}</span>
            <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {e.message || e.stage || ""}
              {e.redacted ? " ·[redacted]" : ""}
            </span>
          </div>
        ))}
        {events.length === 0 ? <div style={{ fontSize: 10, color: C.faint }}>No events yet.</div> : null}
      </div>
    </div>
  );
};

// ── Style learning / preferences panel ──
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
