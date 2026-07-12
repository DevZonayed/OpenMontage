// Interactive timeline editor over the CANONICAL timeline.json the Remotion
// render also consumes — so this preview and the final output cannot diverge.
// Provides: a scrub playhead + schematic stage monitor + on-demand real Remotion
// frame render, drag-to-move / drag-to-resize / keyboard-nudge clip editing,
// an inspector, undo/redo, ETag-guarded save, and a read-only agent-queue view.
// All server values go through textContent (XSS-safe).
import { el, getJSON } from "/ui/lib.js";

function projectId() {
  const m = location.pathname.match(/^\/p\/([^/]+)\/editor/);
  return m ? decodeURIComponent(m[1]) : "";
}
function fmtMMSS(s) { return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`; }

const PID = projectId();
document.getElementById("edBoardLink").setAttribute("href", `/p/${encodeURIComponent(PID)}`);

// CSRF-guarded POST that surfaces the JSON body even on error (so a 409 duration
// conflict can carry its impact for the strategy prompt).
let CSRF = null;
async function mpost(url, body) {
  if (!CSRF) CSRF = (await getJSON("/api/csrf")).csrf;
  const r = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json", "X-OpenMontage-CSRF": CSRF },
    body: JSON.stringify(body || {}) });
  let data = null; try { data = await r.json(); } catch {}
  if (!r.ok) {
    const detail = data && data.detail;
    const e = new Error(typeof detail === "string" ? detail : (r.status === 409 ? "This change affects existing content." : `HTTP ${r.status}`));
    e.status = r.status; e.data = data; throw e;
  }
  return data;
}

// Editable target duration with edit-safety: a change that would affect real
// timeline content prompts for trim / extend / queue-agent-replan.
function durationEditor(p) {
  const label = el("span", { style: "font-family:var(--mono,monospace);font-size:11px;color:var(--dim,#8a93a3);text-transform:uppercase;letter-spacing:0.08em" }, "Target duration");
  const val = el("b", { style: "font-size:16px;color:var(--bone,#e9e4d6)" }, fmtMMSS(p.target_duration_seconds));
  const editBtn = el("button", { class: "set-mini", type: "button", style: "padding:1px 8px;font-size:11px" }, "Edit");
  const panel = el("div", { style: "display:none;margin-top:8px;max-width:420px" });
  const wrap = el("div", { style: "display:flex;flex-direction:column;gap:4px" },
    label, el("div", { style: "display:flex;align-items:center;gap:8px" }, val, editBtn), panel);

  let built = false;
  editBtn.onclick = () => {
    panel.style.display = panel.style.display === "none" ? "block" : "none";
    if (panel.style.display === "block" && !built) { built = true; buildPanel(); }
  };
  function buildPanel() {
    const input = el("input", { class: "set-input", type: "text", value: fmtMMSS(p.target_duration_seconds),
      style: "width:110px", placeholder: "M:SS or seconds", "aria-label": "New target duration" });
    const err = el("p", { class: "set-note", role: "alert", style: "color:var(--red);margin:6px 0 0" });
    const saveBtn = el("button", { class: "set-btn", type: "button" }, "Save duration");
    const strategyRow = el("div", { style: "display:none;flex-direction:column;gap:8px;margin-top:10px" });
    panel.append(el("label", { class: "set-note", for: "np-none" }, "New length"),
      el("div", { style: "display:flex;gap:8px;align-items:center;margin-top:4px" }, input, saveBtn), err, strategyRow);

    const submit = async (strategy) => {
      err.textContent = "";
      [...panel.querySelectorAll("button")].forEach((b) => { b.disabled = true; });
      try {
        await mpost(`/api/project/${encodeURIComponent(PID)}/duration`, { duration: input.value.trim(), strategy });
        location.reload();
      } catch (e) {
        if (e.status === 409 && e.data && e.data.detail && e.data.detail.impact) {
          showStrategy(e.data.detail.impact);
        } else {
          err.textContent = e.message || "Could not change the duration.";
        }
        [...panel.querySelectorAll("button")].forEach((b) => { b.disabled = false; });
      }
    };
    saveBtn.onclick = () => submit(undefined);

    function showStrategy(impact) {
      strategyRow.textContent = "";
      strategyRow.style.display = "flex";
      strategyRow.append(el("p", { class: "set-note", style: "color:var(--warn,#e8c07d)" },
        `Changing ${impact.old_formatted} → ${impact.new_formatted} (${impact.old_frames} → ${impact.new_frames} frames, ` +
        `${impact.frame_delta > 0 ? "+" : ""}${impact.frame_delta}). This affects existing timeline content — choose how:`));
      const btns = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" });
      const mk = (labelText, strat, hint) => {
        const b = el("button", { class: "set-mini", type: "button", title: hint }, labelText);
        b.onclick = () => submit(strat);
        return b;
      };
      btns.append(
        mk("Trim", "trim", "Shorten: drop/clamp content past the new end"),
        mk("Extend", "extend", "Lengthen: keep content, add empty tail"),
        mk("Queue agent replan", "replan", "Preserve content; flag the timeline for the agent to re-plan"));
      strategyRow.append(btns);
    }
  }
  return wrap;
}

const TYPE_COLORS = {
  video: "#4f7cff", image: "#37a0a0", text: "#e8c07d", shape: "#8a7dd6",
  caption: "#d68ab0", narration: "#5ec27a", music: "#c9772a", sfx: "#b0574f",
};

function ruler(totalFrames, fps) {
  const secs = Math.max(1, Math.round(totalFrames / fps));
  const row = el("div", { class: "ed-ruler", style: "position:relative;height:22px;border-bottom:1px solid var(--line,#232a36);margin-bottom:6px" });
  // a tick every ceil(secs/12) seconds, so long timelines stay readable
  const step = Math.max(1, Math.ceil(secs / 12));
  for (let s = 0; s <= secs; s += step) {
    const left = (s / secs) * 100;
    row.append(el("span", {
      style: `position:absolute;left:${left}%;top:0;font-family:var(--mono,monospace);font-size:11px;color:var(--dim,#8a93a3);transform:translateX(-2px)`,
    }, fmtMMSS(s)));
    row.append(el("i", { style: `position:absolute;left:${left}%;top:16px;width:1px;height:6px;background:var(--line,#232a36)` }));
  }
  return row;
}

