// Backlot project board — renders BoardState and stays live via SSE.

import {
  STAGE_ICONS, el, fmtAgo, fmtClock, fmtDuration, fmtMoney,
  getJSON, postJSON, mediaURL, subscribe, thumbURL, waveBars,
} from "/ui/lib.js";
import { wireNewProjectButtons } from "/ui/newproject.js";

// CSRF-guarded POST for run lifecycle actions.
let _CSRF = null;
async function mpost(url, body) {
  if (!_CSRF) _CSRF = (await getJSON("/api/csrf")).csrf;
  return postJSON(url, body || {}, { "X-OpenMontage-CSRF": _CSRF });
}
let runPollTimer = null;
let lastRunSig = null;   // only re-render the run panel when the run materially changes

const rawProjectPath = location.pathname.split("/p/")[1] || "";
const projectId = decodeURIComponent(rawProjectPath);
const encodedProjectId = encodeURIComponent(projectId);
const app = document.getElementById("app");
const modal = document.getElementById("modal");
const player = document.getElementById("player");

let state = null;
let selectedStage = null;   // stage drawer open for this stage name
let activeRender = 0;
let replay = null;          // {t0, t1, t, playing} — replay mode when non-null
let firstPaint = true;

// ---------------------------------------------------------------------------
// header slate
// ---------------------------------------------------------------------------

function renderSlate(s) {
  const board = s.storyboard;
  const chips = [
    el("span", { class: "chip" }, `${s.pipeline.pipeline_type} pipeline`),
    board && board.total_duration_seconds
      ? el("span", { class: "chip" }, `${board.scenes.length} scenes · ${fmtDuration(board.total_duration_seconds)}`)
      : null,
    s.style_playbook ? el("span", { class: "chip" }, s.style_playbook) : null,
  ];

  const awaiting = s.stages.find((x) => x.status === "awaiting_human");
  const inProgress = s.stages.find((x) => x.status === "in_progress");
  const stalled = s.stages.find((x) => x.stalled);
  let liveEl;
  if (awaiting) {
    liveEl = el("span", { class: "live" }, el("span", { class: "dot" }), "◈ AWAITING YOU");
  } else if (stalled) {
    liveEl = el("span", { class: "live", style: "color:var(--red)" },
      el("span", { class: "dot", style: "background:var(--red);animation:none" }), "⚠ STALLED?");
  } else if (s.live || inProgress) {
    liveEl = el("span", { class: "live" }, el("span", { class: "dot" }), "LIVE");
  } else {
    liveEl = el("span", { class: "live idle" }, el("span", { class: "dot" }),
      `IDLE${s.last_activity ? " · " + fmtAgo(s.last_activity).toUpperCase() : ""}`);
  }

  const cost = el("div", { class: "cost" });
  if (s.cost) {
    const spent = s.cost.total_spent_usd ?? 0;
    const budget = spent + (s.cost.budget_remaining_usd ?? 0);
    const hasBudget = s.cost.budget_remaining_usd != null;
    const pct = hasBudget && budget > 0 ? Math.min(100, (spent / budget) * 100) : 0;
    cost.append(el("div", { class: "nums" }, el("b", {}, fmtMoney(spent)),
      hasBudget ? el("span", {}, ` / ${fmtMoney(budget)}`) : ""));
    if (hasBudget) {
      cost.append(el("div", { class: "bar" }, el("i", {
        class: pct > 90 ? "crit" : pct > 75 ? "warn" : "", style: `width:${pct}%`,
      })));
    }
    cost.append(el("div", { class: "label" }, "generation spend"));
  }

  return el("header", { class: "slate" },
    el("div", { class: "clapper" }),
    el("div", {},
      el("a", { class: "wordmark", href: "/", style: "text-decoration:none" }, "Backlot"),
      el("h1", {}, s.title),
    ),
    ...chips,
    el("div", { class: "spacer" }),
    el("a", { class: "chip nav-chip primary", href: `/p/${encodedProjectId}/editor` }, "◫ OPEN TIMELINE"),
    el("button", { class: "chip nav-chip", type: "button", "data-new-project": "" }, "+ NEW PROJECT"),
    liveEl,
    cost,
  );
}

// ---- Production run lifecycle panel -----------------------------------------
// Replaces the old copy-prompt dead-end. Shows the REAL run state (from
// /api/project/<id>/run), a prominent START PRODUCTION action that launches a
// real local preflight/planning worker, live status, and STOP. The manual
// copy-prompt survives only inside a collapsed Advanced section.
const _ACTIVE_RUN_STATES = new Set(["starting", "running", "waiting_for_approval", "cancelling"]);

function _elapsed(startedAt) {
  if (!startedAt) return "";
  const secs = Math.max(0, Math.round((Date.now() - Date.parse(startedAt)) / 1000));
  const m = Math.floor(secs / 60), sVal = secs % 60;
  return `${m}:${String(sVal).padStart(2, "0")}`;
}

function advancedPrompt(s) {
  const box = el("pre", { class: "prompt-box", style: "margin-top:8px" }, "Loading your brief…");
  const copyBtn = el("button", { class: "set-mini", type: "button" }, "Copy production prompt");
  getJSON(`/api/project/${encodedProjectId}/intake`).then((intake) => {
    const brief = (intake && intake.brief) || "";
    const pipeline = (intake && intake.pipeline_type) || s.pipeline.pipeline_type;
    const prompt =
      `Produce the OpenMontage project "${s.title}" (id: ${projectId}, pipeline: ${pipeline}).\n\n` +
      `Brief:\n${brief || "(no brief provided — ask me)"}\n\n` +
      `Follow AGENT_GUIDE.md: read the project's intake.json, run the mandatory preflight ` +
      `(provider_menu_summary), present concepts + the tool/cost plan, and PRESENT BOTH available ` +
      `composition runtimes for my approval before producing anything. Do not skip proposal/checkpoints.`;
    box.textContent = prompt;
    copyBtn.onclick = async () => {
      try { await navigator.clipboard.writeText(prompt); copyBtn.textContent = "Copied ✓"; }
      catch { copyBtn.textContent = "Copy failed — select the text"; }
    };
  }).catch(() => { box.textContent = "(brief unavailable)"; });
  const det = el("details", { class: "run-advanced", style: "margin-top:16px" });
  det.append(el("summary", { style: "cursor:pointer;color:var(--dim)" }, "Advanced — run it manually with your own agent"));
  det.append(box, el("div", { class: "np-actions", style: "justify-content:flex-start" }, copyBtn));
  return det;
}

function _timelineLink() {
  return el("a", { class: "set-mini", href: `/p/${encodedProjectId}/editor` }, "◫ Open timeline");
}

// The plan the preflight/planning worker actually produced (run_plan.json).
function renderPlanCard(plan) {
  const card = el("div", { class: "run-plan-card",
    style: "margin:12px 0;padding:14px 16px;background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:8px" });
  card.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--dim,#8a93a3);margin-bottom:10px" }, "Production plan"));
  const row = el("div", { style: "display:flex;gap:28px;flex-wrap:wrap;margin-bottom:12px" });
  const stat = (label, value) => row.append(el("div", { style: "display:flex;flex-direction:column;gap:2px" },
    el("span", { style: "font-size:11px;color:var(--dim,#8a93a3)" }, label),
    el("b", { style: "font-size:15px;color:var(--bone,#e9e4d6)" }, value)));
  stat("Target length", plan.target_formatted || "—");
  stat("Total frames", `${plan.total_frames ?? "—"} @ ${plan.fps ?? 30}fps`);
  stat("Narration budget", `≈ ${plan.word_budget ?? "—"} words`);
  const pr = plan.provider_readiness || {};
  stat("Providers configured", `${pr.capabilities_configured ?? "?"} of ${pr.capabilities_total ?? "?"}`);
  card.append(row);
  // composition runtimes available
  const rts = pr.composition_runtimes || {};
  const chips = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" });
  chips.append(el("span", { style: "font-size:12px;color:var(--dim,#8a93a3)" }, "Composition runtimes:"));
  for (const [name, ok] of Object.entries(rts)) {
    chips.append(el("span", {
      style: `font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid ${ok ? "var(--ok,#5ec27a)" : "var(--line,#232a36)"};color:${ok ? "var(--ok,#5ec27a)" : "var(--dim,#8a93a3)"}`,
    }, `${name} ${ok ? "✓" : "✗"}`));
  }
  card.append(chips);
  return card;
}

// Live activity feed — the real steps the run took (from run.log). Always visible
// (not a <details>) so you can see exactly what the preflight/planning did.
function renderActivityLog(run) {
  const log = run.log || [];
  if (!log.length) return null;
  const box = el("div", { class: "run-log",
    style: "margin:12px 0;max-height:150px;overflow:auto;background:var(--night-2,#0c1224);border:1px solid var(--line,#232a36);border-radius:8px;padding:10px 12px" });
  box.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--dim,#8a93a3);margin-bottom:6px" }, "Activity — what the run did"));
  for (const e of log.slice(-40)) {
    const line = el("div", { class: "mono", style: "font-size:12px;color:var(--cond,#93a0b4);padding:2px 0;display:flex;gap:8px" });
    line.append(el("span", { style: "color:var(--dim,#8a93a3);flex:0 0 auto" }, (e.ts || "").slice(11, 19)));
    line.append(el("span", {}, e.message || ""));
    box.append(line);
  }
  box.scrollTop = 1e6;
  return box;
}

