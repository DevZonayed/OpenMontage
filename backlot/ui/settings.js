import { el, getJSON, postJSON } from "/ui/lib.js";

let PAYLOAD = null;
let CSRF = null;  // process-scoped token fetched on load; sent on every mutation

const AUTH_LABEL = {
  oauth_subscription: "OAuth · subscription",
  api_key: "API key",
  unknown: "signed in · unverified",
  none: "not signed in",
  not_installed: "not installed",
};

// Mutation POST with the CSRF token header.
async function mpost(url, body) {
  if (!CSRF) CSRF = (await getJSON("/api/csrf")).csrf;
  return postJSON(url, body, { "X-OpenMontage-CSRF": CSRF });
}

// ---- small control helpers (accessible: <label for> + <select id>) ----
let _uid = 0;
function field(labelText, control) {
  const id = `f${_uid++}`;
  control.setAttribute("id", id);
  return el("div", { class: "set-field" },
    el("label", { class: "set-label", for: id }, labelText), control);
}
function select(options, selected, attrs = {}) {
  const s = el("select", { class: "set-select", ...attrs });
  for (const o of options) {
    const opt = el("option", { value: o.value }, o.label);
    if (o.value === (selected ?? "")) opt.setAttribute("selected", "true");
    s.append(opt);
  }
  return s;
}
function textInput(value, placeholder, attrs = {}) {
  return el("input", { class: "set-input", type: "text", value: value || "", placeholder, ...attrs });
}
function engineOptions(includeAuto = true) {
  const opts = includeAuto ? [{ value: "", label: "(auto)" }] : [];
  for (const e of PAYLOAD.engines) {
    opts.push({ value: e.id, label: `${e.name}${e.logged_in ? "" : " — offline"}` });
  }
  return opts;
}

// ============================ Engines + OAuth actions ============================
async function doAction(engine, action, extra = {}) {
  return mpost("/api/providers/action", { engine, action, ...extra });
}
async function doCredential(action, extra = {}) {
  return mpost("/api/providers/credential", { engine: "zai", action, ...extra });
}

function engineCard(e) {
  const badgeCls = e.subscription_backed ? "set-badge ok"
    : e.logged_in ? "set-badge warn" : "set-badge off";
  const badgeText = e.subscription_backed
    ? `SUBSCRIPTION${e.subscription_type ? " · " + e.subscription_type.toUpperCase() : ""}`
    : e.logged_in ? "SIGNED IN" : e.installed ? "INSTALLED" : "ABSENT";

  const rows = [
    el("div", { class: "set-kv" }, el("span", { class: "set-k" }, "Auth"),
      el("span", { class: "set-v" }, AUTH_LABEL[e.auth_method] || e.auth_method)),
    el("div", { class: "set-kv" }, el("span", { class: "set-k" }, "Image gen"),
      el("span", { class: "set-v", title: e.image_blocker || "" },
        e.image_capable ? "supported" : "not via this engine")),
  ];
  if (e.api_key_alternative) {
    rows.push(el("div", { class: "set-kv" }, el("span", { class: "set-k" }, "API fallback"),
      el("span", { class: "set-v" }, e.api_key_alternative)));
  }

  const notes = (e.blockers || []).map((b) => el("p", { class: "set-note warn" }, "⚠ " + b))
    .concat((e.notes || []).map((n) => el("p", { class: "set-note" }, n)));

  // OAuth action buttons — gated by the engine's supported-action matrix.
  const actions = e.actions || {};
  const msg = el("p", { class: "set-note" });
  const bar = el("div", { class: "set-actionbar" });

  if (actions.status) {
    bar.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
      msg.textContent = "Checking…";
      try {
        const r = await doAction(e.id, "status");
        msg.textContent = r.state
          ? `status: ${r.state.logged_in ? "signed in" : "not signed in"}${r.state.subscription_backed ? " · subscription" : ""}`
          : (r.message || "ok");
      } catch (err) { msg.textContent = "Error: " + err.message; }
    } }, "Refresh status"));
  }
  if (actions.connect) {
    // D: this is NOT a one-click connect — sign-in is interactive/manual, so the
    // button reveals instructions + a copyable command (never claims success).
    bar.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
      msg.innerHTML = "";
      try {
        const r = await doAction(e.id, "connect");
        const line = el("span", {}, r.message || "");
        msg.append(line);
        if (r.command) {
          msg.append(el("code", { class: "set-cmd" }, r.command));
          msg.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
            try { await navigator.clipboard.writeText(r.command); } catch { /* clipboard blocked */ }
          } }, "Copy"));
        }
      } catch (err) { msg.textContent = "Error: " + err.message; }
    } }, "Sign-in instructions"));
  }
  if (actions.logout === "auto") {
    bar.append(el("button", { class: "set-mini danger", type: "button", onclick: async () => {
      if (!window.confirm(`Log out of ${e.name}? You'll need to sign in again to use it.`)) return;
      msg.textContent = "Logging out…";
      try {
        const r = await doAction(e.id, "logout", { confirm: true });
        msg.textContent = r.message || (r.ok ? "Logged out." : "Logout failed.");
        if (r.ok) load();  // refresh whole page state
      } catch (err) { msg.textContent = "Error: " + err.message; }
    } }, "Log out"));
  } else if (actions.logout === "unsupported") {
    bar.append(el("span", { class: "set-note" }, "logout n/a"));
  }

  return el("div", { class: "set-card" },
    el("div", { class: "set-card-head" }, el("h3", {}, e.name), el("span", { class: badgeCls }, badgeText)),
    ...rows, ...notes, bar, msg);
}

