/* Akana PWA service worker — installability + offline shell only.
 *
 * Deliberately minimal so it never fights the app's cache-busting:
 *  - /static assets are content-hashed (?v=) and immutable → we pass them
 *    straight through to the network/HTTP cache, never SW-cache them.
 *  - index.html ("/") is served no-store → we keep an opportunistic copy used
 *    ONLY as an offline fallback when the network is unreachable.
 *  - API / WebSocket / non-GET requests are never touched.
 */
// v2: only the root "/" is cached as the SHELL (the old v1 wrote every
// navigation under "/" → after visiting /memory, on an offline/flaky connection
// "/" would return /memory instead, leaving the user stuck in Memory). The
// version bump deletes the old (poisoned) cache on activate.
// v3: stale-shell complaints after deploy → we bump the version and definitively
// clear the old shell cache; the page side (index.html) auto-refreshes ONCE when
// the new SW takes over, so a manual "Clear Site Data" is not needed.
const CACHE = "akana-shell-v3";
const SHELL = "/";

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((c) => c.add(SHELL).catch(() => undefined)),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  // Only the document navigation gets network-first + offline fallback.
  if (request.mode === "navigate") {
    // ONLY the root "/" is cached as the SHELL. Pages like /memory are NEVER
    // written to the "/" key → the offline fallback is always the cockpit, so the
    // user never gets stuck on a sub-page (Memory).
    const isRoot = new URL(request.url).pathname === "/";
    event.respondWith(
      fetch(request)
        .then((resp) => {
          if (isRoot && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(SHELL, copy)).catch(() => undefined);
          }
          return resp;
        })
        .catch(() =>
          caches.match(SHELL).then(
            (cached) =>
              cached ||
              new Response(
                "<!doctype html><meta charset=utf-8><title>Akana</title>" +
                  "<body style='font-family:system-ui;background:#06080d;color:#cbd5e1;" +
                  "display:grid;place-items:center;height:100vh;margin:0'>" +
                  "<p>Can't reach the Akana server.<br>Are your PC and Tailscale running?</p>",
                { headers: { "Content-Type": "text/html; charset=utf-8" } },
              ),
          ),
        ),
    );
    return;
  }

  // Everything else (static assets, API GETs): straight to network.
});