// Expandable "what the models can do" — per-capability provider readiness.
function renderProviderDetail(plan) {
  const media = ((plan.provider_readiness || {}).media_capabilities) || [];
  if (!media.length) return null;
  const det = el("details", { class: "run-providers", style: "margin:12px 0" });
  det.append(el("summary", { style: "cursor:pointer;color:var(--dim,#8a93a3);font-size:13px" },
    "Providers & models — what this run can and can't generate"));
  const grid = el("div", { style: "margin-top:10px;display:grid;grid-template-columns:1fr;gap:6px" });
  for (const m of media) {
    const ok = m.configured > 0;
    const row = el("div", { style: "display:flex;align-items:center;gap:10px;font-size:13px" });
    row.append(el("span", { style: `flex:0 0 150px;color:${ok ? "var(--bone,#e9e4d6)" : "var(--dim,#8a93a3)"}` }, m.capability));
    row.append(el("span", { style: `flex:0 0 64px;color:${ok ? "var(--ok,#5ec27a)" : "var(--warn,#e8c07d)"}` }, `${m.configured}/${m.total}`));
    row.append(el("span", { style: "color:var(--cond,#93a0b4)" },
      (m.available_providers && m.available_providers.length) ? m.available_providers.join(", ") : "none configured — add a key in Settings"));
    grid.append(row);
  }
  det.append(grid);
  return det;
}

// Inline player for the free preview animatic once it exists.
function previewSection(run) {
  if (!run.preview || !run.preview.url) return null;
  const wrap = el("div", { style: "margin:14px 0" });
  wrap.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--amber,#e8c07d);margin-bottom:8px" },
    `▶ Preview animatic · ${run.preview.measured_seconds != null ? run.preview.measured_seconds + "s" : "ready"} · free`));
  wrap.append(el("video", { controls: "true", preload: "metadata", src: run.preview.url,
    style: "width:100%;max-width:680px;border-radius:8px;border:1px solid var(--line,#232a36);background:#000" }));
  return wrap;
}

function _inboxSig(inbox) {
  if (!inbox) return "";
  return [inbox.count, inbox.replan ? 1 : 0, (inbox.approval && inbox.approval.needs) || "",
    (inbox.revisions || []).map((r) => r.id).join(",")].join("~");
}

function _runSig(run) {
  const st = run.state || "not_started";
  return [st, !!run.plan_approved, run.phase || "", run.activity || "",
    (run.log || []).length, run.error || "", (run.preview && run.preview.at) || "",
    _inboxSig(run._inbox),
    boardHasProduction(currentState()) ? 1 : 0].join("|");
}

function currentState() { return replay ? stateAt(state, replay.t) : state; }

function renderRunPanel(s) {
  const wrap = el("div", { class: "board-empty run-panel" });
  const body = el("div", {});
  wrap.append(body);
  lastRunSig = null;               // fresh panel → force first render
  refreshRun(s, body, wrap, true);
  return wrap;
}

// Fetch the run; only rebuild the DOM when it MATERIALLY changed (prevents the
// every-poll flicker + collapse of expanded sections). Always updates the badge.
async function refreshRun(s, body, wrap, force) {
  let run;
  try { run = await getJSON(`/api/project/${encodedProjectId}/run`); }
  catch {
    if (force) { body.textContent = ""; body.append(el("p", { class: "hint" }, "Could not load run status.")); }
    _scheduleRunPoll(s, body, wrap, "running");
    return;
  }
  // Best-effort: what's queued for the agent (revisions / re-plan / approval).
  try { run._inbox = await getJSON(`/api/project/${encodedProjectId}/agent-inbox`); }
  catch { run._inbox = null; }
  const st = run.state || "not_started";
  // The command-center card (canonical /status) owns the slate badge now; the
  // coarse run panel lives inside the collapsed technical-details section.
  if (boardHasProduction(s) && st === "not_started") { wrap.style.display = "none"; return; }
  wrap.style.display = "";

  const sig = _runSig(run);
  if (force || sig !== lastRunSig) {
    lastRunSig = sig;
    renderRunBody(s, body, wrap, run);
  }
  _scheduleRunPoll(s, body, wrap, st);
}

function _scheduleRunPoll(s, body, wrap, st) {
  if (runPollTimer) { clearTimeout(runPollTimer); runPollTimer = null; }
  if (_ACTIVE_RUN_STATES.has(st)) {
    // slower cadence for the durable waiting state; snappier while working
    const ms = st === "waiting_for_approval" ? 4000 : 2000;
    runPollTimer = setTimeout(() => refreshRun(s, body, wrap, false), ms);
  }
}

function renderRunBody(s, body, wrap, run) {
  const st = run.state || "not_started";
  body.textContent = "";
  const repaint = () => { lastRunSig = null; refreshRun(s, body, wrap, true); };
  const disableBtns = () => [...wrap.querySelectorAll("button")].forEach((b) => { b.disabled = true; });
  const act = async (url, msg) => {
    disableBtns();
    try { await mpost(url, msg || {}); }
    catch (e) { body.append(el("p", { class: "hint", style: "color:var(--red)" }, e.message || "Action failed.")); }
    repaint();
  };
  const start = () => act(`/api/project/${encodedProjectId}/run`);
  const stop = () => { if (window.confirm("Stop this production run? Completed artifacts and checkpoints are preserved.")) act(`/api/project/${encodedProjectId}/run/cancel`, { run_id: run.run_id }); };
  const startBtn = (label) => { const b = el("button", { class: "set-btn", type: "button" }, label); b.onclick = start; return b; };
  const stopBtn = () => { const b = el("button", { class: "set-mini", type: "button", style: "border-color:var(--red);color:var(--red)" }, "■ Stop production"); b.onclick = stop; return b; };
  const previewBtn = () => {
    const b = el("button", { class: "set-mini", type: "button" }, run.preview ? "↻ Re-render preview" : "▶ Generate preview animatic (free)");
    b.onclick = async () => {
      disableBtns();
      const note = el("p", { class: "hint", style: "color:var(--amber,#e8c07d)" }, "Rendering a free preview animatic locally (~20–40s)…");
      body.append(note);
      try { await mpost(`/api/project/${encodedProjectId}/run/preview`, {}); }
      catch (e) { note.textContent = e.message || "Preview render failed."; note.style.color = "var(--red)"; setTimeout(repaint, 1800); return; }
      repaint();
    };
    return b;
  };

  if (st === "not_started") {
    body.append(el("h3", {}, "Production not started"));
    body.append(el("p", { class: "hint" },
      `Your workspace, brief and ${s.target_formatted || "target"} duration are saved — nothing is generating yet. ` +
      "Start the local preflight & planning run: it validates your intake, reads the provider menu, and plans a " +
      "frame-accurate timeline, then stops at the first approval point. No paid generation runs automatically."));
    body.append(el("div", { class: "np-actions", style: "justify-content:flex-start" }, startBtn("▶ START PRODUCTION")));
    body.append(advancedPrompt(s));
  } else if (st === "starting" || st === "running" || st === "cancelling") {
    body.append(el("h3", {}, st === "cancelling" ? "Cancelling…" : "Production running"));
    body.append(el("p", { class: "run-activity" }, run.activity || ""));
    const meta = el("p", { class: "hint mono" });
    meta.textContent = `state: ${st}${run.phase ? " · phase: " + run.phase : ""}${run.started_at ? " · elapsed " + _elapsed(run.started_at) : ""}`;
    body.append(meta);
    const log = renderActivityLog(run); if (log) body.append(log);
    if (st !== "cancelling") body.append(el("div", { class: "np-actions", style: "justify-content:flex-start" }, stopBtn()));
  } else if (st === "waiting_for_approval") {
    const approved = !!run.plan_approved;
    body.append(el("h3", {}, approved ? "✓ Plan approved — awaiting your agent" : "◈ Review & approve the plan"));
    body.append(el("p", { class: "run-activity" }, run.activity || ""));
    if (run.plan) body.append(renderPlanCard(run.plan));
    if (run.plan) { const pd = renderProviderDetail(run.plan); if (pd) body.append(pd); }
    const pvs = previewSection(run); if (pvs) body.append(pvs);
    const log = renderActivityLog(run); if (log) body.append(log);
    if (!approved) {
      body.append(el("p", { class: "hint" },
        "This is the provider + proposal approval point. Review the plan above, then Approve to give your go-ahead. " +
        "You can also render a free preview animatic of the plan now. Generating the full film's assets is agent-driven " +
        "(Backlot never auto-runs paid generation)."));
      const approveBtn = el("button", { class: "set-btn", type: "button" }, "✓ Approve plan");
      approveBtn.onclick = () => act(`/api/project/${encodedProjectId}/run/approve`, { run_id: run.run_id });
      body.append(el("div", { class: "np-actions", style: "justify-content:flex-start;gap:10px;flex-wrap:wrap" }, approveBtn, previewBtn(), _timelineLink(), stopBtn()));
    } else {
      body.append(el("p", { class: "hint" },
        "✓ Approved. Nothing is generating automatically right now — that's by design: Backlot never runs paid " +
        "generation on its own (Rule Zero). Producing the full film's assets happens through your agent, using the " +
        "handoff shown in the Agent Queue below. The run stays here (cancellable) until the agent takes over."));
      body.append(el("p", { class: "hint", style: "color:var(--amber,#e8c07d)" },
        "To SEE real output now — for free, locally — open the timeline and hit “▶ Render preview film” for a complete " +
        "Remotion render you can play, or render a quick preview animatic here."));
      body.append(el("div", { class: "np-actions", style: "justify-content:flex-start;gap:10px;flex-wrap:wrap" }, _timelineLink(), previewBtn(), stopBtn()));
      body.append(advancedPrompt(s));
    }
  } else if (st === "cancelled" || st === "failed" || st === "completed") {
    const titleMap = { cancelled: "Run cancelled", failed: "Run failed", completed: "Production complete" };
    body.append(el("h3", {}, titleMap[st]));
    if (st === "failed" && run.error) body.append(el("p", { class: "hint", style: "color:var(--red)" }, run.error));
    else body.append(el("p", { class: "hint" }, run.activity || ""));
    const log = renderActivityLog(run); if (log) body.append(log);
    body.append(el("div", { class: "np-actions", style: "justify-content:flex-start;gap:10px" }, startBtn("▶ Start again"), _timelineLink()));
    if (st !== "completed") body.append(advancedPrompt(s));
  }
  if (run._inbox && run._inbox.count) body.append(renderAgentQueueBoard(run._inbox));
}

