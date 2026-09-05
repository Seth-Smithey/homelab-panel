/* Minimal service worker.
 *
 * It exists so the browser will offer "Install" / "Add to Home Screen" — a
 * manifest alone is not enough in Chrome. It deliberately does NOT cache
 * anything: this is a status board, and a cached frame showing a green lab
 * that is actually on fire is worse than no board at all.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Network-only, with one exception: if the shell itself can't be fetched
// while offline, say so plainly rather than showing the browser's error page.
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () =>
        new Response(
          "<!doctype html><meta charset=utf-8><title>Panel offline</title>" +
            "<body style='font:14px system-ui;padding:2rem'>" +
            "<h1>Can't reach the panel</h1>" +
            "<p>The dashboard host is unreachable from this device. " +
            "Nothing here is cached on purpose — a stale board is worse than none.</p>",
          { headers: { "Content-Type": "text/html; charset=utf-8" } }
        )
    )
  );
});