// ============================ Z.AI secure credential panel ============================
const ZAI_STATUS = {
  not_configured: { cls: "off", text: "NOT CONFIGURED" },
  stored_unverified: { cls: "warn", text: "STORED · UNVERIFIED" },
  verified: { cls: "ok", text: "VERIFIED" },
  invalid: { cls: "warn", text: "KEY REJECTED" },
};

function zaiCard(e) {
  const cred = e.credential || (PAYLOAD.zai_credential || {});
  const s = ZAI_STATUS[cred.status] || ZAI_STATUS.not_configured;
  const head = el("div", { class: "set-card-head" },
    el("h3", {}, "Z.AI (GLM)"),
    el("span", { class: `set-badge ${s.cls}` }, s.text));
  const msg = el("p", { class: "set-note", role: "status", "aria-live": "polite" });

  const body = el("div", {});
  if (!cred.keychain_available) {
    body.append(el("p", { class: "set-note warn" },
      "⚠ No secure system keychain is available, so a key cannot be stored safely. " +
      "Configure a keyring backend to enable Z.AI."));
    return el("div", { class: "set-card" }, head, body);
  }

  body.append(el("p", { class: "set-note" },
    "Your API key is stored only in the system Keychain — never written to project files, .env, or logs."));

  if (!cred.configured) {
    // ---- entry form ----
    const plan = select([
      { value: "coding", label: "GLM Coding Plan" },
      { value: "general", label: "General API" },
    ], "coding", { "aria-label": "Z.AI plan type" });
    const keyInput = el("input", {
      class: "set-input", type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: "Paste API key", "aria-label": "Z.AI API key",
    });
    const showToggle = el("button", { class: "set-mini", type: "button", "aria-pressed": "false",
      onclick: () => {
        const show = keyInput.getAttribute("type") === "password";
        keyInput.setAttribute("type", show ? "text" : "password");
        showToggle.setAttribute("aria-pressed", String(show));
        showToggle.textContent = show ? "Hide" : "Show";
      } }, "Show");
    const saveBtn = el("button", { class: "set-btn small", type: "button" }, "Save & verify");
    const submit = async () => {
      const key = keyInput.value;
      if (!key) { msg.style.color = "var(--red)"; msg.textContent = "Enter a key first."; return; }
      saveBtn.disabled = true; msg.style.color = ""; msg.textContent = "Saving to Keychain & verifying…";
      keyInput.value = ""; keyInput.setAttribute("type", "password");  // clear DOM immediately
      try {
        await doCredential("store", { api_key: key, plan_type: plan.value, verify: true });
        await load();
      } catch (err) {
        msg.style.color = "var(--red)"; msg.textContent = "Rejected: " + err.message;
        saveBtn.disabled = false;
      }
    };
    saveBtn.addEventListener("click", submit);
    keyInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
    body.append(field("Plan", plan));
    const keyRow = el("div", { class: "set-keyrow" }, keyInput, showToggle);
    body.append(el("label", { class: "set-label", for: "" }, "API key"), keyRow);
    body.append(el("div", { class: "set-actionbar" }, saveBtn));
  } else {
    // ---- manage existing ----
    body.append(el("div", { class: "set-kv" },
      el("span", { class: "set-k" }, "Plan"), el("span", { class: "set-v" }, cred.plan_type || "—")));
    const bar = el("div", { class: "set-actionbar" });
    bar.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
      msg.style.color = ""; msg.textContent = "Testing (sends one minimal request)…";
      try { await doCredential("verify"); await load(); }
      catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Error: " + err.message; }
    } }, "Test connection"));
    bar.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
      msg.style.color = ""; msg.textContent = "Opening a scoped Claude Code session…";
      try { const r = await doCredential("launch"); msg.textContent = r.launched ? "Launched (scoped to Z.AI)." : "Could not launch."; }
      catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Error: " + err.message; }
    } }, "Launch Claude Code"));
    bar.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
      // replace = clear then re-enter
      await doCredential("remove").catch(() => {}); await load();
    } }, "Replace"));
    bar.append(el("button", { class: "set-mini danger", type: "button", onclick: async () => {
      if (!window.confirm("Remove the Z.AI key from your Keychain? Z.AI becomes unavailable.")) return;
      try { await doCredential("remove"); await load(); }
      catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Error: " + err.message; }
    } }, "Remove"));
    body.append(bar);
  }
  return el("div", { class: "set-card" }, head, body, msg);
}