// Consolidated, honest view of what's queued for the agent (Rule Zero) — the
// production hub shows exactly what the agent will pick up next.
function renderAgentQueueBoard(inbox) {
  const card = el("div", { style: "margin-top:14px;border:1px solid var(--amber,#e8c07d);border-radius:8px;padding:10px 12px;background:#1a1710" });
  card.append(el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:6px" },
    el("b", { style: "color:var(--amber,#e8c07d);font-size:12px;letter-spacing:0.1em" }, "AGENT QUEUE"),
    el("span", { class: "mono", style: "font-size:11px;color:var(--amber,#e8c07d)" }, `${inbox.count} queued`)));
  card.append(el("p", { class: "hint", style: "margin:0 0 6px" }, inbox.summary));
  for (const r of (inbox.revisions || [])) {
    card.append(el("div", { style: "font-size:12px;color:var(--bone,#e9e4d6)" },
      `✦ ${r.layer_type || "layer"} ${r.layer_id} — “${String(r.prompt || "").slice(0, 80)}”`));
  }
  if (inbox.replan) card.append(el("div", { style: "font-size:12px;color:var(--bone,#e9e4d6)" },
    "⟳ Duration re-plan requested — the agent will rebuild the timeline to the new length."));
  if (inbox.approval && inbox.approval.needs === "agent") card.append(el("div", { style: "font-size:12px;color:var(--bone,#e9e4d6)" },
    "▶ Plan approved — awaiting the agent to produce."));
  card.append(el("p", { class: "hint", style: "margin:8px 0 0" },
    "Honest, machine-readable requests the agent consumes — Backlot never generates media itself (Rule Zero)."));
  card.append(el("div", { style: "margin-top:8px" }, _timelineLink()));
  return card;
}

function boardHasProduction(s) {
  return (s.stages || []).some((x) => x.status === "completed" || x.status === "in_progress")
    || (s.storyboard && (s.storyboard.scenes || []).length)
    || (s.renders && s.renders.length);
}

// ===========================================================================
// COMMAND CENTER — the single, dominant production-status card.
// Driven by the canonical /status view model (shared with the Remotion Studio),
// so the board and studio always show the same NOW / NEXT / LAST and stage.
// ===========================================================================
let statusPollTimer = null;
let lastStatusSig = null;
let lastStatusView = null;   // last-known-good view (preserved on network error)

const OWNER_LABEL = { hermes: "Hermes", user: "You", system: "System" };
const STEP_GLYPH = {
  completed: "✓", current: "◉", blocked: "▲", awaiting: "◈",
  failed: "✕", skipped: "–", upcoming: "",
};

function _statusPollMs(view) {
  const st = view && view.overall_state;
  if (st === "producing" || st === "planning" || st === "cancelling") return 2500;
  if (st === "reconciling") return 2000;
  if (["awaiting_approval", "awaiting_plan_approval", "blocked", "ready_to_produce"].includes(st)) return 4000;
  return 8000;   // idle / terminal — still poll (connection state can change)
}

function _statusSig(v) {
  if (!v) return "none";
  return [v.overall_state, v.current_stage, v.owner, v.headline,
    (v.primary_action || {}).id, v.stop_available, (v.connection || {}).status,
    v.stale, (v.diagnostics || []).length, (v.latest_event || {}).seq,
    v.render && v.render.renderable].join("|");
}

function _setStatusBadge(view) {
  const badge = document.querySelector(".slate .live");
  if (!badge) return;
  const st = view.overall_state;
  const label = {
    not_started: "NOT STARTED", planning: "PREPARING",
    awaiting_plan_approval: "REVIEW PLAN", ready_to_produce: "READY",
    producing: "PRODUCING", awaiting_approval: "NEEDS YOU",
    blocked: "BLOCKED", cancelling: "CANCELLING", cancelled: "CANCELLED",
    failed: "FAILED", completed: "COMPLETE", reconciling: "RECONCILING",
  }[st] || st.toUpperCase();
  const live = ["producing", "planning", "cancelling"].includes(st);
  const attention = ["awaiting_approval", "awaiting_plan_approval", "blocked", "failed"].includes(st);
  badge.textContent = "";
  badge.classList.remove("idle");
  const dotStyle = attention ? "background:var(--amber,#e8c07d);animation:none" : "";
  badge.append(el("span", { class: "dot", style: dotStyle }),
    (view.stale ? "⟳ " : "") + label);
  if (!live && !attention) badge.classList.add("idle");
  badge.style.color = attention ? "var(--amber,#e8c07d)" : "";
}

function renderCommandCenter(s) {
  const wrap = el("section", { class: "cmd-center", "aria-live": "polite" });
  // Paint immediately from the last-known view (no flash), then refresh.
  if (lastStatusView) renderStatusCard(wrap, lastStatusView);
  else wrap.append(el("div", { class: "cmd-loading" }, "Loading production status…"));
  refreshStatus(wrap, true);
  return wrap;
}

async function refreshStatus(wrap, force) {
  if (statusPollTimer) { clearTimeout(statusPollTimer); statusPollTimer = null; }
  let view;
  try {
    view = await getJSON(`/api/project/${encodedProjectId}/status`);
    lastStatusView = view;
  } catch {
    // Network error: PRESERVE the last-known-good state, flag it stale +
    // reconnecting. NEVER blank the card or invent a fake state.
    if (lastStatusView) {
      view = Object.assign({}, lastStatusView, { stale: true });
      view.diagnostics = (lastStatusView.diagnostics || []).concat(
        [{ kind: "stale", message: "Reconnecting to live updates…" }]);
    } else {
      view = null;
    }
  }
  if (!view) {
    wrap.textContent = "";
    wrap.append(el("div", { class: "cmd-loading" }, "Production status is temporarily unavailable — reconnecting…"));
    statusPollTimer = setTimeout(() => refreshStatus(wrap, true), 3000);
    return;
  }
  _setStatusBadge(view);
  const sig = _statusSig(view);
  if (force || sig !== lastStatusSig) {
    lastStatusSig = sig;
    renderStatusCard(wrap, view);
  }
  statusPollTimer = setTimeout(() => refreshStatus(wrap, false), _statusPollMs(view));
}

