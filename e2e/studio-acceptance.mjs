// Real-browser acceptance for the production command center (board + Studio).
//
// Drives the ACTUAL served pages (including /ui/studio.bundle.js) in headless
// Chromium at 1440x900 AND 1920x1080 against the exact rejected fixture, asserting
// the visible full-document text has NO legacy leakage, exactly one primary action,
// no enabled "Start production", the truthful 2:30 target, render disabled, and
// board==Studio parity — with zero console/page errors. Exits non-zero on any
// failure. This is the committed guard for the "tests passed / browser failed" path.
//
// Run:  python e2e/setup_fixture.py
//       OPENMONTAGE_PROJECTS_DIR=/tmp/openmontage-acceptance-projects \
//         .venv/bin/python -m backlot serve --port 8894 &   (or your runner)
//       BASE=http://127.0.0.1:8894 node e2e/studio-acceptance.mjs
//
// Requires playwright-core + a cached Chromium (npm i -D playwright-core). If it is
// not installed the script SKIPS (exit 0) with a clear message rather than failing.

let chromium;
try {
  ({ chromium } = await import("playwright-core"));
} catch {
  console.log("SKIP: playwright-core not installed (npm i -D playwright-core to run this E2E).");
  process.exit(0);
}

const BASE = process.env.BASE || "http://127.0.0.1:8894";
const PID = process.env.PID || "the-electricity-bulb";
const SIZES = [{ w: 1440, h: 900 }, { w: 1920, h: 1080 }];
const FORBIDDEN = ["fake_driver", "NO LIVE RUN", "NOT STARTED", "brain: —", "DETERMINISTIC FIXTURE", "OFFLINE DRIVER"];

const failures = [];
const ok = (cond, msg) => { if (!cond) failures.push(msg); };
const grab = async (p, sel) => { try { return (await p.textContent(sel))?.trim() || null; } catch { return null; } };

const browser = await chromium.launch({ headless: true });
try {
  for (const s of SIZES) {
    const tag = `${s.w}x${s.h}`;
    const ctx = await browser.newContext({ viewport: { width: s.w, height: s.h } });
    const errors = [];

    // ---- BOARD ----
    const board = await ctx.newPage();
    board.on("console", (m) => m.type() === "error" && errors.push("board:" + m.text()));
    board.on("pageerror", (e) => errors.push("board:" + String(e)));
    await board.goto(`${BASE}/p/${PID}`, { waitUntil: "domcontentloaded" });
    await board.waitForSelector(".cmd-center .cmd-primary", { timeout: 15000 });
    await board.waitForTimeout(700);
    const boardText = await board.evaluate(() => document.body.innerText);
    const boardHeadline = await grab(board, ".cmd-center .cmd-headline");
    const boardConnect = await board.$$eval("button, .cmd-btn",
      (els) => els.filter((e) => /connect hermes/i.test(e.textContent || "") && !e.disabled).length);

    // ---- STUDIO ----
    const st = await ctx.newPage();
    st.on("console", (m) => m.type() === "error" && errors.push("studio:" + m.text()));
    st.on("pageerror", (e) => errors.push("studio:" + String(e)));
    await st.goto(`${BASE}/p/${PID}/editor`, { waitUntil: "domcontentloaded" });
    await st.waitForSelector('[data-testid="cc-headline"]', { timeout: 20000 });
    await st.waitForSelector('[data-testid="production-inspector"]', { timeout: 20000 });
    await st.waitForTimeout(1000);
    const studioText = await st.evaluate(() => document.body.innerText);
    const studioHeadline = await grab(st, '[data-testid="cc-headline"]');
    const piHeadline = await grab(st, '[data-testid="pi-headline"]');
    const primaries = await st.$$eval('[data-testid="cc-primary"]', (n) => n.filter((e) => !e.disabled).length);
    const renderEnabled = await st.$$eval("button", (els) => {
      const b = els.find((e) => /render final film/i.test(e.textContent || ""));
      return b ? !b.disabled : false;
    });
    const tlMeta = await grab(st, '[data-testid="tl-meta"]');
    // Enumerate EVERY enabled button; classify production-next actions.
    const enabledBtns = await st.$$eval("button", (els) =>
      els.filter((e) => !e.disabled).map((e) => (e.textContent || "").trim()));
    // Production-next = an ADVANCING primary action (anchored to the real labels so
    // the secondary "Preview approved plan locally" is NOT miscounted).
    const nextRe = /^(connect hermes|continue production|start production|review & approve|approve |resume |retry |go to the next step)/i;
    const nextActions = enabledBtns.filter((t) => nextRe.test(t));
    const gotoBtns = enabledBtns.filter((t) => /go to the next step/i.test(t));
    const connectBtns = enabledBtns.filter((t) => /connect hermes/i.test(t));

    // ---- assertions ----
    for (const bad of FORBIDDEN) {
      ok(!boardText.includes(bad), `[${tag}] board leaks "${bad}"`);
      ok(!studioText.includes(bad), `[${tag}] studio leaks "${bad}"`);
    }
    ok(boardConnect === 1, `[${tag}] board Connect buttons = ${boardConnect} (want 1)`);
    ok(primaries === 1, `[${tag}] studio enabled primaries = ${primaries} (want 1)`);
    // Exactly ONE production-next action across the whole studio page; the single
    // Connect is it; no duplicate Connect, no "go to next step" button.
    ok(nextActions.length === 1, `[${tag}] studio production-next actions = ${nextActions.length} (want 1): ${JSON.stringify(nextActions)}`);
    ok(gotoBtns.length === 0, `[${tag}] studio has a "go to next step" button (want 0)`);
    ok(connectBtns.length === 1, `[${tag}] studio Connect buttons = ${connectBtns.length} (want 1)`);
    ok(renderEnabled === false, `[${tag}] render final film enabled (want disabled)`);
    ok((tlMeta || "").includes("2:30"), `[${tag}] timeline meta lacks 2:30: ${tlMeta}`);
    // No internal composer frame count / 1:00 ANYWHERE in the served document
    // (header, empty card, OR the transport scrubber like "f0/1800").
    ok(!/\b1800\b/.test(studioText), `[${tag}] studio document contains 1800`);
    ok(!/\b1:00\b/.test(studioText), `[${tag}] studio document contains 1:00`);
    ok(/f0\/4500/.test(studioText) || !/f0\//.test(studioText), `[${tag}] scrubber denominator not 4500`);
    ok(boardHeadline === studioHeadline && studioHeadline === piHeadline, `[${tag}] headline parity mismatch: board=${boardHeadline} cc=${studioHeadline} pi=${piHeadline}`);
    ok(errors.length === 0, `[${tag}] console/page errors: ${JSON.stringify(errors.slice(0, 5))}`);

    console.log(`[${tag}] board="${boardHeadline}" primaries=${primaries} nextActions=${nextActions.length} goto=${gotoBtns.length} connect=${connectBtns.length} render=${renderEnabled} tlMeta="${tlMeta}" has1800=${/\b1800\b/.test(studioText)} errors=${errors.length}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}

if (failures.length) {
  console.error("\nE2E FAILURES:\n" + failures.map((f) => " ✗ " + f).join("\n"));
  process.exit(1);
}
console.log("\n✓ studio/board acceptance passed at 1440x900 and 1920x1080.");
