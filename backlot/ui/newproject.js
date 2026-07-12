// Accessible "New project" modal — creates a workspace + saved intake via the
// CSRF-guarded API, then navigates to the project board. Production stays
// agent-driven (Rule Zero); this only initializes the workspace + brief.
import { el, getJSON, postJSON } from "/ui/lib.js";

let CSRF = null;
async function mpost(url, body) {
  if (!CSRF) CSRF = (await getJSON("/api/csrf")).csrf;
  return postJSON(url, body, { "X-OpenMontage-CSRF": CSRF });
}

function slugPreview(title) {
  return (title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
}

const DUR_MIN = 1, DUR_MAX = 300, DUR_DEFAULT = 60;
const DUR_PRESETS = [30, 60, 150, 300];
function fmtMMSS(s) { return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

// Accessible STORY DURATION control: presets + Custom min/sec, canonical mm:ss
// display, inline error. Returns { node, getSeconds } where getSeconds() throws
// a friendly Error for an invalid custom value (used to block Create).
function durationControl() {
  const err = el("p", { class: "np-err", role: "alert", style: "margin:4px 0 0" });
  const selectedOut = el("b", {}, fmtMMSS(DUR_DEFAULT));
  const minInput = el("input", { class: "set-input", type: "number", min: "0", max: "5",
    inputmode: "numeric", id: "np-dur-min", "aria-label": "Minutes", value: "1",
    style: "width:64px" });
  const secInput = el("input", { class: "set-input", type: "number", min: "0", max: "59",
    inputmode: "numeric", id: "np-dur-sec", "aria-label": "Seconds", value: "00",
    style: "width:64px" });
  const custom = el("div", { class: "np-dur-custom", style: "display:none;gap:8px;align-items:center;margin-top:8px" },
    el("label", { class: "set-note", for: "np-dur-min" }, "min"), minInput,
    el("span", {}, ":"),
    el("label", { class: "set-note", for: "np-dur-sec" }, "sec"), secInput);

  const chips = el("div", { class: "np-dur-presets", role: "radiogroup", "aria-label": "Story duration",
    style: "display:flex;gap:8px;flex-wrap:wrap" });
  const radios = [];
  const mkChip = (label, value) => {
    const input = el("input", { type: "radio", name: "np-dur", value: String(value),
      style: "position:absolute;opacity:0;width:0;height:0" });
    if (value === DUR_DEFAULT) input.setAttribute("checked", "true");
    input.addEventListener("change", onChange);
    radios.push(input);
    return el("label", { class: "np-chip", tabindex: "-1",
      style: "display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border:1px solid var(--set-line,#2a2f3a);border-radius:999px;cursor:pointer;font-size:13px" },
      input, el("span", {}, label));
  };
  chips.append(
    mkChip("0:30", 30), mkChip("1:00", 60), mkChip("2:30", 150), mkChip("5:00", 300),
    mkChip("Custom", "custom"));

  function current() {
    const r = radios.find((x) => x.checked);
    return r ? r.value : String(DUR_DEFAULT);
  }
  function highlight() {
    for (const r of radios) {
      const chip = r.closest(".np-chip");
      if (chip) chip.style.borderColor = r.checked ? "var(--accent,#e8c07d)" : "var(--set-line,#2a2f3a)";
    }
  }
  function onChange() {
    const cur = current();
    custom.style.display = cur === "custom" ? "flex" : "none";
    err.textContent = "";
    try { selectedOut.textContent = fmtMMSS(getSeconds()); } catch { selectedOut.textContent = "—"; }
    highlight();
  }
  minInput.addEventListener("input", onChange);
  secInput.addEventListener("input", onChange);

  function getSeconds() {
    const cur = current();
    if (cur !== "custom") return parseInt(cur, 10);
    const m = parseInt(minInput.value, 10);
    const s = parseInt(secInput.value, 10);
    if (!Number.isFinite(m) || !Number.isFinite(s) || m < 0 || s < 0 || s > 59) {
      throw new Error("Enter a valid custom length (minutes and seconds 0–59).");
    }
    const total = m * 60 + s;
    if (total < DUR_MIN || total > DUR_MAX) {
      throw new Error(`Duration must be between 0:01 and ${fmtMMSS(DUR_MAX)}.`);
    }
    return total;
  }

  highlight();
  const node = el("fieldset", { class: "np-dur", style: "border:none;padding:0;margin:0" },
    el("legend", { class: "set-label", style: "padding:0" }, "Story duration"),
    chips, custom,
    el("p", { class: "set-note", style: "margin:8px 0 0" },
      "Target length: ", selectedOut,
      " — sets the planned narration, scene count, timeline length and render."),
    err);
  return { node, getSeconds };
}

export async function openNewProjectModal() {
  const opener = document.activeElement;
  let pipelines = [];
  try { pipelines = await getJSON("/api/pipelines"); } catch { pipelines = []; }

  const errLine = el("p", { class: "np-err", role: "alert" });
  const idPreview = el("code", { class: "np-id" }, "—");

  const titleInput = el("input", {
    class: "set-input", type: "text", id: "np-title-input", maxlength: "120",
    placeholder: "e.g. The Sundarbans at Dawn", "aria-describedby": "np-id-row",
  });
  const briefInput = el("textarea", {
    class: "set-input", id: "np-brief-input", rows: "4", maxlength: "4000",
    placeholder: "What's the video about? Topic, angle, audience…",
  });
  const pipeSelect = el("select", { class: "set-select", id: "np-pipe-input" });
  for (const p of pipelines) {
    const label = `${p.id}${p.beta ? " (beta)" : ""}`;
    pipeSelect.append(el("option", { value: p.id }, label));
  }
  const pipeDesc = el("p", { class: "set-note" });
  const syncPipeDesc = () => {
    const p = pipelines.find((x) => x.id === pipeSelect.value);
    pipeDesc.textContent = p ? (p.description || "") + (p.beta ? "  ⚠ Beta — expect rough edges." : "") : "";
  };
  pipeSelect.addEventListener("change", syncPipeDesc);
  if (pipelines.length) { pipeSelect.value = pipelines.find((p) => p.id === "animation") ? "animation" : pipelines[0].id; }
  syncPipeDesc();

  titleInput.addEventListener("input", () => { idPreview.textContent = slugPreview(titleInput.value) || "—"; });

  const createBtn = el("button", { class: "set-btn", type: "submit" }, "Create project");
  const cancelBtn = el("button", { class: "set-mini", type: "button" }, "Cancel");
  const duration = durationControl();

  const form = el("form", { class: "np-form" },
    el("label", { class: "set-label", for: "np-title-input" }, "Project title"),
    titleInput,
    el("div", { class: "np-id-row", id: "np-id-row" }, el("span", {}, "Workspace id: "), idPreview),
    el("label", { class: "set-label", for: "np-brief-input" }, "Production brief / topic"),
    briefInput,
    duration.node,
    el("label", { class: "set-label", for: "np-pipe-input" }, "Pipeline"),
    pipeSelect, pipeDesc,
    errLine,
    el("div", { class: "np-actions" }, cancelBtn, createBtn),
  );

  const dialog = el("div", { class: "np-dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "np-title" },
    el("h2", { class: "set-h2", id: "np-title" }, "New project"),
    el("p", { class: "set-note" }, "Creates the workspace + saves your brief. Production runs through the agent pipeline."),
    form);
  const overlay = el("div", { class: "np-overlay" }, dialog);

  function close() {
    overlay.remove();
    document.removeEventListener("keydown", onKey, true);
    if (opener && opener.focus) opener.focus();
  }
  function focusables() {
    return [...dialog.querySelectorAll("input,textarea,select,button")].filter((n) => !n.disabled);
  }
  function onKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); close(); return; }
    if (ev.key === "Tab") {
      const f = focusables();
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    }
  }
  cancelBtn.addEventListener("click", close);
  overlay.addEventListener("mousedown", (ev) => { if (ev.target === overlay) close(); });
  document.addEventListener("keydown", onKey, true);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    errLine.textContent = "";
    const title = titleInput.value.trim();
    if (!title) { errLine.textContent = "Enter a project title."; titleInput.focus(); return; }
    let targetSeconds;
    try { targetSeconds = duration.getSeconds(); }
    catch (e) { errLine.textContent = e.message; return; }  // block Create on invalid duration
    createBtn.disabled = true; cancelBtn.disabled = true; createBtn.textContent = "Creating…";
    try {
      const r = await mpost("/api/projects", {
        title, brief: briefInput.value, pipeline: pipeSelect.value,
        target_duration_seconds: targetSeconds,
      });
      window.location.href = `/p/${encodeURIComponent(r.project_id)}`;
    } catch (err) {
      errLine.textContent = err.message || "Could not create the project.";
      createBtn.disabled = false; cancelBtn.disabled = false; createBtn.textContent = "Create project";
    }
  });

  document.body.append(overlay);
  titleInput.focus();
}

// Wire any element with [data-new-project] to open the modal.
export function wireNewProjectButtons() {
  for (const btn of document.querySelectorAll("[data-new-project]")) {
    if (btn.__npWired) continue;
    btn.__npWired = true;
    btn.addEventListener("click", (ev) => { ev.preventDefault(); openNewProjectModal().catch(console.error); });
  }
}