function renderStatusCard(wrap, view) {
  wrap.textContent = "";
  wrap.dataset.state = view.overall_state;
  wrap.dataset.owner = view.owner || "";

  // --- connection banner (only when connecting is the actual next step) ----
  // Never nag to connect while a live run is already producing, or on a
  // terminal run — the banner is for when the user must connect to proceed.
  const conn = view.connection || {};
  const activeRun = view.is_live || ["producing", "cancelling", "cancelled", "failed", "completed"].includes(view.overall_state);
  if (!conn.available && conn.status && conn.status !== "unknown" && !activeRun) {
    wrap.append(renderConnectionBanner(view));
  }
  // --- diagnostics (reconciling / stale) ----------------------------------
  for (const d of (view.diagnostics || [])) {
    if (d.kind === "stale") {
      wrap.append(el("div", { class: "cmd-diag stale", role: "status" },
        el("span", { class: "spinner" }), d.message));
    } else if (d.kind === "source_conflict") {
      wrap.append(el("div", { class: "cmd-diag conflict", role: "status" }, "⚠ " + d.message));
    }
  }

  // --- the three-up NOW / NEXT / LAST -------------------------------------
  const grid = el("div", { class: "cmd-grid" });

  // NOW
  const stageTag = view.stage_number
    ? `Stage ${view.stage_number} of ${view.stage_count} · ${view.current_stage_label || ""}`
    : "";
  const now = el("div", { class: "cmd-now" });
  now.append(el("div", { class: "cmd-eyebrow" }, "NOW"));
  if (stageTag) now.append(el("div", { class: "cmd-stagetag" }, stageTag));
  now.append(el("h2", { class: "cmd-headline" }, view.headline || "—"));
  if (view.active_task) now.append(el("p", { class: "cmd-task" }, view.active_task));
  const metaBits = [];
  if (view.owner) metaBits.push(`Owner: ${OWNER_LABEL[view.owner] || view.owner}`);
  if (typeof view.elapsed_seconds === "number") metaBits.push(`Elapsed ${_fmtSecs(view.elapsed_seconds)}`);
  if (metaBits.length) now.append(el("div", { class: "cmd-meta" }, metaBits.join(" · ")));
  grid.append(now);

  // NEXT (single primary action + owner)
  const next = el("div", { class: "cmd-next" });
  next.append(el("div", { class: "cmd-eyebrow" }, "NEXT"));
  const pa = view.primary_action || {};
  const owns = OWNER_LABEL[pa.owner] || "";
  next.append(el("div", { class: "cmd-owner-line" },
    pa.owner === "user" ? "Your move" : pa.owner === "hermes" ? "Hermes' move" : "In progress"));
  next.append(renderPrimaryAction(view, pa));
  if (view.why_waiting) next.append(el("p", { class: "cmd-why" }, view.why_waiting));
  // secondary actions
  const secs = (view.secondary_actions || []);
  if (secs.length || view.stop_available) {
    const row = el("div", { class: "cmd-secondary" });
    for (const a of secs) row.append(renderSecondaryAction(view, a));
    if (view.stop_available) row.append(renderStopAction(view));
    next.append(row);
  }
  grid.append(next);

  // LAST (latest completed event / output)
  const last = el("div", { class: "cmd-last" });
  last.append(el("div", { class: "cmd-eyebrow" }, "LATEST"));
  const ev = view.latest_event;
  if (ev && ev.label) {
    last.append(el("p", { class: "cmd-lastmsg" }, ev.label));
    const ms = ev.ts ? Date.parse(ev.ts) : NaN;
    if (!Number.isNaN(ms)) last.append(el("div", { class: "cmd-meta" }, fmtAgo(ms / 1000)));
  } else {
    last.append(el("p", { class: "cmd-lastmsg dim" }, "No activity yet."));
  }
  // real telemetry when a live job is running (never for fixtures)
  const id = view.identity || {};
  if (view.is_live && (id.job || id.tool || id.provider)) {
    const chips = el("div", { class: "cmd-idchips" });
    if (id.tool) chips.append(el("span", { class: "idchip" }, id.tool));
    if (id.provider) chips.append(el("span", { class: "idchip" }, id.provider));
    if (id.job) chips.append(el("span", { class: "idchip mono" }, "job " + id.job));
    last.append(chips);
  }
  if (view.is_fixture) last.append(el("div", { class: "cmd-fixture" }, "◐ Demo data — no live run"));
  grid.append(last);

  wrap.append(grid);

  // --- 11-stage stepper ---------------------------------------------------
  wrap.append(renderStepper(view));

  // --- render availability note (when relevant) ---------------------------
  const r = view.render || {};
  if (!r.renderable && r.reason && ["ready_to_produce", "producing", "awaiting_plan_approval"].includes(view.overall_state)) {
    wrap.append(el("div", { class: "cmd-rendernote" }, "▤ " + r.reason));
  }
}

function renderStepper(view) {
  const nav = el("nav", { class: "cmd-stepper", "aria-label": "Production stages" });
  for (const st of (view.stages || [])) {
    const node = el("div", { class: `cmd-step ${st.status}`, title: `${st.label} — ${st.status}` });
    node.append(el("span", { class: "cmd-step-dot" }, STEP_GLYPH[st.status] || String(st.index + 1)));
    node.append(el("span", { class: "cmd-step-label" }, st.label));
    if (st.status === "current" && typeof st.progress === "number" && st.progress > 0) {
      node.append(el("span", { class: "cmd-step-pct" }, Math.round(st.progress * 100) + "%"));
    }
    nav.append(node);
  }
  return nav;
}

function renderConnectionBanner(view) {
  // Status text ONLY — no button. The single clickable Connect action on the
  // page is the command-center primary below (one-action invariant).
  const conn = view.connection || {};
  const banner = el("div", { class: `cmd-conn ${conn.status} statusonly`, role: "status" });
  banner.append(el("div", { class: "cmd-conn-text" },
    el("b", {}, conn.headline || "Hermes connection"),
    conn.detail ? el("div", { class: "cmd-conn-detail" }, conn.detail) : null));
  return banner;
}

// ---- action wiring ---------------------------------------------------------
function _statusRepaint(wrap) {
  lastStatusSig = null;
  const w = wrap || document.querySelector(".cmd-center");
  if (w) refreshStatus(w, true);
}

function renderPrimaryAction(view, pa) {
  const passive = pa.advances_production === false && ["status", "deliverable"].includes(pa.kind);
  if (passive && pa.kind === "status") {
    return el("div", { class: "cmd-primary passive", role: "status" },
      el("span", { class: "spinner" }), pa.label);
  }
  const btn = el("button", { class: "cmd-primary", type: "button" }, pa.label);
  btn.onclick = () => runStatusAction(view, pa, btn);
  return btn;
}

function renderSecondaryAction(view, a) {
  const btn = el("button", { class: "cmd-btn ghost", type: "button" }, a.label);
  btn.onclick = () => runStatusAction(view, a, btn);
  return btn;
}

function renderStopAction(view) {
  const btn = el("button", { class: "cmd-btn danger", type: "button" }, "■ Stop production");
  btn.onclick = () => {
    if (!window.confirm("Stop this production run? Completed work is preserved.")) return;
    const brain = !!view.sources.brain_run_id;
    const url = brain
      ? `/api/project/${encodedProjectId}/brain/cancel`
      : `/api/project/${encodedProjectId}/run/cancel`;
    _doStatusPost(url, { run_id: view.run_id }, btn);
  };
  return btn;
}

async function runStatusAction(view, a, btn) {
  const P = `/api/project/${encodedProjectId}`;
  switch (a.id) {
    case "connect_hermes":
    case "retry_connect":
      return openConnectModal(view);
    case "start":
    case "continue_hermes":
    case "restart":
      return _doStatusPost(`${P}/brain/start`, {}, btn);
    case "approve_plan":
      return _doStatusPost(`${P}/run/approve`, { run_id: view.run_id }, btn);
    case "request_changes":
      return _doStatusPost(`${P}/run/cancel`, { run_id: view.run_id }, btn,
        "Send the plan back for changes? The current run stops; nothing is lost.");
    case "approve":
      return _doStatusPost(`${P}/brain/approve`,
        { run_id: view.run_id, approval_id: a.approval_id, stage: a.stage }, btn);
    case "reject":
      return _doStatusPost(`${P}/brain/reject`,
        { run_id: view.run_id, approval_id: a.approval_id, stage: a.stage }, btn);
    case "retry_stage":
    case "retry_control":
      return _doStatusPost(`${P}/brain/retry`, { run_id: view.run_id, stage: a.stage }, btn);
    case "preview":
      return _doPreview(btn);
    case "view_deliverable":
      { const el0 = document.querySelector(".renders, .render-card"); if (el0) el0.scrollIntoView({ behavior: "smooth" }); return; }
    default:
      return _statusRepaint();
  }
}

async function _doStatusPost(url, body, btn, confirmMsg) {
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  if (btn) { btn.disabled = true; }
  try {
    await mpost(url, body || {});
  } catch (e) {
    const wrap = document.querySelector(".cmd-center");
    if (wrap) wrap.append(el("div", { class: "cmd-diag conflict" }, "⚠ " + (e.message || "Action failed.")));
    if (btn) btn.disabled = false;
    return;
  }
  _statusRepaint();
}

async function _doPreview(btn) {
  if (btn) btn.disabled = true;
  const wrap = document.querySelector(".cmd-center");
  const note = el("div", { class: "cmd-diag stale" }, "Rendering a free local preview animatic (~20–40s)…");
  if (wrap) wrap.append(note);
  try {
    await mpost(`/api/project/${encodedProjectId}/run/preview`, {});
  } catch (e) {
    note.className = "cmd-diag conflict";
    note.textContent = "⚠ " + (e.message || "Preview render failed.");
    if (btn) btn.disabled = false;
    return;
  }
  _statusRepaint();
}

