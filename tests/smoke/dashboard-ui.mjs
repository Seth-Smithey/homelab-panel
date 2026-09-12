// Populated UI regression: real frontend + deterministic local API fixtures.
// Run: node tests/smoke/dashboard-ui.mjs (uses the same Playwright install as CI).
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { chromium } from "playwright";

let tick = Math.floor(Date.now() / 1000);
const check = (id, severity = "ok", extra = {}) => ({ id, name: id, severity, value: "online", detail: "Reporting normally", group: "Guests", ts: tick, ...extra });
const panel = (key, checks, extra = {}) => ({ key, title: key, checks, severity: "ok", summary: `${checks.length} monitored checks`, ts: tick, age: 0, interval: 30, ...extra });
let panels = [
  panel("Compute", [...Array.from({ length: 18 }, (_, i) => check(`vm.${i}`)), check("disk", "warning", { value: "84%", metric: 84, percent: 84 })], { severity: "warning" }),
  panel("Network", [check("WAN"), check("AP", "critical", { value: "offline", detail: "uplink-".repeat(35) })], { severity: "critical" }),
  panel("Muted", [check("maintenance", "critical", { muted: true })]),
  panel("Stale", [check("old", "ok", { stale: true })], { severity: "unknown", stale: true, age: 600 }),
  panel("Unreachable", [], { severity: "unknown", error: "Controller did not answer" }),
  panel("Pending", [], { severity: "unknown", pending: true }),
  panel("Empty", [], { severity: "unknown" }),
];
function snapshot() {
  const counts = { ok: 0, critical: 0, warning: 0, unknown: 0 };
  for (const p of panels) for (const c of p.checks) if (!c.muted) counts[c.stale ? "unknown" : c.severity]++;
  return { panels, site: { name: "Test lab" }, panel_order: panels.map((p) => p.key), counts,
    overall: "critical", headline: "Your lab needs attention", muted: panels.flatMap((p) => p.checks).filter((c) => c.muted).length,
    generated: tick, uptime: 3600, stale: panels.filter((p) => p.stale).length, version: "test", events: [] };
}
const streams = new Set();
function publish() { tick++; for (const p of panels) p.ts = tick; for (const stream of streams) stream.write(`data: ${JSON.stringify(snapshot())}\n\n`); }
const files = new Map([['/', ['index.html', 'text/html']], ['/static/app.js', ['app.js', 'text/javascript']], ['/static/style.css', ['style.css', 'text/css']]]);
const server = createServer((req, res) => {
  const path = new URL(req.url, "http://localhost").pathname;
  if (files.has(path)) {
    const [file, type] = files.get(path);
    res.writeHead(200, { "Content-Type": type }); res.end(readFileSync(new URL(`../../web/${file}`, import.meta.url))); return;
  }
  if (path === "/api/stream") {
    res.writeHead(200, { "Content-Type": "text/event-stream" }); streams.add(res);
    res.write(`data: ${JSON.stringify(snapshot())}\n\n`); req.on("close", () => streams.delete(res)); return;
  }
  let response = {};
  if (path === "/api/status") response = snapshot();
  if (path === "/api/version") response = { version: "test" };
  if (path.startsWith("/api/history/")) response = { points: [{ ts: tick - 60, value: 70 }, { ts: tick, value: 84 }] };
  if (path.startsWith("/api/availability/")) response = { observed: true, ok_percent: 99, coverage_percent: 100, transitions: 1 };
  if (path.startsWith("/api/mutes/")) {
    const id = decodeURIComponent(path.split("/").at(-1));
    for (const p of panels) for (const c of p.checks) if (c.id === id) c.muted = req.method !== "DELETE";
    publish();
  }
  if (path.startsWith("/api/refresh/")) publish();
  res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(response));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch(process.env.BROWSER_CHANNEL ? { channel: process.env.BROWSER_CHANNEL } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const errors = []; page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(base);
  await page.locator('[data-key="Compute"] .row').first().waitFor();
  assert.equal(await page.locator('[data-key="Compute"] .row').count(), 3, "summary bounded with 19 checks");
  assert.equal(await page.locator('.panel').first().getAttribute('data-key'), 'Compute', 'default fixed order');
  assert.equal(await page.locator('.attention-item').first().getAttribute('data-check-target'), 'AP', 'critical check first');
  assert.equal(await page.locator('[data-check-target="maintenance"]').count(), 0, 'muted critical excluded');
  for (const key of ['Stale', 'Unreachable', 'Pending', 'Empty']) assert.equal(await page.locator(`[data-attention-key="panel:${key}"]`).count(), 1, `${key} visible`);
  assert.equal(await page.locator('#coverage-map').isVisible(), false);

  await page.locator('[data-expand="Compute"]').click();
  assert.equal(await page.locator('[data-key="Compute"] .row').count(), 19);
  publish();
  await page.waitForFunction(() => document.querySelector('[data-expand="Compute"]').getAttribute('aria-expanded') === 'true');
  assert.equal(await page.locator('[data-key="Compute"] .row').count(), 19, 'expansion persists');
  await page.locator('[data-expand="Compute"]').click();
  await page.locator('#filter').fill('WAN');
  await page.waitForFunction(() => !document.querySelector('[data-key="Compute"]'));
  await page.locator('[data-check-target="disk"]').click();
  await page.waitForFunction(() => document.activeElement?.closest('.row')?.dataset.id === 'disk');
  assert.equal(await page.locator('#filter').inputValue(), '', 'attention navigation clears hiding filter');
  assert.equal(await page.locator('[data-id="disk"] .row-toggle').getAttribute('aria-expanded'), 'true');
  await page.locator('[data-id="disk"] .spark').waitFor();
  publish();
  await page.waitForFunction(() => document.querySelector('#foot-status').textContent.includes('collectors'));
  assert.equal(await page.evaluate(() => document.activeElement?.closest('.row')?.dataset.id), 'disk');
  await page.locator('[data-mute="disk"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-check-target="disk"]'));
  await page.locator('[data-mute="disk"]').click();
  await page.locator('[data-check-target="disk"]').waitFor();
  await page.locator('[data-expand="Compute"]').click();

  await page.locator('.display-menu summary').click();
  await page.locator('#coverage').click();
  await page.locator('.cell[data-target="vm.17"]').click();
  assert.equal(await page.locator('[data-id="vm.17"] .row-toggle').getAttribute('aria-expanded'), 'true', 'map reveals hidden summary row');
  await page.locator('#coverage').click();
  await page.locator('.display-menu summary').click();
  await page.locator('#problems').click();
  assert.equal(await page.locator('[data-key="Stale"]').count(), 1);
  assert.equal(await page.locator('[data-key="Empty"]').count(), 1);
  await page.locator('#problems').click();
  await page.locator('[data-expand="Compute"]').click();

  for (const colorScheme of ['dark', 'light']) {
    await page.emulateMedia({ colorScheme });
    await page.waitForFunction((theme) => document.documentElement.dataset.theme === theme, colorScheme);
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 950 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `${colorScheme} ${width}px horizontal overflow`);
      assert.equal(await page.locator('[data-check-target="AP"]').isVisible(), true);
      await page.locator('.display-menu summary').click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `${colorScheme} ${width}px display menu overflow`);
      await page.locator('.display-menu summary').click();
    }
  }
  // Long untrusted labels remain text, and a healthy reset removes old problems.
  panels = [panel('Healthy', [check('<img src=x onerror=alert(1)>')])]; publish();
  await page.waitForFunction(() => document.querySelectorAll('.attention-item').length === 0);
  assert.equal(await page.locator('.row-name img').count(), 0);
  assert.match(await page.locator('.row-name').innerText(), /<img/);
  assert.match(await page.locator('.attention-empty').innerText(), /No active problems/);
  assert.deepEqual(errors, []);
  console.log('Dashboard UI passed: populated summaries, attention, stale/unknown/muted states, navigation, expansion, focus, history, mute actions, themes, 320–1440px layout, escaping.');
} finally {
  await browser?.close();
  for (const stream of streams) stream.end();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