const LAYER_TYPES = ["video", "image", "text", "shape", "caption", "narration", "music", "sfx"];
// Editable timeline state (deterministic edits — no AI). Server validates + ETag.
const ST = { p: null, tl: null, etag: null, dirty: false, selected: null, status: "", playhead: 0, inbox: null, history: [], future: [] };

// A consolidated, honest view of what's queued for the agent (Rule Zero): queued
// layer regenerations, a pending duration re-plan, the run approval state. Read-only.
function agentQueueCard(inbox) {
  const card = el("div", { style: "background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:8px;padding:12px 14px;margin-bottom:14px" });
  const n = inbox ? inbox.count : 0;
  const head = el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px" });
  head.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--dim,#8a93a3)" }, "Agent queue"));
  head.append(el("span", { style: `font-family:var(--mono,monospace);font-size:11px;padding:2px 9px;border-radius:10px;border:1px solid ${n ? "var(--amber,#e8c07d)" : "var(--line,#232a36)"};color:${n ? "var(--amber,#e8c07d)" : "var(--dim,#8a93a3)"}` }, `${n} queued`));
  card.append(head);
  if (!inbox) { card.append(el("p", { class: "set-note" }, "Queue status unavailable.")); return card; }
  card.append(el("p", { class: "set-note", style: "margin-bottom:8px" }, inbox.summary));
  if (n === 0) return card;
  const list = el("div", { style: "display:flex;flex-direction:column;gap:6px" });
  for (const r of (inbox.revisions || [])) {
    list.append(el("div", { style: "font-size:13px;color:var(--bone,#e9e4d6)" },
      el("span", { style: "color:var(--amber,#e8c07d)" }, "✦ "),
      `${r.layer_type || "layer"} ${r.layer_id} — `,
      el("i", { style: "color:var(--dim,#8a93a3)" }, `"${String(r.prompt || "").slice(0, 90)}"`)));
  }
  if (inbox.replan) list.append(el("div", { style: "font-size:13px;color:var(--bone,#e9e4d6)" },
    "⟳ Duration re-plan requested — the agent will rebuild the timeline to the new length."));
  if (inbox.approval) list.append(el("div", { style: "font-size:13px;color:var(--bone,#e9e4d6)" },
    inbox.approval.needs === "agent" ? "▶ Plan approved — awaiting the agent to produce."
      : "⏳ Plan awaiting your approval on the board."));
  card.append(list);
  card.append(el("p", { class: "set-note", style: "margin-top:8px;color:var(--dim,#8a93a3)" },
    "Honest, machine-readable requests the agent consumes — Backlot never generates media itself (Rule Zero)."));
  return card;
}