// ---- Connect Hermes guided modal ------------------------------------------
function openConnectModal(view) {
  const conn = view.connection || {};
  modal.textContent = "";
  modal.classList.add("open");
  const card = el("div", { class: "cmd-modal" });
  card.append(el("h3", {}, "Connect Hermes"));
  card.append(el("p", { class: "hint" },
    "OpenMontage runs production through the Hermes brain (Mochlet). It verifies "
    + "the local MCP orchestrator's capabilities and the project. Your token is "
    + "stored only in the OS keychain — never printed or written to disk."));
  const urlIn = el("input", { class: "cmd-input", type: "text",
    value: conn.suggested_endpoint || conn.endpoint || "http://127.0.0.1:9235/mcp",
    placeholder: "http://127.0.0.1:9235/mcp" });
  const tokIn = el("input", { class: "cmd-input", type: "password", autocomplete: "off",
    placeholder: "Access token (from Mochlet)" });
  card.append(el("label", { class: "cmd-label" }, "Mochlet MCP endpoint"), urlIn);
  card.append(el("label", { class: "cmd-label" }, "Token (kept in the OS keychain)"), tokIn);
  // Project chooser (revealed when Mochlet reports multiple projects).
  const projectWrap = el("div", { style: "display:none" });
  const projectLabel = el("label", { class: "cmd-label" }, "OpenMontage project in Mochlet");
  const projectSel = el("select", { class: "cmd-input" });
  projectWrap.append(projectLabel, projectSel);
  const status = el("div", { class: "cmd-modal-status" });
  const testBtn = el("button", { class: "cmd-btn ghost", type: "button" }, "Test connection");
  const connectBtn = el("button", { class: "cmd-primary", type: "button" }, "Connect & verify");
  const cancelBtn = el("button", { class: "cmd-btn ghost", type: "button" }, "Cancel");
  cancelBtn.onclick = () => modal.classList.remove("open");

  const showProjects = (projects) => {
    projectSel.textContent = "";
    for (const p of projects) {
      projectSel.append(el("option", { value: p.id }, p.name || p.path || p.id));
    }
    projectWrap.style.display = "";
    connectBtn.textContent = "Connect to project";
  };

  const doConnect = async (withProject) => {
    connectBtn.disabled = true; testBtn.disabled = true;
    status.className = "cmd-modal-status";
    status.textContent = "Verifying Hermes (MCP capabilities + project)…";
    const body = { url: urlIn.value.trim(), token: tokIn.value || undefined };
    if (withProject && projectSel.value) body.project_id = projectSel.value;
    try {
      const res = await mpost(`/api/hermes/connect`, body);
      if (res.available) {
        status.className = "cmd-modal-status ok";
        status.textContent = "✓ " + (res.headline || "Connected");
        setTimeout(() => { modal.classList.remove("open"); _statusRepaint(); }, 700);
      } else if (res.status === "needs_project" && Array.isArray(res.projects) && res.projects.length) {
        showProjects(res.projects);
        status.className = "cmd-modal-status";
        status.textContent = res.detail || "Choose the OpenMontage project in Mochlet.";
        connectBtn.disabled = false; testBtn.disabled = false;
      } else {
        status.className = "cmd-modal-status err";
        status.textContent = (res.headline || "Couldn't connect") + (res.detail ? " — " + res.detail : "");
        connectBtn.disabled = false; testBtn.disabled = false;
      }
    } catch (e) {
      status.className = "cmd-modal-status err";
      status.textContent = e.message || "Connection failed.";
      connectBtn.disabled = false; testBtn.disabled = false;
    }
  };
  testBtn.onclick = () => doConnect(false);
  connectBtn.onclick = () => doConnect(projectWrap.style.display !== "none");
  card.append(projectWrap, status,
    el("div", { class: "cmd-modal-actions" }, connectBtn, testBtn, cancelBtn));
  modal.append(card);
}

function _fmtSecs(secs) {
  if (secs == null) return "";
  const s = Math.max(0, Math.round(secs));
  const m = Math.floor(s / 60), r = s % 60;
  return m ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
}

// ---------------------------------------------------------------------------
// stage rail
// ---------------------------------------------------------------------------

function stageSub(st) {
  if (st.status === "awaiting_human") return "awaiting your approval\nreply in chat to continue";
  if (st.status === "in_progress" && st.stalled) {
    return `stalled? no activity for ${st.stalled_minutes}m\nask the agent for status`;
  }
  if (st.status === "in_progress" && st.partial_progress) {
    const done = st.partial_progress.completed_scene_ids;
    if (Array.isArray(done)) return `${done.length} scene${done.length === 1 ? "" : "s"} done`;
    return "in progress";
  }
  if (st.status === "in_progress") return "in progress";
  if (st.status === "failed") return st.error ? String(st.error).slice(0, 60) : "failed";
  if (st.timestamp) {
    const approved = st.gated && st.human_approved ? " · approved" : "";
    return fmtClock(st.timestamp) + approved;
  }
  return "";
}

function renderRail(s) {
  const rail = el("nav", { class: "rail" });
  let pendingIndex = 1;
  for (const st of s.stages) {
    const cls = st.status === "completed" ? "done"
      : st.status === "in_progress" ? (st.stalled ? "active stalled" : "active")
      : st.status === "awaiting_human" ? "await"
      : st.status === "failed" ? "failed" : "";
    const icon = STAGE_ICONS[st.status] || String(pendingIndex);
    if (!STAGE_ICONS[st.status]) pendingIndex += 1;
    const node = el("div", {
      class: `stage ${cls}${selectedStage === st.name ? " selected" : ""}${st.undeclared ? " undeclared" : ""}`,
      title: st.undeclared ? `"${st.name}" ran but isn't declared by this pipeline's manifest` : null,
      onclick: () => toggleDrawer(st.name),
    },
      el("span", { class: "line" }),
      el("span", { class: "node" }, icon),
      el("span", { class: "name" }, st.name),
      el("span", { class: "sub", style: "white-space:pre-line" },
        st.undeclared ? `${stageSub(st)}\nunlisted`.trim() : stageSub(st)),
    );
    rail.append(node);
  }
  return rail;
}

function toggleDrawer(stageName) {
  selectedStage = selectedStage === stageName ? null : stageName;
  render();
}

const STAGE_ARTIFACTS = {
  research: ["research_brief"],
  proposal: ["proposal_packet"],
  idea: ["brief"],
  script: ["script"],
  scene_plan: ["scene_plan"],
  assets: ["asset_manifest"],
  edit: ["edit_decisions"],
  compose: ["render_report", "final_review"],
  publish: ["publish_log"],
};

function renderDrawer(s) {
  if (!selectedStage) return null;
  const st = s.stages.find((x) => x.name === selectedStage);
  if (!st) return null;

  const body = el("div", { class: "drawer-body" });

  if (st.review) {
    body.append(el("div", { class: "findings", style: "margin-bottom:12px" },
      el("span", { class: `f ${st.review.critical ? "crit" : ""}` }, `${st.review.critical ?? 0} critical`),
      el("span", { class: `f ${st.review.suggestions ? "sugg" : ""}` }, `${st.review.suggestions ?? 0} suggestions`),
      el("span", { class: "f" }, `${st.review.nitpicks ?? 0} nitpicks`),
      typeof st.review.summary === "string" ? el("span", { style: "font-size:calc(11.5px * var(--fs-scale));color:var(--text-2);margin-left:8px" }, st.review.summary) : null,
    ));
  }

  const names = STAGE_ARTIFACTS[st.name] || [];
  let shown = false;
  for (const name of names) {
    const artifact = s.artifacts[name];
    if (!artifact) continue;
    shown = true;
    body.append(
      el("div", { class: "d-cat", style: "font-family:var(--mono);font-size:calc(9.5px * var(--fs-scale));color:var(--text-3);letter-spacing:.1em;text-transform:uppercase;margin:6px 0 4px" }, name),
      el("pre", {}, JSON.stringify(artifact, null, 2)),
    );
  }
  if (!shown) {
    body.append(el("div", { class: "hint" },
      st.status === "pending" ? "This stage hasn't run yet." : "No canonical artifact found on disk for this stage."));
  }

  return el("div", { class: "drawer" },
    el("div", { class: "drawer-head" },
      el("h3", {}, `${st.name} — ${st.status}`),
      st.gate_skipped ? el("span", { class: "gate-chip" }, "⚑ GATE SKIPPED") : null,
      st.versions > 1 ? el("span", { class: "ver-chip" }, `v${st.versions}`) : null,
      st.timestamp ? el("span", { class: "meta", style: "font-family:var(--mono);font-size:calc(10.5px * var(--fs-scale));color:var(--text-3)" }, st.timestamp) : null,
      el("span", { class: "close", onclick: () => toggleDrawer(st.name) }, "CLOSE ✕"),
    ),
    body,
  );
}

// ---------------------------------------------------------------------------
// script card
// ---------------------------------------------------------------------------