// ============================ Antigravity (Google) card ============================
function antigravityCard(e) {
  const signedIn = !!e.subscription_backed;
  const badgeCls = signedIn ? "set-badge ok" : e.installed ? "set-badge warn" : "set-badge off";
  const badgeText = signedIn ? "SIGNED IN · OAUTH" : e.installed ? "INSTALLED · SIGN IN" : "NOT INSTALLED";
  const head = el("div", { class: "set-card-head" },
    el("h3", {}, e.name), el("span", { class: badgeCls }, badgeText));
  const notes = (e.notes || []).map((n) => el("p", { class: "set-note" }, n));
  const msg = el("p", { class: "set-note", role: "status", "aria-live": "polite" });
  const bar = el("div", { class: "set-actionbar" });

  if (!e.installed) {
    bar.append(el("button", { class: "set-btn small", type: "button", onclick: async (ev) => {
      if (!window.confirm("Install the official Google Antigravity CLI (agy) to ~/.local/bin? " +
        "Downloads from Google and verifies its checksum.")) return;
      ev.target.disabled = true; msg.style.color = ""; msg.textContent = "Downloading & verifying (official installer)…";
      try { const r = await doAction("antigravity", "install"); msg.textContent = r.message || "Installed."; if (r.installed) load(); else ev.target.disabled = false; }
      catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Install failed: " + err.message; ev.target.disabled = false; }
    } }, "Install Google CLI"));
  } else {
    bar.append(el("button", { class: "set-mini", type: "button", onclick: async () => {
      msg.style.color = ""; msg.textContent = "Checking…";
      try { const r = await doAction("antigravity", "status"); msg.textContent = r.state.signed_in ? "Signed in." : "Not signed in."; }
      catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Error: " + err.message; }
    } }, "Refresh status"));
    if (!signedIn) {
      bar.append(el("button", { class: "set-btn small", type: "button", onclick: async () => {
        msg.style.color = ""; msg.textContent = "Opening a terminal for Google sign-in…";
        try { const r = await doAction("antigravity", "connect"); msg.textContent = r.message || "Waiting for browser sign-in — then Refresh status."; }
        catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Error: " + err.message; }
      } }, "Connect Google"));
    } else {
      bar.append(el("button", { class: "set-mini danger", type: "button", onclick: async () => {
        if (!window.confirm("Open a terminal to sign out of Antigravity? (You'll use the in-session sign-out.)")) return;
        try { const r = await doAction("antigravity", "logout", { confirm: true }); msg.textContent = r.message || ""; }
        catch (err) { msg.style.color = "var(--red)"; msg.textContent = "Error: " + err.message; }
      } }, "Sign out"));
    }
  }
  return el("div", { class: "set-card" }, head, ...notes, bar, msg);
}

function renderEngines() {
  const host = document.getElementById("engines");
  host.innerHTML = "";
  for (const e of PAYLOAD.engines) {
    if (e.id === "zai") host.append(zaiCard(e));
    else if (e.id === "antigravity") host.append(antigravityCard(e));
    else host.append(engineCard(e));
  }
  return;
}