function tc(frame, fps) {  // timecode "M:SS·FF"
  const f = Math.max(0, Math.round(frame));
  const s = Math.floor(f / fps);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}·${String(f % fps).padStart(2, "0")}`;
}

// Update playhead visuals WITHOUT a full repaint (smooth scrub): the line, the
// readout, and the "active at this frame" highlight on clips.
// Valid frame indices are [0, total-1]; the playhead must never sit on `total`
// (that frame doesn't exist — the render clamps it, so readout/overlay/render
// would describe a different frame than the playhead shows).
function _maxFrame() { return Math.max(0, ((ST.tl && ST.tl.total_frames) || 1) - 1); }

function updatePlayhead() {
  const total = (ST.tl && ST.tl.total_frames) || 1;
  const fps = (ST.tl && ST.tl.fps) || 30;
  const ph = Math.max(0, Math.min(_maxFrame(), ST.playhead));
  const pct = (ph / total) * 100;
  const line = document.getElementById("ed-playhead");
  if (line) line.style.left = `${pct}%`;
  const handle = document.getElementById("ed-scrub-handle");
  if (handle) handle.style.left = `${pct}%`;
  let active = 0;
  for (const clip of document.querySelectorAll(".ed-clip")) {
    const start = parseInt(clip.dataset.start, 10), dur = parseInt(clip.dataset.dur, 10);
    const on = ph >= start && ph < start + dur;
    if (on) active += 1;
    clip.style.boxShadow = (ST.selected === clip.dataset.id) ? "0 0 0 2px var(--amber,#e8c07d)"
      : (on ? "0 0 0 2px var(--ok,#5ec27a)" : "none");
  }
  const out = document.getElementById("ed-playhead-readout");
  if (out) out.textContent = `Playhead ${tc(ph, fps)} · frame ${ph} / ${total} · ${active} layer${active === 1 ? "" : "s"} active`;
  drawStage();
}

const _AUDIO_TYPES = new Set(["narration", "music", "sfx"]);

// A live "stage monitor": a schematic composite of the layers active at the
// playhead frame, drawn by z-order onto a 16:9 canvas. NOT the final render —
// it's an honest structural preview so scrubbing shows what's on screen. The
// Remotion render (same timeline.json) remains the source of the real pixels.
function drawStage() {
  const cv = document.getElementById("ed-stage");
  if (!cv || !ST.tl) return;
  // A rendered real frame is only valid for the frame it was rendered at — hide
  // it whenever the playhead moves so the schematic is the source of truth again.
  const realImg = document.getElementById("ed-stage-real");
  if (realImg && realImg.style.display !== "none") realImg.style.display = "none";
  const ctx = cv.getContext("2d");
  if (!ctx) return;
  const W = 480, H = 270;
  ctx.setTransform(2, 0, 0, 2, 0, 0);     // backing store is 960×540 for crispness
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0a0e18"; ctx.fillRect(0, 0, W, H);

  const ph = ST.playhead, fps = ST.tl.fps || 30;
  const on = (ST.tl.layers || []).filter((L) => L.enabled !== false && ph >= L.start_frame && ph < L.start_frame + L.duration_frames);
  on.sort((a, b) => (a.z || 0) - (b.z || 0));   // low z first (background) → high z on top

  let visible = 0; const audio = [];
  for (const L of on) {
    const color = TYPE_COLORS[L.type] || "#8a93a3";
    if (_AUDIO_TYPES.has(L.type)) { audio.push(L); continue; }
    visible += 1;
    const op = typeof L.opacity === "number" ? Math.max(0, Math.min(1, L.opacity)) : 1;
    if (L.type === "text" || L.type === "caption") {
      ctx.globalAlpha = op;
      ctx.font = "600 20px system-ui, sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const y = L.type === "caption" ? H - 42 : H / 2;
      const txt = String(L.text || L.type).slice(0, 60);
      ctx.fillStyle = "rgba(0,0,0,0.65)"; ctx.fillText(txt, W / 2 + 1, y + 1);
      ctx.fillStyle = color; ctx.fillText(txt, W / 2, y);
    } else {
      ctx.globalAlpha = op * 0.28;
      ctx.fillStyle = color; ctx.fillRect(12, 12, W - 24, H - 24);
      ctx.globalAlpha = op;
      ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.strokeRect(12, 12, W - 24, H - 24);
      ctx.fillStyle = color; ctx.font = "600 12px ui-monospace, monospace";
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(L.type.toUpperCase() + (L.text ? " · " + String(L.text).slice(0, 24) : ""), 20, 20);
    }
    ctx.globalAlpha = 1;
  }

  if (!visible) {
    ctx.fillStyle = "#3a4356"; ctx.font = "500 14px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("— no visible layers at this frame —", W / 2, H / 2);
  }
  let bx = 14;
  for (const L of audio) {
    const color = TYPE_COLORS[L.type] || "#8a93a3";
    const label = (L.type === "narration" ? "🎙 " : "♪ ") + L.type;
    ctx.font = "500 11px system-ui, sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    const w = ctx.measureText(label).width + 14;
    ctx.globalAlpha = 0.18; ctx.fillStyle = color; ctx.fillRect(bx, H - 26, w, 17);
    ctx.globalAlpha = 1; ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.strokeRect(bx, H - 26, w, 17);
    ctx.fillStyle = color; ctx.fillText(label, bx + 7, H - 17);
    bx += w + 8;
  }
  ctx.fillStyle = "rgba(233,228,214,0.85)"; ctx.font = "500 11px ui-monospace, monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "top";
  ctx.fillText(`f${ph} · ${tc(ph, fps)}`, W - 12, 12);
  ctx.setTransform(1, 0, 0, 1, 0, 0);

  cv.dataset.frame = String(ph); cv.dataset.visible = String(visible); cv.dataset.audio = String(audio.length);
  const leg = document.getElementById("ed-stage-legend");
  if (leg) leg.textContent = `Frame ${ph} · ${visible} visible layer${visible === 1 ? "" : "s"}, ${audio.length} audio`;
}

function stageMonitor() {
  const card = el("div", { style: "background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:8px;padding:12px 14px;margin-bottom:14px" });
  card.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--dim,#8a93a3);margin-bottom:8px" }, "Stage monitor"));
  // Canvas (schematic) with the real rendered frame overlaid on top when present.
  const stack = el("div", { style: "position:relative;width:100%;max-width:480px" });
  stack.append(el("canvas", { id: "ed-stage", width: "960", height: "540",
    style: "width:100%;aspect-ratio:16/9;border:1px solid var(--line,#232a36);border-radius:6px;display:block;background:#0a0e18" }));
  stack.append(el("img", { id: "ed-stage-real", alt: "rendered frame",
    style: "display:none;position:absolute;inset:0;width:100%;height:100%;object-fit:contain;border:1px solid var(--ok,#5ec27a);border-radius:6px;background:#0a0e18" }));
  card.append(stack);
  card.append(el("p", { id: "ed-stage-legend", class: "set-note", style: "margin-top:8px" }, ""));

  const bar = el("div", { style: "display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap" });
  const btn = el("button", { class: "set-mini", type: "button", title: "Render this exact frame with Remotion (free, local)" }, "⤓ Render real frame");
  const status = el("span", { class: "set-note", role: "status" });
  btn.onclick = async () => {
    if (ST.dirty) { status.textContent = "Save your edits first — the render reads the saved timeline."; status.style.color = "var(--warn,#e8c07d)"; return; }
    btn.disabled = true; status.style.color = "var(--dim,#8a93a3)"; status.textContent = `Rendering frame ${ST.playhead}…`;
    try {
      const r = await mpost(`/api/project/${encodeURIComponent(PID)}/frame`, { frame: ST.playhead });
      const img = document.getElementById("ed-stage-real");
      img.onload = () => { img.style.display = "block"; };
      img.src = `${r.url}?t=${Date.now()}`;
      status.style.color = "var(--ok,#5ec27a)";
      status.textContent = `Real frame ${r.frame} · ${Math.max(1, Math.round((r.size_bytes || 0) / 1024))} KB`;
    } catch (e) {
      status.style.color = "var(--red,#e06c75)";
      status.textContent = e.message || "Render failed.";
    } finally { btn.disabled = false; }
  };
  const schematicBtn = el("button", { class: "set-mini", type: "button", title: "Hide the rendered frame, show the live schematic" }, "Show schematic");
  schematicBtn.onclick = () => { const img = document.getElementById("ed-stage-real"); if (img) img.style.display = "none"; };

  // ── Complete Remotion render → a real, playable/scrubbable film ──────────────
  const filmBtn = el("button", { class: "set-btn", type: "button", title: "Render the WHOLE timeline to a real video with Remotion (free, local)" }, "▶ Render preview film");
  filmBtn.onclick = async () => {
    if (ST.dirty) { status.textContent = "Save your edits first — the render reads the saved timeline."; status.style.color = "var(--warn,#e8c07d)"; return; }
    filmBtn.disabled = true; btn.disabled = true;
    status.style.color = "var(--dim,#8a93a3)";
    const t0 = Date.now();
    const tick = setInterval(() => { status.textContent = `Rendering the full timeline with Remotion… ${Math.round((Date.now() - t0) / 1000)}s`; }, 500);
    status.textContent = "Rendering the full timeline with Remotion…";
    try {
      const r = await mpost(`/api/project/${encodeURIComponent(PID)}/timeline/render`, {});
      clearInterval(tick);
      const wrap = document.getElementById("ed-film");
      wrap.textContent = "";
      const v = el("video", { controls: "", autoplay: "", muted: "", playsinline: "",
        style: "width:100%;max-width:640px;border:1px solid var(--ok,#5ec27a);border-radius:8px;display:block;background:#000" });
      v.src = `${r.url}?t=${Date.now()}`;
      const fps = r.fps || 30;
      const cap = el("p", { class: "set-note", style: "margin-top:6px;color:var(--ok,#5ec27a)" },
        `Complete Remotion render · ${r.measured_seconds != null ? r.measured_seconds + "s" : Math.round(r.frames_rendered / fps) + "s"}` +
        (r.truncated ? ` — preview of the first ${Math.round(r.frames_rendered / fps)}s of ${Math.round(r.total_frames / fps)}s (full render on production)` : " — the whole timeline"));
      wrap.append(v, cap);
      wrap.style.display = "block";
      wrap.scrollIntoView({ behavior: "smooth", block: "nearest" });
      status.style.color = "var(--ok,#5ec27a)";
      status.textContent = `Rendered ${r.frames_rendered} frames (${Math.max(1, Math.round((r.size_bytes || 0) / 1024))} KB).`;
    } catch (e) {
      clearInterval(tick);
      status.style.color = "var(--red,#e06c75)";
      status.textContent = e.message || "Timeline render failed.";
    } finally { filmBtn.disabled = false; btn.disabled = false; }
  };

  bar.append(filmBtn, btn, schematicBtn, status);
  card.append(bar);
  card.append(el("div", { id: "ed-film", style: "display:none;margin-top:12px" }));

  card.append(el("p", { class: "set-note", style: "margin-top:8px;color:var(--dim,#8a93a3)" },
    "The live canvas is a schematic composite at the playhead. “Render preview film” runs Remotion on the saved " +
    "timeline.json (free, local) and gives you the real, playable video — the exact renderer and pixels the final " +
    "film uses. “Render real frame” overlays a single actual frame at the playhead."));
  return card;
}

function setPlayhead(frame) {
  ST.playhead = Math.max(0, Math.min(_maxFrame(), Math.round(frame)));
  updatePlayhead();
}

function scrubberRow(total) {
  const bar = el("div", { id: "ed-scrub-bar", role: "slider", tabindex: "0",
    "aria-label": "Playhead (scrub)", "aria-valuemin": "0", "aria-valuemax": String(total),
    style: "position:relative;height:20px;background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:5px;cursor:ew-resize" });
  bar.append(el("div", { id: "ed-scrub-handle",
    style: "position:absolute;top:-3px;bottom:-3px;width:3px;background:var(--amber,#e8c07d);left:0;transform:translateX(-50%);pointer-events:none;border-radius:2px" }));
  const frameFromEvent = (clientX) => {
    const r = bar.getBoundingClientRect();
    return ((clientX - r.left) / Math.max(1, r.width)) * total;
  };
  let dragging = false;
  const move = (ev) => { if (dragging) setPlayhead(frameFromEvent(ev.clientX)); };
  const up = () => { dragging = false; window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  bar.addEventListener("pointerdown", (ev) => {
    dragging = true; setPlayhead(frameFromEvent(ev.clientX));
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  });
  bar.addEventListener("keydown", (ev) => {
    const step = ev.shiftKey ? ((ST.tl.fps || 30)) : 1;
    if (ev.key === "ArrowRight") { ev.preventDefault(); setPlayhead(ST.playhead + step); }
    else if (ev.key === "ArrowLeft") { ev.preventDefault(); setPlayhead(ST.playhead - step); }
    else if (ev.key === "Home") { ev.preventDefault(); setPlayhead(0); }
    else if (ev.key === "End") { ev.preventDefault(); setPlayhead(total); }
  });
  return el("div", { style: "display:grid;grid-template-columns:120px 1fr;gap:10px;align-items:center;margin:2px 0 8px" },
    el("span", { id: "ed-playhead-readout", style: "font-family:var(--mono,monospace);font-size:11px;color:var(--dim,#8a93a3)" }, ""),
    bar);
}

function clipBlock(layer, totalFrames) {
  const left = (layer.start_frame / totalFrames) * 100;
  const width = Math.max(0.6, (layer.duration_frames / totalFrames) * 100);
  const color = TYPE_COLORS[layer.type] || "#666";
  const dim = layer.enabled === false ? "opacity:0.4;" : "";
  const sel = ST.selected === layer.id ? "box-shadow:0 0 0 2px var(--amber,#e8c07d);" : "";
  const cursor = layer.locked ? "pointer" : "grab";
  const block = el("div", {
    class: "ed-clip", tabindex: "0", role: "button",
    "data-id": layer.id, "data-start": String(layer.start_frame), "data-dur": String(layer.duration_frames),
    "aria-label": `${layer.type} ${layer.id}`,
    title: `${layer.type} · ${layer.id} · frames ${layer.start_frame}–${layer.start_frame + layer.duration_frames}` +
      (layer.locked ? "" : " · drag to move, drag edges to resize"),
    style: `position:absolute;left:${left}%;width:${width}%;top:3px;bottom:3px;${dim}${sel}` +
      `background:${color}22;border:1px solid ${color};border-radius:5px;overflow:hidden;cursor:${cursor};` +
      `display:flex;align-items:center;padding:0 8px;box-sizing:border-box`,
  });
  block.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectLayer(layer.id); return; }
    if (layer.locked) return;
    const step = e.shiftKey ? (ST.tl.fps || 30) : 1;
    if (e.key === "ArrowRight") { e.preventDefault(); nudgeLayer(layer, step); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); nudgeLayer(layer, -step); }
  };
  const queued = layer.revision && layer.revision.status ? " ✦" : "";
  block.append(el("span", { style: "font-size:12px;color:var(--bone,#e9e4d6);white-space:nowrap;text-overflow:ellipsis;overflow:hidden;pointer-events:none" },
    `${layer.type}${layer.locked ? " 🔒" : ""}${queued}`));
  if (layer.locked) { block.onclick = () => selectLayer(layer.id); }
  else { attachClipDrag(block, layer, totalFrames); }
  return block;
}

// Keyboard nudge: move a clip by ±frames (clamped), commit + repaint.
function nudgeLayer(layer, deltaFrames) {
  const total = ST.tl.total_frames || 1;
  mutate(() => {
    layer.start_frame = Math.max(0, Math.min(total - layer.duration_frames, layer.start_frame + deltaFrames));
    ST.selected = layer.id;
  });
  const el2 = document.querySelector(`.ed-clip[data-id="${CSS.escape(layer.id)}"]`);
  if (el2) el2.focus();
}

// Drag-to-move (grab body) and drag-to-resize (edge handles). Live visual update
// during the drag; commit to the canonical model + repaint only on pointerup so
// edits still flow through the validated, ETag-guarded Save. Frame-accurate + clamped.
function attachClipDrag(block, layer, totalFrames) {
  const HANDLE = "position:absolute;top:0;bottom:0;width:8px;cursor:ew-resize;z-index:2;";
  const leftH = el("div", { "data-h": "l", style: HANDLE + "left:0;border-left:2px solid rgba(255,255,255,0.35)" });
  const rightH = el("div", { "data-h": "r", style: HANDLE + "right:0;border-right:2px solid rgba(255,255,255,0.35)" });
  block.append(leftH, rightH);

  let mode = null, startX = 0, laneW = 1, dragged = false;
  let origStart = layer.start_frame, origDur = layer.duration_frames;
  let curStart = origStart, curDur = origDur;

  const begin = (ev, m) => {
    mode = m; startX = ev.clientX; dragged = false;
    origStart = layer.start_frame; origDur = layer.duration_frames;
    curStart = origStart; curDur = origDur;
    laneW = Math.max(1, block.parentElement.getBoundingClientRect().width);
    if (block.setPointerCapture) { try { block.setPointerCapture(ev.pointerId); } catch (_) {} }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    ev.preventDefault();
  };
  const onMove = (ev) => {
    if (Math.abs(ev.clientX - startX) > 3) dragged = true;
    if (!dragged) return;
    const df = Math.round(((ev.clientX - startX) / laneW) * totalFrames);
    if (mode === "move") {
      curStart = Math.max(0, Math.min(totalFrames - origDur, origStart + df));
      curDur = origDur;
    } else if (mode === "resize-r") {
      curStart = origStart;
      curDur = Math.max(1, Math.min(totalFrames - origStart, origDur + df));
    } else { // resize-l
      const origEnd = origStart + origDur;
      curStart = Math.max(0, Math.min(origEnd - 1, origStart + df));
      curDur = origEnd - curStart;
    }
    block.style.left = `${(curStart / totalFrames) * 100}%`;
    block.style.width = `${Math.max(0.6, (curDur / totalFrames) * 100)}%`;
    block.style.boxShadow = "0 0 0 2px var(--amber,#e8c07d)";
    block.style.cursor = mode === "move" ? "grabbing" : "ew-resize";
  };
  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    if (!dragged) { selectLayer(layer.id); return; }
    mutate(() => {
      layer.start_frame = curStart; layer.duration_frames = curDur;
      ST.selected = layer.id;
    });
  };

  block.addEventListener("pointerdown", (ev) => begin(ev, "move"));
  leftH.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); begin(ev, "resize-l"); });
  rightH.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); begin(ev, "resize-r"); });
}

function trackRow(name, layers, totalFrames) {
  const lane = el("div", { style: "position:relative;height:40px;background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:6px;margin-bottom:6px" });
  for (const L of layers) lane.append(clipBlock(L, totalFrames));
  return el("div", { style: "display:grid;grid-template-columns:120px 1fr;gap:10px;align-items:center;margin-bottom:2px" },
    el("span", { style: "font-family:var(--mono,monospace);font-size:12px;color:var(--dim,#8a93a3)" }, name),
    lane);
}

let _uid = 0;
function newLayer() {
  const total = (ST.tl && ST.tl.total_frames) || 900;
  return { id: `layer${Date.now().toString(36)}${_uid++}`, type: "text", track: 0,
    start_frame: 0, duration_frames: Math.min(90, total), z: (ST.tl.layers || []).length,
    enabled: true, locked: false, opacity: 1.0, text: "New text" };
}

function selectLayer(id) { ST.selected = id; paint(); }
function markDirty() { ST.dirty = true; ST.status = ""; }

// Undo/redo: snapshot the timeline BEFORE each edit, then apply. Snapshots are
// plain JSON clones so undo/redo fully restore layer geometry, flags, text, etc.
const _HIST_MAX = 60;
function mutate(fn) {
  ST.history.push(JSON.stringify(ST.tl));
  if (ST.history.length > _HIST_MAX) ST.history.shift();
  ST.future = [];
  fn();
  ST.dirty = true; ST.status = "";
  paint();
}
function _restore(fromStack, toStack) {
  if (!fromStack.length) return;
  toStack.push(JSON.stringify(ST.tl));
  ST.tl = JSON.parse(fromStack.pop());
  if (!Array.isArray(ST.tl.layers)) ST.tl.layers = [];
  if (!ST.tl.layers.some((L) => L.id === ST.selected)) ST.selected = null;
  ST.dirty = true; ST.status = "";
  paint();
}
function undo() { _restore(ST.history, ST.future); }
function redo() { _restore(ST.future, ST.history); }

function inspector() {
  const L = (ST.tl.layers || []).find((x) => x.id === ST.selected);
  const box = el("div", { style: "background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:8px;padding:14px 16px" });
  box.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--dim,#8a93a3);margin-bottom:10px" }, "Inspector"));
  if (!L) { box.append(el("p", { class: "set-note" }, "Select a layer to edit it, or add one.")); return box; }

  const locked = !!L.locked;
  const field = (label, control) => el("div", { style: "display:flex;flex-direction:column;gap:3px;margin-bottom:10px" },
    el("label", { class: "set-note" }, label), control);
  const numInput = (val, on) => {
    const i = el("input", { class: "set-input", type: "number", value: String(val), style: "width:120px" });
    if (locked) i.setAttribute("disabled", "true");
    i.addEventListener("change", () => { const v = parseInt(i.value, 10); if (Number.isFinite(v)) { on(v); } });
    return i;
  };

  const typeSel = el("select", { class: "set-select" });
  for (const t of LAYER_TYPES) { const o = el("option", { value: t }, t); if (t === L.type) o.setAttribute("selected", "true"); typeSel.append(o); }
  if (locked) typeSel.setAttribute("disabled", "true");
  typeSel.addEventListener("change", () => mutate(() => { L.type = typeSel.value; }));
  box.append(field("Type", typeSel));

  if (["text", "caption"].includes(L.type)) {
    const ta = el("input", { class: "set-input", type: "text", value: L.text || "", placeholder: "on-screen text" });
    if (locked) ta.setAttribute("disabled", "true");
    ta.addEventListener("change", () => mutate(() => { L.text = ta.value; }));
    box.append(field("Text", ta));
  }
  box.append(field("Start frame", numInput(L.start_frame, (v) => mutate(() => { L.start_frame = Math.max(0, v); }))));
  box.append(field("Duration (frames)", numInput(L.duration_frames, (v) => mutate(() => { L.duration_frames = Math.max(1, v); }))));
  box.append(field("Track", numInput(L.track || 0, (v) => mutate(() => { L.track = Math.max(0, v); }))));
  box.append(field("Z-order", numInput(L.z || 0, (v) => mutate(() => { L.z = v; }))));

  const flags = el("div", { style: "display:flex;gap:18px;margin:6px 0 12px" });
  const cb = (label, key, disabledWhenLocked) => {
    const c = el("input", { type: "checkbox" }); if (L[key]) c.setAttribute("checked", "true");
    if (disabledWhenLocked && locked) c.setAttribute("disabled", "true");
    c.addEventListener("change", () => mutate(() => { L[key] = c.checked; }));
    return el("label", { style: "display:flex;gap:6px;align-items:center;font-size:13px;color:var(--bone,#e9e4d6)" }, c, label);
  };
  flags.append(cb("Enabled", "enabled", true), cb("Locked", "locked", false));
  box.append(flags);

  // ── Honest AI regeneration: a QUEUED agent request, never a fake toast ──
  const rev = L.revision;
  if (rev && rev.status) {
    box.append(el("div", { style: "margin:8px 0;padding:8px 10px;border:1px solid var(--amber,#e8c07d);border-radius:6px;background:#1a1710" },
      el("span", { style: "font-size:13px;color:var(--amber,#e8c07d)" }, `✦ Regeneration ${rev.status} for agent`)));
  }
  const regen = el("div", { style: "margin:10px 0" });
  const regenBtn = el("button", { class: "set-mini", type: "button" }, "✦ Regenerate this layer with AI");
  const regenPanel = el("div", { style: "display:none;margin-top:8px" });
  if (locked) regenBtn.setAttribute("disabled", "true");
  regenBtn.onclick = () => {
    if (ST.dirty) { regenPanel.style.display = "block"; regenPanel.textContent = ""; regenPanel.append(el("p", { class: "set-note", style: "color:var(--warn,#e8c07d)" }, "Save your changes first, then queue a regeneration.")); return; }
    regenPanel.style.display = regenPanel.style.display === "none" ? "block" : "none";
    if (regenPanel.style.display === "block" && !regenPanel.dataset.built) buildRegen();
  };
  function buildRegen() {
    regenPanel.dataset.built = "1";
    const ta = el("textarea", { class: "set-input", rows: "3", maxlength: "4000",
      placeholder: "Describe how to regenerate this layer (e.g. 'a warmer sunset palette, slower fade')…" });
    const err = el("p", { class: "set-note", role: "alert", style: "color:var(--red);margin:6px 0 0" });
    const queueBtn = el("button", { class: "set-btn", type: "button" }, "Queue for agent");
    const note = el("p", { class: "set-note", style: "margin-top:6px" },
      "This does not generate anything now — it records a request the agent will pick up and regenerate only this layer (Rule Zero).");
    queueBtn.onclick = async () => {
      err.textContent = "";
      if (!ta.value.trim()) { err.textContent = "Enter a prompt."; return; }
      queueBtn.disabled = true;
      try {
        await mpost(`/api/project/${encodeURIComponent(PID)}/timeline/revision`, { layer_id: L.id, prompt: ta.value.trim() });
        await load();  // re-fetch: the layer now shows 'queued'
      } catch (e) { err.textContent = e.message || "Could not queue the request."; queueBtn.disabled = false; }
    };
    regenPanel.append(el("label", { class: "set-note" }, "Regeneration prompt"), ta, queueBtn, err, note);
  }
  regen.append(regenBtn, regenPanel);
  box.append(regen);

  const del = el("button", { class: "set-mini", type: "button", style: "border-color:var(--red);color:var(--red)" }, "Delete layer");
  if (locked) del.setAttribute("disabled", "true");
  del.onclick = () => mutate(() => { ST.tl.layers = ST.tl.layers.filter((x) => x.id !== L.id); ST.selected = null; });
  box.append(del);
  if (locked) box.append(el("p", { class: "set-note", style: "margin-top:8px" }, "🔒 Locked — uncheck Locked to edit."));
  return box;
}

async function saveTimeline(statusEl) {
  statusEl.textContent = "Saving…";
  try {
    const r = await mpost(`/api/project/${encodeURIComponent(PID)}/timeline`, { timeline: ST.tl, if_match: ST.etag });
    ST.etag = r.etag; ST.dirty = false; ST.status = "Saved ✓"; paint();
  } catch (e) {
    if (e.status === 409) { ST.status = "conflict"; paint(); }
    else { statusEl.textContent = e.message || "Could not save."; statusEl.style.color = "var(--red)"; }
  }
}

function summaryBar(p) {
  const target = fmtMMSS(p.target_duration_seconds);
  const measured = p.measured_output_seconds != null ? `${p.measured_output_seconds}s` : "not rendered yet";
  const chip = (label, value) => el("div", { style: "display:flex;flex-direction:column;gap:2px" },
    el("span", { style: "font-family:var(--mono,monospace);font-size:11px;color:var(--dim,#8a93a3);text-transform:uppercase;letter-spacing:0.08em" }, label),
    el("b", { style: "font-size:16px;color:var(--bone,#e9e4d6)" }, value));
  return el("div", { style: "display:flex;gap:32px;flex-wrap:wrap;margin:6px 0 20px;padding:14px 18px;background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:8px" },
    durationEditor(p),
    chip("Total frames", `${p.total_frames} @ ${p.fps}fps`),
    chip("Narration budget", `≈ ${p.word_budget} words`),
    chip("Measured output", measured));
}

function layerList() {
  const layers = ST.tl.layers || [];
  const box = el("div", { style: "background:var(--panel,#12151c);border:1px solid var(--line,#232a36);border-radius:8px;padding:10px;max-height:360px;overflow:auto" });
  box.append(el("div", { style: "font-family:var(--mono,monospace);font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--dim,#8a93a3);margin-bottom:8px" }, `Layers (${layers.length})`));
  if (!layers.length) box.append(el("p", { class: "set-note" }, "No layers yet — click “+ Add layer”."));
  for (const L of layers) {
    const active = ST.selected === L.id;
    const row = el("div", { role: "button", tabindex: "0",
      style: `display:flex;justify-content:space-between;gap:8px;padding:7px 9px;border-radius:6px;cursor:pointer;margin-bottom:4px;` +
        `border:1px solid ${active ? "var(--amber,#e8c07d)" : "transparent"};background:${active ? "#1a1f2b" : "transparent"}` });
    row.onclick = () => selectLayer(L.id);
    row.onkeydown = (e) => { if (e.key === "Enter") selectLayer(L.id); };
    const color = TYPE_COLORS[L.type] || "#666";
    const regenMark = (L.revision && L.revision.status) ? " ✦" : "";
    row.append(el("span", { style: "display:flex;gap:8px;align-items:center;font-size:13px;color:var(--bone,#e9e4d6)" },
      el("i", { style: `width:9px;height:9px;border-radius:2px;background:${color};display:inline-block` }),
      `${L.type}${L.text ? " · " + String(L.text).slice(0, 18) : ""}${L.locked ? " 🔒" : ""}${L.enabled === false ? " (off)" : ""}${regenMark}`));
    row.append(el("span", { class: "mono", style: "font-size:11px;color:var(--dim,#8a93a3)" }, `${L.start_frame}–${L.start_frame + L.duration_frames}`));
    box.append(row);
  }
  return box;
}

function toolbar() {
  const bar = el("div", { style: "display:flex;gap:10px;align-items:center;margin:6px 0 14px;flex-wrap:wrap" });
  const add = el("button", { class: "set-btn", type: "button" }, "+ Add layer");
  add.onclick = () => mutate(() => { const L = newLayer(); ST.tl.layers = [...(ST.tl.layers || []), L]; ST.selected = L.id; });
  const undoBtn = el("button", { class: "set-mini", type: "button", title: "Undo (⌘Z)" }, "↶ Undo");
  undoBtn.disabled = !ST.history.length;
  undoBtn.onclick = () => undo();
  const redoBtn = el("button", { class: "set-mini", type: "button", title: "Redo (⌘⇧Z)" }, "↷ Redo");
  redoBtn.disabled = !ST.future.length;
  redoBtn.onclick = () => redo();
  const save = el("button", { class: "set-btn", type: "button" }, "Save changes");
  save.disabled = !ST.dirty;
  const status = el("span", { class: "set-note", role: "status" });
  save.onclick = () => saveTimeline(status);
  bar.append(add, undoBtn, redoBtn, save, status);
  if (ST.status === "Saved ✓") { status.textContent = "Saved ✓"; status.style.color = "var(--ok,#5ec27a)"; }
  else if (ST.status === "conflict") {
    status.textContent = "Timeline changed elsewhere — ";
    status.style.color = "var(--warn,#e8c07d)";
    const rl = el("button", { class: "set-mini", type: "button" }, "Reload");
    rl.onclick = () => load();
    status.append(rl);
  } else if (ST.dirty) { status.textContent = "Unsaved changes"; status.style.color = "var(--warn,#e8c07d)"; }
  return bar;
}

function paint() {
  const body = document.getElementById("edBody");
  body.textContent = "";
  const p = ST.p, tl = ST.tl;
  document.getElementById("edTitle").textContent = `${PID} — timeline`;
  document.getElementById("edTarget").textContent = `TARGET ${p.target_formatted}`;
  const rt = document.getElementById("edRuntime");
  rt.textContent = p.remotion_render_ready ? "● REMOTION RENDER-READY" : "● REMOTION NOT READY";
  rt.style.color = p.remotion_render_ready ? "var(--ok,#5ec27a)" : "var(--warn,#e8c07d)";

  body.append(summaryBar(p));
  body.append(toolbar());
  body.append(agentQueueCard(ST.inbox));
  body.append(stageMonitor());

  const layers = tl.layers || [];
  const total = tl.total_frames || 1;
  const surface = el("div", { style: "padding:14px 16px;background:var(--night-2,#0c1224);border:1px solid var(--line,#232a36);border-radius:8px" });
  surface.append(ruler(total, tl.fps || 30));
  surface.append(scrubberRow(total));
  if (!layers.length) {
    surface.append(el("p", { class: "set-note" },
      "Frame-accurate to the target duration, no layers yet. Add one with “+ Add layer”, or run the agent to populate it. " +
      "Preview and the Remotion render read this same timeline.json."));
    surface.append(trackRow("track 0", [], total));
  } else {
    const byTrack = new Map();
    for (const L of layers) { const k = L.track ?? 0; if (!byTrack.has(k)) byTrack.set(k, []); byTrack.get(k).push(L); }
    for (const k of [...byTrack.keys()].sort((a, b) => a - b)) surface.append(trackRow(`track ${k}`, byTrack.get(k), total));
  }
  body.append(surface);

  const cols = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px;align-items:start" });
  cols.append(layerList(), inspector());
  body.append(cols);

  body.append(el("p", { class: "set-note", style: "margin-top:16px" },
    "Deterministic edits — drag a clip to move it, drag its edges to resize, or use the inspector for exact " +
    "frames — save to the canonical timeline.json via the validated, ETag-guarded API (the same file the Remotion " +
    "render reads). Scrub the bar under the ruler to see what's on screen at any frame. Regenerating a layer with " +
    "AI is a queued agent revision (Rule Zero), not a fake toast."));

  ST.playhead = Math.max(0, Math.min(_maxFrame(), ST.playhead));  // keep in range if the timeline shrank
  updatePlayhead();
}

async function load() {
  const prevSelected = ST.selected;
  try {
    ST.p = await getJSON(`/api/project/${encodeURIComponent(PID)}/timeline`);
  } catch (err) {
    document.getElementById("edBody").append(el("p", { class: "set-note warn" }, "Could not load the timeline for this project."));
    return;
  }
  ST.tl = ST.p.timeline || { fps: 30, total_frames: 1, layers: [] };
  if (!Array.isArray(ST.tl.layers)) ST.tl.layers = [];
  ST.etag = ST.p.etag; ST.dirty = false; ST.status = "";
  ST.history = []; ST.future = [];   // fresh document → fresh undo history
  // keep the current selection if the layer still exists (e.g. after queueing a revision)
  ST.selected = ST.tl.layers.some((L) => L.id === prevSelected) ? prevSelected : null;
  try { ST.inbox = await getJSON(`/api/project/${encodeURIComponent(PID)}/agent-inbox`); }
  catch (_) { ST.inbox = null; }
  paint();
}

// Global undo/redo shortcuts — ignored while typing in a field.
document.addEventListener("keydown", (e) => {
  const tag = (e.target && e.target.tagName) || "";
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;
  if (!(e.metaKey || e.ctrlKey)) return;
  const k = e.key.toLowerCase();
  if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
  else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); redo(); }
});

load().catch(console.error);