function scriptSections(script, limit) {
  const sections = script.sections || [];
  const shown = limit ? sections.slice(0, limit) : sections;
  const nodes = [];
  for (const sec of shown) {
    nodes.push(el("div", { class: "sp-slug" },
      `${(sec.id || "").toUpperCase()} — ${sec.label || "Section"} `,
      el("span", { class: "tc" }, `${fmtDuration(sec.start_seconds)} – ${fmtDuration(sec.end_seconds)}`)));
    if (sec.text) nodes.push(el("div", { class: "sp-action" }, sec.text));
    if (sec.speaker_directions) nodes.push(el("div", { class: "sp-paren" }, `(${sec.speaker_directions})`));
    const cues = sec.enhancement_cues || [];
    if (cues.length) {
      nodes.push(el("div", { style: "margin-left:42px" },
        cues.map((c) => el("span", { class: "sp-cue" }, `▸ ${c.type} · ${String(c.description || "").slice(0, 60)}`))));
    }
  }
  if (limit && sections.length > limit) {
    nodes.push(el("div", { class: "sp-fade" }, `… ${sections.length - limit} more sections`));
  }
  return nodes;
}

function renderScriptCard(s) {
  const script = s.artifacts.script;
  if (!script) return null;
  const scriptStage = s.stages.find((x) => x.name === "script");
  const approved = scriptStage && scriptStage.status === "completed";

  const card = el("div", { class: "script-card", title: "Click to expand full script", onclick: openScriptModal },
    approved ? el("span", { class: "script-approved" }, "APPROVED") : null,
    el("div", { class: "sp-title" }, script.title || s.title),
    el("div", { class: "sp-meta" },
      `script · ${fmtDuration(script.total_duration_seconds)} · ${(script.sections || []).length} sections`),
    ...scriptSections(script, 4),
    el("span", { class: "sp-expand" }, "⤢ EXPAND SCRIPT"),
  );
  return card;
}

function openScriptModal() {
  const script = state && state.artifacts.script;
  if (!script) return;
  modal.innerHTML = "";
  modal.append(
    el("span", { class: "modal-close", onclick: closeModal }, "ESC · CLOSE"),
    el("div", { class: "modal-page" },
      el("div", { class: "script-card", style: "cursor:default" },
        el("div", { class: "sp-title" }, script.title || state.title),
        el("div", { class: "sp-meta" },
          `script · ${fmtDuration(script.total_duration_seconds)} · ${(script.sections || []).length} sections`),
        ...scriptSections(script, 0),
        el("div", { class: "sp-fade" }, "END"),
      )),
  );
  modal.classList.add("open");
}

function openNarrModal(card) {
  modal.innerHTML = "";
  const meta = [sceneLabel(card.id), card.section_label, fmtDuration(card.duration_seconds)]
    .filter(Boolean).join(" · ");
  modal.append(
    el("span", { class: "modal-close", onclick: closeModal }, "ESC · CLOSE"),
    el("div", { class: "modal-page" },
      el("div", { class: "script-card", style: "cursor:default" },
        el("div", { class: "sp-meta" }, meta),
        card.narration ? el("div", { class: "sp-action", style: "margin-left:0" }, card.narration) : null,
        card.shot_intent ? el("div", { class: "sp-paren", style: "margin-left:0" }, `Intent — ${card.shot_intent}`) : null,
        card.description ? el("div", { class: "sp-paren", style: "margin-left:0" }, card.description) : null,
      )),
  );
  modal.classList.add("open");
}