// Fixed, allowlisted Remotion maintenance actions (verify/install/repair).
// Server values are set via textContent only (never innerHTML) — XSS-safe.
function remotionActionBar(opt) {
  const status = el("span", { class: "set-note", role: "status" });
  status.textContent = opt.available ? "Render-ready." : (opt.reason || "");
  const bar = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" });
  const mk = (label, action, busyText) => {
    const b = el("button", { class: "set-mini", type: "button" }, label);
    b.addEventListener("click", async () => {
      const btns = [...bar.querySelectorAll("button")];
      btns.forEach((x) => { x.disabled = true; });
      status.textContent = busyText;
      try {
        const r = await mpost("/api/providers/runtime", { runtime: "remotion", action });
        const doc = (r && r.doctor) || {};
        status.textContent = doc.available ? "Render-ready." : (r.message || doc.reason || "Done.");
        await load(); // refresh availability + re-render the whole section
      } catch (err) {
        status.textContent = (err && err.message) || "Action failed.";
        btns.forEach((x) => { x.disabled = false; });
      }
    });
    return b;
  };
  bar.append(mk("Verify", "verify", "Verifying…"));
  if (!opt.available) {
    bar.append(mk("Install", "install", "Installing dependencies…"));
    bar.append(mk("Repair", "repair", "Repairing (deps + browser)…"));
  }
  return el("div", { style: "display:flex;gap:12px;align-items:center;margin:6px 0 12px;flex-wrap:wrap" },
    bar, status);
}

// ============================ Preferred runtime ============================
function renderRuntimes() {
  const host = document.getElementById("runtimeOptions");
  host.innerHTML = "";
  const current = PAYLOAD.preferences.preferred_render_runtime;
  for (const opt of PAYLOAD.render_runtime_options) {
    const id = `rt-${opt.id}`;
    const input = el("input", { type: "radio", name: "preferred_render_runtime", id, value: opt.id });
    if (!opt.available) input.setAttribute("disabled", "true");
    if (current === opt.id) input.setAttribute("checked", "true");
    host.append(el("label", { class: `set-runtime${opt.available ? "" : " disabled"}`, for: id, title: opt.reason || "" },
      input,
      el("span", { class: "set-runtime-name" }, opt.id),
      el("span", { class: `set-badge ${opt.available ? "ok" : "off"}` }, opt.available ? "AVAILABLE" : "UNAVAILABLE"),
      opt.reason ? el("span", { class: "set-runtime-reason" }, opt.reason) : null));
    if (opt.id === "remotion") host.append(remotionActionBar(opt));
  }
  const noneId = "rt-none";
  const noneInput = el("input", { type: "radio", name: "preferred_render_runtime", id: noneId, value: "" });
  if (!current) noneInput.setAttribute("checked", "true");
  host.append(el("label", { class: "set-runtime", for: noneId },
    noneInput, el("span", { class: "set-runtime-name" }, "(no default)")));

  const warnHost = document.getElementById("runtimeWarnings");
  warnHost.innerHTML = "";
  for (const w of PAYLOAD.runtime_warnings || []) warnHost.append(el("p", { class: "set-note warn" }, "⚠ " + w));

  const sel = document.getElementById("authoringMode");
  sel.innerHTML = "";
  sel.append(el("option", { value: "" }, "(not set)"));
  for (const m of PAYLOAD.authoring_modes) {
    const o = el("option", { value: m }, m);
    if (PAYLOAD.preferences.authoring_mode === m) o.setAttribute("selected", "true");
    sel.append(o);
  }
}

// ============================ Purpose → engine ============================
function purposeRow(purpose) {
  const sel = (PAYLOAD.preferences.purposes && PAYLOAD.preferences.purposes[purpose]) || {};
  const eff = (PAYLOAD.effective_text_engines || {})[purpose] || {};
  const engineSel = select(engineOptions(true), sel.engine, { "data-purpose": purpose, "data-role": "engine" });
  const modelInput = textInput(sel.model, "model (optional)", { "data-purpose": purpose, "data-role": "model" });
  const fbList = `dl-eng`;
  const fbInput = textInput((sel.fallback || []).join(", "), "fallback engines (comma-sep)",
    { "data-purpose": purpose, "data-role": "fallback", list: fbList });
  const effLine = el("p", { class: "set-note" },
    `effective: ${eff.engine || "none available"}${eff.reason ? " — " + eff.reason : ""}`);
  return el("div", { class: "set-card" },
    el("div", { class: "set-card-head" }, el("h3", {}, purpose)),
    field("Engine", engineSel), field("Model", modelInput), field("Fallback order", fbInput), effLine);
}

function renderPurposes() {
  document.getElementById("subscriptionFirst").checked = !!PAYLOAD.preferences.subscription_first;
  const host = document.getElementById("purposes");
  host.innerHTML = "";
  // shared datalist of engine ids for fallback suggestions
  host.append(el("datalist", { id: "dl-eng" }, ...PAYLOAD.catalog.engines.map((id) => el("option", { value: id }))));
  for (const p of PAYLOAD.text_purposes) host.append(purposeRow(p));
}

