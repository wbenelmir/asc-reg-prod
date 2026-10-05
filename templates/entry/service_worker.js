/* ASC 2026 Entry & Security service worker (Phase 4 Prompt 2, ADR-0023).
 *
 * Served from /entry/sw.js, so its scope can never exceed /entry/ (TRD
 * §7.3). It keeps exactly two kinds of response:
 *   - the static offline shell page (no participant data, no identifier);
 *   - the static assets that page needs.
 * Every other request -- every checkpoint page, every result, the device
 * API -- goes to the network and is NEVER cached. This worker never opens
 * IndexedDB: an update, an activation or a cache clean-up can never touch
 * the device's local records.
 *
 * Updates never interrupt an active operation: a new version waits until
 * every entry page is closed or reloaded (no automatic skipWaiting). */
"use strict";

const CACHE = "{{ cache_name }}";
const PREFIX = "{{ cache_prefix }}";
const PRECACHE = {{ precache|safe }};
const SHELL = {{ shell_url|safe }};

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll(
        PRECACHE.map((url) => new Request(url, { credentials: "same-origin", cache: "reload" }))
      )
    )
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((name) => name.startsWith(PREFIX) && name !== CACHE).map((name) => caches.delete(name))
        )
      )
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === SHELL && url.search === "") {
    // Network first; the cached shell is the fallback when there is no network.
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok && response.type === "basic" && !response.redirected) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(SHELL, copy));
          }
          return response;
        })
        .catch(() => caches.match(SHELL).then((cached) => cached || Response.error()))
    );
    return;
  }
  if (PRECACHE.indexOf(url.pathname) !== -1 && url.search === "") {
    event.respondWith(caches.match(url.pathname).then((cached) => cached || fetch(request)));
  }
  // Anything else: no respondWith -- straight to the network, never cached.
});