function closeModal() { modal.classList.remove("open"); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

// ---------------------------------------------------------------------------
// right rail: decisions, activity
// ---------------------------------------------------------------------------

function renderDecisions(s) {
  const log = s.artifacts.decision_log;
  const decisions = (log && log.decisions) || [];
  if (!decisions.length) return null;
  const body = el("div", { class: "panel-body" });
  // Collapse by category+subject: a decision that changed mid-run (e.g. voice
  // openai_onyx → chirp3) is superseded by the later entry — show the CURRENT
  // choice, not the first one recorded, and mark that it was revised.
  const current = new Map();
  decisions.forEach((d, i) => {
    const key = `${d.category || "decision"}::${d.subject || ""}`;
    const prev = current.get(key);
    current.set(key, { d, order: i, revised: prev ? prev.revised + 1 : 0 });
  });
  const shown = [...current.values()].sort((a, b) => b.order - a.order).slice(0, 8);
  for (const { d, revised } of shown) {
    const selLabel = (() => {
      // Prefer the human label of the selected option over its bare id.
      const opt = (d.options_considered || []).find((o) => (o.option_id ?? o.label) === d.selected);
      return (opt && opt.label) || d.selected || "";
    })();
    const alts = (d.options_considered || [])
      .filter((o) => (o.option_id ?? o.label) !== d.selected && (o.option_id || o.label));
    body.append(el("div", { class: "decision" },
      el("div", { class: "d-cat" }, `${d.category || "decision"}${d.confidence ? ` · ${d.confidence}` : ""}`,
        revised ? el("span", { class: "d-revised" }, " · revised") : null),
      el("div", { class: "d-pick" }, `${d.subject || ""} `, el("span", { class: "arrow" }, "→"), ` ${selLabel}`),
      d.reason ? el("div", { class: "d-why" }, d.reason) : null,
      alts.length ? el("div", { class: "d-alt" }, "also considered: ",
        alts.slice(0, 3).map((o, i) => [i ? " · " : "", el("s", {}, o.label || o.option_id)]).flat()) : null,
    ));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", {}, "Decisions"), el("span", { class: "meta" }, "decision_log.json")),
    body);
}

function renderActivity(s) {
  const events = s.events || [];
  if (!events.length) return null;
  const body = el("div", { class: "panel-body" });
  // A start is "running" only until a later finish/error for the same
  // tool+scene closes it — closed starts are dropped (the finish row tells
  // the story), unmatched starts render as live. Counted (not keyed-single)
  // so parallel runs of the same tool on the same scene stay visible.
  const open = new Map(); // key -> {count, ev}
  const rows = [];
  for (const ev of events) {
    const key = `${ev.tool}:${ev.scene_id || ""}`;
    if (ev.event === "start") {
      const slot = open.get(key) || { count: 0, ev };
      slot.count += 1;
      slot.ev = ev;
      open.set(key, slot);
    } else {
      const slot = open.get(key);
      if (slot) {
        slot.count -= 1;
        if (slot.count <= 0) open.delete(key);
      }
      rows.push(ev);
    }
  }
  for (const slot of open.values()) rows.push(slot.ev);
  rows.sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  for (const ev of rows.slice(-10).reverse()) {
    let statusEl;
    if (ev.event === "finish") {
      statusEl = el("span", { class: `status ${ev.success === false ? "err" : "ok"}` },
        `${ev.success === false ? "✕" : "✓"}${ev.duration_s != null ? ` ${ev.duration_s.toFixed ? ev.duration_s.toFixed(1) : ev.duration_s}s` : ""}${ev.cost_usd ? ` ${fmtMoney(ev.cost_usd)}` : ""}`);
    } else if (ev.event === "error") {
      statusEl = el("span", { class: "status err" }, "✕");
    } else {
      statusEl = el("span", { class: "status run" }, "● running");
    }
    body.append(el("div", { class: "act-row" },
      el("span", { class: "t" }, fmtClock(ev.ts)),
      el("span", { class: "tool" }, ev.tool || ""),
      el("span", { class: "target" }, ev.scene_id || ""),
      statusEl,
    ));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", {}, "Activity"), el("span", { class: "meta" }, "events.jsonl")),
    body);
}

// ---------------------------------------------------------------------------
// storyboard filmstrip
// ---------------------------------------------------------------------------

function sceneLabel(id) {
  // "sc4" → "SC 04", "scene-11" → "SC 11", anything else → uppercased id
  const m = String(id).match(/(\d+)\s*$/);
  if (m) return `SC ${m[1].padStart(2, "0")}`;
  return String(id).toUpperCase().slice(0, 10);
}

function sceneCard(s, card) {
  const dur = card.duration_seconds;
  const width = Math.max(132, Math.min(300, 70 + (dur || 3) * 26));
  const wrap = el("div", { class: "scene-card", style: `width:${width}px` });

  const slate = el("div", { class: "sc-slate" },
    el("span", { class: "num" }, sceneLabel(card.id)),
    card.takes.length > 1 ? el("span", { class: "take" }, `T${card.takes.length}`) : null,
    card.hero_moment ? el("span", { class: "hero" }, "★ HERO") : null,
    el("span", { class: "dur" }, fmtDuration(dur)),
  );
  wrap.append(slate);

  // visual slot
  let thumb;
  if (card.generating) {
    thumb = el("div", { class: "thumb generating" },
      el("div", { class: "shimmer" }),
      el("div", { class: "gen-label" },
        el("span", {}, "◉ GENERATING"),
        el("span", { class: "sub" }, card.generating_tool || "")));
  } else if (card.visual && card.visual.exists) {
    const v = card.visual;
    const badge = [v.model || v.source_tool, v.cost_usd != null ? fmtMoney(v.cost_usd) : null,
      v.quality_score != null ? `q ${v.quality_score}` : null].filter(Boolean).join(" · ");
    if (v.type === "video") {
      thumb = el("div", { class: "thumb approved" },
        el("video", { src: mediaURL(s.project_id, v.path), muted: "", preload: "metadata", playsinline: "" }),
        el("span", { class: "play" }, "▶"),
        badge ? el("span", { class: "badge" }, badge) : null);
      thumb.onclick = () => {
        const vid = thumb.querySelector("video");
        if (vid.paused) vid.play(); else vid.pause();
      };
    } else {
      const img = el("img", { src: thumbURL(s.project_id, v.path, 640), loading: "lazy", alt: "" });
      // A thumbnail that fails to load must never show a broken-image icon —
      // fall back to the shot spec in place (F: broken links).
      img.onerror = () => {
        const t = img.closest(".thumb");
        if (!t) return;
        t.className = "thumb spec";
        t.innerHTML = "";
        t.append(el("div", { class: "spec-in" },
          el("div", { class: "spec-desc" }, card.description || "asset unavailable"),
          el("div", { class: "spec-shot" }, [card.framing, card.movement].filter(Boolean).join(" · ").slice(0, 70))));
      };
      thumb = el("div", { class: "thumb approved" }, img,
        v.snapshot ? el("span", { class: "badge" }, "snapshot") : (badge ? el("span", { class: "badge" }, badge) : null));
    }
  } else if (card.type === "animation") {
    // Bespoke/atelier scene with no snapshot yet — name it as such rather
    // than "no asset yet" (the composition IS the asset).
    thumb = el("div", { class: "thumb spec bespoke" },
      el("div", { class: "spec-in" },
        el("span", { class: "bespoke-tag" }, "◆ BESPOKE"),
        el("div", { class: "spec-desc" }, card.description || ""),
        el("div", { class: "spec-shot" }, "hand-authored composition")));
  } else if (card.visual && !card.visual.exists) {
    thumb = el("div", { class: "thumb missing" },
      el("div", { class: "spec-in" },
        el("span", { class: "warn-ic" }, "⚑"),
        el("div", { class: "spec-desc" }, "asset in manifest, file missing"),
        el("div", { class: "spec-shot" }, card.visual.path || "")));
  } else if (card.type === "text_card") {
    thumb = el("div", { class: "thumb textcard" },
      el("div", { class: "tc-copy" }, (card.narration || card.description || "").slice(0, 48)));
  } else if (card.required_assets.length) {
    thumb = el("div", { class: "thumb missing" },
      el("div", { class: "spec-in" },
        el("span", { class: "warn-ic" }, "⚑"),
        el("div", { class: "spec-desc" }, "no asset yet"),
        el("div", { class: "spec-shot" }, (card.required_assets[0].description || "").slice(0, 60))));
  } else {
    thumb = el("div", { class: "thumb spec" },
      el("div", { class: "spec-in" },
        el("div", { class: "spec-desc" }, card.description || ""),
        el("div", { class: "spec-shot" }, [card.framing, card.movement].filter(Boolean).join(" · ").slice(0, 70))));
  }
  wrap.append(thumb);

  // shot language chips
  const sl = card.shot_language;
  if (sl) {
    wrap.append(el("div", { class: "shotchips", style: "display:flex;flex-wrap:wrap;gap:4px;padding:7px 2px 0" },
      [sl.shot_size, sl.camera_movement, sl.lens_mm ? `${sl.lens_mm}mm` : null, sl.lighting_key]
        .filter(Boolean)
        .map((t) => el("span", { style: "font-family:var(--mono);font-size:calc(8.5px * var(--fs-scale));letter-spacing:.04em;color:#62626c;border:1px solid #212129;border-radius:3px;padding:1px 5px" }, String(t).replaceAll("_", " ")))));
  }

  // takes drawer
  if (card.takes.length > 1) {
    const takes = el("div", { class: "takes" });
    card.takes.forEach((t, i) => {
      const isActive = card.visual && (
        t === card.visual
        || (t.path && t.path === card.visual.path)
        || (t.id && t.id === card.visual.id)
      );
      const tk = el("span", { class: `tk${isActive ? " active" : ""}`, title: `take ${i + 1}` });
      if (t.exists && t.type === "image") tk.append(el("img", { src: thumbURL(s.project_id, t.path, 320), loading: "lazy", alt: "" }));
      takes.append(tk);
    });
    takes.append(el("span", { class: "tk-label" }, `${card.takes.length} TAKES`));
    wrap.append(takes);
  }

  // narration + audio — clickable to read in full (F: narration text cut off)
  if (card.narration) {
    const long = card.narration.length > 90;
    wrap.append(el("div", {
      class: `narr${long ? " clip" : ""}`,
      title: "Click to read the full narration",
      onclick: () => openNarrModal(card),
    }, card.narration, long ? el("span", { class: "narr-more" }, "⤢") : null));
  } else if (card.shot_intent || card.description) {
    wrap.append(el("div", { class: "narr tc-note" }, (card.shot_intent || card.description || "").slice(0, 110)));
  }
  const narrAudio = card.audio.find((a) => a.exists && (a.type === "narration" || a.type === "audio"));
  if (narrAudio) {
    const wave = el("div", { class: "wave", style: "cursor:pointer", title: "Play narration" });
    waveBars(wave, card.id + narrAudio.path);
    wave.append(el("span", { class: "wv-time" }, narrAudio.duration_seconds ? fmtDuration(narrAudio.duration_seconds) : "♪"));
    wave.onclick = () => {
      player.src = mediaURL(s.project_id, narrAudio.path);
      player.play();
    };
    wrap.append(wave);
  }
  return wrap;
}

function renderStoryboard(s) {
  const board = s.storyboard;
  if (!board) return null;
  const strip = el("div", { class: "filmstrip" });
  for (const card of board.scenes) strip.append(sceneCard(s, card));
  return el("div", {},
    el("div", { class: "section-title" }, "Storyboard",
      el("span", { class: "meta" },
        `${board.scenes.length} scenes${board.total_duration_seconds ? ` · ${fmtDuration(board.total_duration_seconds)}` : ""} · card width ∝ duration`)),
    el("div", { class: "strip-outer" }, strip));
}

// ---------------------------------------------------------------------------
// renders + degraded media
// ---------------------------------------------------------------------------

function renderRenders(s) {
  const renders = s.media.renders;
  if (!renders.length) return null;
  if (activeRender >= renders.length) activeRender = 0;
  const current = renders[activeRender];
  // Full re-renders (every SSE refresh) must not reset an in-progress
  // watch: carry playback position/state over to the recreated element.
  const prev = document.querySelector(".render-hero video");
  const src = mediaURL(s.project_id, current.path);
  const video = el("video", { src, controls: "", preload: "none" });
  // Click the frame to start playback (controls handle pause/scrub) — the
  // big player was inert to a click on the picture itself.
  video.addEventListener("click", () => { if (video.paused) video.play().catch(() => {}); });
  if (prev && prev.getAttribute("src") === src && (prev.currentTime > 0 || !prev.paused)) {
    const t = prev.currentTime;
    const wasPlaying = !prev.paused && !prev.ended;
    video.addEventListener("loadedmetadata", () => { video.currentTime = t; }, { once: true });
    video.setAttribute("preload", "metadata");
    if (wasPlaying) video.autoplay = true;
  }
  const versions = el("div", { class: "render-meta" },
    renders.map((r, i) => el("span", {
      class: `v${i === activeRender ? " active" : ""}`,
      onclick: () => { activeRender = i; render(); },
    }, `${r.path.split("/").pop()}${r.at_root ? " · root" : ""}`)),
    el("span", { style: "margin-left:auto" }, `${(current.size / 1048576).toFixed(1)} MB`),
  );
  return el("div", {},
    el("div", { class: "section-title" }, "Renders",
      el("span", { class: "meta" }, `${renders.length} version${renders.length === 1 ? "" : "s"}`)),
    el("div", { class: "render-hero" }, video),
    versions);
}

function renderFoundMedia(s) {
  // Degraded view: show discovered snapshots when there's no storyboard.
  if (s.storyboard || !s.media.snapshots.length) return null;
  const grid = el("div", { class: "found-grid" });
  for (const snap of s.media.snapshots.slice(0, 12)) {
    grid.append(el("div", { class: "thumb" },
      el("img", { src: thumbURL(s.project_id, snap.path, 640), loading: "lazy", alt: "" })));
  }
  return el("div", {},
    el("div", { class: "section-title" }, "What the watcher found",
      el("span", { class: "meta" }, "snapshots / verification frames")),
    grid);
}

// ---------------------------------------------------------------------------
// replay — scrub a completed run from its timestamps
// ---------------------------------------------------------------------------

// Python writers emit tz-aware UTC isoformat, but treat tz-naive strings as
// UTC too — mixing local-parsed and UTC-parsed timestamps would skew replay
// ordering by the user's UTC offset.
const ts = (iso) => {
  if (!iso) return null;
  let s = String(iso);
  if (!/(Z|[+-]\d{2}:?\d{2})$/.test(s)) s += "Z";
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
};

function replayBounds(s) {
  const moments = [];
  for (const st of s.stages) {
    for (const h of st.history_entries || []) {
      const t = ts(h.timestamp);
      if (t) moments.push(t);
    }
  }
  for (const ev of s.events || []) {
    const t = ts(ev.ts);
    if (t) moments.push(t);
  }
  if (moments.length < 2) return null;
  return { t0: Math.min(...moments), t1: Math.max(...moments) };
}

function stateAt(s, T) {
  const view = structuredClone(s);
  for (const st of view.stages) {
    const past = (st.history_entries || []).filter((h) => ts(h.timestamp) != null && ts(h.timestamp) <= T);
    if (!past.length) {
      st.status = "pending"; st.review = null; st.timestamp = null;
      st.gate_skipped = false; st.partial_progress = null;
    } else {
      const cur = past[past.length - 1];
      st.status = cur.status || "pending";
      st.timestamp = cur.timestamp;
    }
  }
  view.events = (view.events || []).filter((ev) => ts(ev.ts) != null && ts(ev.ts) <= T);

  // Storyboard: visuals appear as their scene finishes (events) or when the
  // assets stage has completed as of T (legacy runs without events).
  if (view.storyboard) {
    const assetsStage = view.stages.find((x) => x.name === "assets");
    const assetsDone = assetsStage && assetsStage.status === "completed";
    const finished = new Set();
    const startedNow = new Map();
    for (const ev of view.events) {
      if (!ev.scene_id) continue;
      if (ev.event === "finish") { finished.add(ev.scene_id); startedNow.delete(ev.scene_id); }
      else if (ev.event === "start") startedNow.set(ev.scene_id, ev);
      else if (ev.event === "error") startedNow.delete(ev.scene_id);
    }
    const scenePlanStage = view.stages.find((x) => x.name === "scene_plan");
    const scenePlanDone = scenePlanStage && ["completed", "awaiting_human"].includes(scenePlanStage.status);
    if (!scenePlanDone) {
      view.storyboard = null;
    } else {
      for (const card of view.storyboard.scenes) {
        const visible = assetsDone || finished.has(card.id);
        if (!visible) { card.visual = null; card.takes = []; card.audio = []; }
        card.generating = startedNow.has(card.id);
        card.generating_tool = (startedNow.get(card.id) || {}).tool;
      }
    }
  }
  // Final artifacts hide until their stage happened — for every project
  // shape, storyboard or not (a degraded run must not show the finished
  // movie before its stages ran).
  const scriptStage = view.stages.find((x) => x.name === "script");
  if (!(scriptStage && ["completed", "awaiting_human"].includes(scriptStage.status))) {
    delete view.artifacts.script;
  }
  const composeStage = view.stages.find((x) => x.name === "compose");
  if (!(composeStage && composeStage.status === "completed")) {
    view.media.renders = [];
  }
  return view;
}

function renderReplayBar(s) {
  const bounds = replayBounds(s);
  if (!bounds) return null;
  if (!replay) {
    // collapsed: just the entry button
    return el("div", { class: "replay-bar", style: "justify-content:flex-end" },
      el("span", { class: "rp-time" }, "scrub the whole run"),
      el("span", { class: "rp-btn", onclick: startReplay }, "▶ REPLAY RUN"));
  }
  const pos = (replay.t - replay.t0) / Math.max(1, replay.t1 - replay.t0);
  const timeLabel = el("span", { class: "rp-time" },
    new Date(replay.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  const setT = (value) => {
    replay.t = replay.t0 + (Number(value) / 1000) * (replay.t1 - replay.t0);
    timeLabel.textContent = new Date(replay.t)
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  return el("div", { class: "replay-bar" },
    el("span", { class: "rp-btn", onclick: toggleReplayPlay }, replay.playing ? "❚❚" : "▶"),
    el("input", {
      type: "range", min: "0", max: "1000", value: String(Math.round(pos * 1000)),
      // A full render() would destroy this slider mid-drag: while dragging,
      // only pause + track the time label; re-render the board on release.
      onpointerdown: () => { replay.playing = false; },
      oninput: (e) => setT(e.target.value),
      onchange: (e) => { setT(e.target.value); render(); },
    }),
    timeLabel,
    el("span", { class: "rp-btn", onclick: stopReplay }, "✕ LIVE"),
  );
}

let replayTimer = null;

function startReplay() {
  const bounds = replayBounds(state);
  if (!bounds) return;
  replay = { ...bounds, t: bounds.t0, playing: true };
  document.body.classList.add("replaying");
  scheduleTick();
  render();
}

function stopReplay() {
  replay = null;
  clearTimeout(replayTimer);
  document.body.classList.remove("replaying");
  render();
}

function toggleReplayPlay() {
  if (!replay) return;
  replay.playing = !replay.playing;
  if (replay.playing) scheduleTick();
  render();
}

function scheduleTick() {
  // Single pending tick, ever — rapid pause/play must not stack chains.
  clearTimeout(replayTimer);
  replayTimer = setTimeout(tickReplay, 100);
}

function tickReplay() {
  if (!replay || !replay.playing) return;
  // A full run replays in ~20 seconds regardless of real duration
  // (10 renders/second — full re-render per tick, keep it modest).
  const step = (replay.t1 - replay.t0) / 200;
  replay.t = Math.min(replay.t1, replay.t + step);
  if (replay.t >= replay.t1) replay.playing = false;
  render();
  if (replay.playing) scheduleTick();
}

// ---------------------------------------------------------------------------
// page assembly
// ---------------------------------------------------------------------------

function render() {
  if (!state) return;
  const s = replay ? stateAt(state, replay.t) : state;
  document.title = `Backlot — ${s.title}`;
  document.body.classList.toggle("first", firstPaint);
  firstPaint = false;
  if (runPollTimer) { clearTimeout(runPollTimer); runPollTimer = null; }
  if (statusPollTimer) { clearTimeout(statusPollTimer); statusPollTimer = null; }
  app.innerHTML = "";
  app.append(renderSlate(s));
  // PRIMARY: the single, dominant command-center card (canonical /status).
  app.append(renderCommandCenter(s));

  // SECONDARY: the pipeline checkpoint rail + coarse run controls + manual
  // handoff, collapsed under an expandable "details" section — never the
  // primary hierarchy. (Open timeline / Agent Queue / manual prompt live here.)
  const tech = el("details", { class: "tech-details" });
  tech.append(el("summary", {}, "Production details, checkpoints & tools"));
  tech.append(renderRail(s));
  tech.append(renderRunPanel(s));
  app.append(tech);

  const replayBar = renderReplayBar(state);
  if (replayBar) app.append(replayBar);
  const drawer = renderDrawer(s);
  if (drawer) app.append(drawer);

  const main = el("div", { class: "main-col" });
  const script = renderScriptCard(s);
  if (script) main.append(script);
  const aside = el("aside", {});
  const decisions = renderDecisions(s);
  const activity = renderActivity(s);
  if (decisions) aside.append(decisions);
  if (activity) aside.append(activity);

  if (script || decisions || activity) {
    app.append(el("div", { class: "board" }, main, aside));
  }

  const storyboard = renderStoryboard(s);
  if (storyboard) app.append(storyboard);
  const found = renderFoundMedia(s);
  if (found) app.append(found);
  const renders = renderRenders(s);
  if (renders) app.append(renders);

  wireNewProjectButtons();
}

// Defensive normalization (F-02): the server contract guarantees these
// fields, but a sparse/legacy payload must degrade, never crash the board.
function normalize(s) {
  s.pipeline = s.pipeline || { pipeline_type: "unknown", stages: [], known: false };
  s.stages = Array.isArray(s.stages) ? s.stages : [];
  s.artifacts = s.artifacts || {};
  s.media = s.media || {};
  s.media.renders = Array.isArray(s.media.renders) ? s.media.renders : [];
  s.media.snapshots = Array.isArray(s.media.snapshots) ? s.media.snapshots : [];
  s.media.music = Array.isArray(s.media.music) ? s.media.music : [];
  s.events = Array.isArray(s.events) ? s.events : [];
  if (s.storyboard && Array.isArray(s.storyboard.scenes)) {
    for (const c of s.storyboard.scenes) {
      c.takes = Array.isArray(c.takes) ? c.takes : [];
      c.audio = Array.isArray(c.audio) ? c.audio : [];
      c.required_assets = Array.isArray(c.required_assets) ? c.required_assets : [];
    }
  } else {
    s.storyboard = null;
  }
  return s;
}

async function refresh() {
  state = normalize(await getJSON(`/api/project/${encodeURIComponent(projectId)}/state`));
  render();
}

refresh().catch((err) => {
  app.innerHTML = "";
  app.append(el("div", { class: "empty", style: "margin-top:80px" },
    el("div", { class: "big" }, "PROJECT NOT FOUND"),
    el("div", {}, String(err))));
});
// ?static=1 disables the live feed (screenshots, static exports).
if (!new URLSearchParams(location.search).has("static")) {
  subscribe(`/api/project/${encodeURIComponent(projectId)}/events`, () => refresh().catch(console.error));
}