// ============================ Media ============================
function mediaRow(kind) {
  const catKey = kind === "image" ? "image_providers" : "video_providers";
  const providers = PAYLOAD.catalog[catKey] || [];
  const capName = kind === "image" ? "image_generation" : "video_generation";
  const cap = (PAYLOAD.media_capabilities || []).find((c) => c.capability === capName) || {};
  const sel = PAYLOAD.preferences[kind] || {};
  const provOpts = [{ value: "", label: "(auto)" }, ...providers.map((p) => ({ value: p, label: p }))];
  const provSel = select(provOpts, sel.provider, { "data-kind": kind, "data-role": "provider" });
  const modelInput = textInput(sel.model, "model (optional)", { "data-kind": kind, "data-role": "model" });
  const listId = `dl-${kind}`;
  const fbInput = textInput((sel.fallback || []).join(", "), "fallback providers (comma-sep)",
    { "data-kind": kind, "data-role": "fallback", list: listId });
  const datalist = el("datalist", { id: listId }, ...providers.map((p) => el("option", { value: p })));
  const count = `${cap.configured ?? 0}/${cap.total ?? 0} configured`;
  return el("div", { class: "set-card" },
    el("div", { class: "set-card-head" }, el("h3", {}, kind), el("span", { class: "set-badge" }, count)),
    datalist,
    field("Provider", provSel), field("Model", modelInput), field("Fallback order", fbInput));
}

function renderMedia() {
  const note = document.getElementById("imageSubNote");
  note.textContent = PAYLOAD.image_via_subscription_supported
    ? "A subscription image path is available."
    : "No subscription/OAuth engine exposes image generation — image uses the media providers below (local, stock, or API).";
  const host = document.getElementById("media");
  host.innerHTML = "";
  host.append(mediaRow("image"));
  host.append(mediaRow("video"));
}

// ============================ Collect + save ============================
function parseList(str) {
  return (str || "").split(",").map((s) => s.trim()).filter(Boolean);
}
function collect() {
  const purposes = {};
  for (const p of PAYLOAD.text_purposes) {
    purposes[p] = {
      engine: document.querySelector(`select[data-purpose="${p}"][data-role="engine"]`).value || null,
      model: document.querySelector(`input[data-purpose="${p}"][data-role="model"]`).value.trim() || null,
      fallback: parseList(document.querySelector(`input[data-purpose="${p}"][data-role="fallback"]`).value),
    };
  }
  const media = {};
  for (const kind of ["image", "video"]) {
    media[kind] = {
      provider: document.querySelector(`select[data-kind="${kind}"][data-role="provider"]`).value || null,
      model: document.querySelector(`input[data-kind="${kind}"][data-role="model"]`).value.trim() || null,
      fallback: parseList(document.querySelector(`input[data-kind="${kind}"][data-role="fallback"]`).value),
    };
  }
  const rt = document.querySelector('input[name="preferred_render_runtime"]:checked');
  return {
    subscription_first: document.getElementById("subscriptionFirst").checked,
    purposes, image: media.image, video: media.video,
    preferred_render_runtime: rt && rt.value ? rt.value : null,
    authoring_mode: document.getElementById("authoringMode").value || null,
  };
}

function renderAll() {
  renderEngines();
  renderRuntimes();
  renderPurposes();
  renderMedia();
  document.getElementById("saveState").textContent =
    PAYLOAD.any_subscription_ready ? `${PAYLOAD.subscription_ready.length} SUBSCRIPTION READY` : "NO SUBSCRIPTION READY";
  document.getElementById("saveBtn").disabled = false;
}

async function load() {
  document.getElementById("loading").style.display = "block";
  document.getElementById("saveBtn").disabled = true;
  try {
    PAYLOAD = await getJSON("/api/providers");
    renderAll();
    document.getElementById("loadError").style.display = "none";
  } catch (err) {
    const e = document.getElementById("loadError");
    e.style.display = "block";
    e.textContent = "Failed to load providers: " + err.message;
  } finally {
    document.getElementById("loading").style.display = "none";
  }
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  const msg = document.getElementById("saveMsg");
  const btn = document.getElementById("saveBtn");
  btn.disabled = true;
  msg.style.color = "";
  msg.textContent = "Saving…";
  try {
    PAYLOAD = await mpost("/api/providers", collect());
    renderAll();
    msg.style.color = "var(--green)";
    msg.textContent = "Saved.";
  } catch (err) {
    msg.style.color = "var(--red)";
    msg.textContent = "Rejected: " + err.message;
  } finally {
    btn.disabled = false;
  }
});

load();
