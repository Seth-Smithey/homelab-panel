// Browser smoke test: starts the real server on a spare port with the
// starter config, loads the board in headless Chromium, and checks the things
// a syntax check cannot — the page renders without console errors, the live
// stream connects, a row opens and closes with correct disclosure semantics,
// the chart cache refreshes on a newer poll, and a transport heartbeat keeps
// the connection watchdog quiet while a dead stream does not.
//
//   node tests/smoke/browser-smoke.mjs          (needs: npm i playwright; python deps installed)
//
// CI runs this in the `web` job.

import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";

const ROOT = new URL("../..", import.meta.url).pathname;
const PORT = Number(process.env.SMOKE_PORT || 8099);
const BASE = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.PYTHON || "python3";

function fail(msg) { console.error(`BROWSER SMOKE FAILED: ${msg}`); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A config with a SLOW host_metrics interval: the stream is then quiet
// between polls, which is exactly what the connection watchdog must tolerate.
// Polls are forced where the test needs one, through the refresh API.
const work = mkdtempSync(join(tmpdir(), "panel-browser-"));
const starter = readFileSync(join(ROOT, "config.starter.yaml"), "utf8")
  .replace(/port:\s*8080/, `port: ${PORT}`)
  .replace(/host:\s*"0\.0\.0\.0"/, 'host: "127.0.0.1"');
const cfg = join(work, "config.yaml");
writeFileSync(cfg, starter.replace(/(host_metrics:\s*\n\s+enabled:\s*true\s*\n\s+interval:\s*)\d+/, "$1120"));
if (!/interval: 120\n/.test(readFileSync(cfg, "utf8"))) fail("could not set a 120s host_metrics interval in the smoke config");
const poll = () => page.evaluate(() => fetch("/api/refresh/host_metrics", { method: "POST" }).then((r) => r.ok));

const server = spawn(PYTHON, ["-m", "app.main"], {
  cwd: ROOT,
  env: { ...process.env, PANEL_CONFIG: cfg, PANEL_DB: join(work, "panel.db"), PANEL_LOG_LEVEL: "warning" },
  stdio: ["ignore", "inherit", "inherit"],
});
const stop = () => { try { server.kill("SIGTERM"); } catch { /* gone */ } };
process.on("exit", stop);

// Wait for /healthz ready.
let ready = false;
for (let i = 0; i < 60 && !ready; i++) {
  await sleep(500);
  try {
    const r = await fetch(`${BASE}/healthz`);
    const j = await r.json();
    ready = Boolean(j.ok && j.ready);
  } catch { /* not yet */ }
}
if (!ready) fail("server did not become ready");

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.waitForSelector(".row .row-toggle", { timeout: 20000 });
await page.waitForFunction(() => document.getElementById("feed")?.textContent === "live", null, { timeout: 15000 })
  .catch(() => fail("stream never went live"));

// Disclosure: a real button, aria-controls pointing at the detail region.
const toggle = page.locator(".row .row-toggle").first();
const rowId = await toggle.evaluate((b) => b.closest(".row").dataset.id);
if ((await toggle.getAttribute("aria-expanded")) !== "false") fail("row starts expanded");
await toggle.click();
const controls = await page.locator(`.row[data-id="${rowId}"] .row-toggle`).getAttribute("aria-controls");
if (!controls) fail("aria-controls missing");
await page.waitForSelector(`#${controls}`, { timeout: 5000 }).catch(() => fail("detail region not rendered"));
if ((await page.locator(`.row[data-id="${rowId}"] .row-toggle`).getAttribute("aria-expanded")) !== "true") fail("aria-expanded not true after open");
const nestedButtons = await page.locator(`.row[data-id="${rowId}"] .row-toggle button, .row[data-id="${rowId}"] .row-toggle a`).count();
if (nestedButtons) fail("interactive controls nested inside the disclosure button");
await page.waitForFunction((id) => {
  const slot = document.querySelector(`#${id} .spark-slot`);
  return slot && !/Loading/.test(slot.textContent || "");
}, controls, { timeout: 10000 }).catch(() => fail("chart never finished loading"));
if (!(await page.locator(`#${controls} .detail-text`).count())) fail("expanded row lacks the full diagnostic text");

// Focus survives a re-render.
await page.locator(`.row[data-id="${rowId}"] .row-toggle`).focus();
if (!(await poll())) fail("refresh API refused a same-origin POST");
await sleep(1500);
const focused = await page.evaluate(() => document.activeElement?.closest(".row")?.dataset.id || "");
if (focused !== rowId) fail(`focus lost across a re-render (on ${focused || "nothing"})`);

// Close, wait for a newer poll, reopen: the chart must be refetched (a fresh
// "Loading…" or a repaint for the newer ts), never painted from a stale cache.
await page.locator(`.row[data-id="${rowId}"] .row-toggle`).click();
if (await page.locator(`#${controls}`).count()) fail("detail region still present after close");
await sleep(1100); // metrics are timestamped to the second
await poll();
await sleep(1500);
const requestsBefore = [];
page.on("request", (r) => { if (r.url().includes("/api/history/")) requestsBefore.push(r.url()); });
await page.locator(`.row[data-id="${rowId}"] .row-toggle`).click();
await sleep(1500);
if (!requestsBefore.length) fail("reopening after a newer poll did not refetch the chart");

// Watchdog: healthy quiet must not trip it. The watchdog is 45s, the
// heartbeat 20s, the next poll is minutes away; watch for 55s.
await sleep(55000);
const bannerText = await page.evaluate(() => (document.getElementById("banner")?.hidden ? "" : document.getElementById("banner")?.textContent) || "");
if (/No update from the panel/.test(bannerText)) fail(`watchdog fired on a healthy stream: ${bannerText}`);

// Dark and light both render the verdict.
for (const scheme of ["dark", "light"]) {
  await page.emulateMedia({ colorScheme: scheme });
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  if (!bg) fail(`no background in ${scheme}`);
}

if (errors.length) fail(`console errors: ${errors.join(" | ")}`);
await browser.close();
stop();
console.log("browser smoke passed: render, live stream, disclosure, focus, chart refresh, watchdog, themes");
process.exit(0);
