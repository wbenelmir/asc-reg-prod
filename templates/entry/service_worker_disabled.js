/* ASC 2026 Entry & Security: offline continuity is DISABLED (Phase 4 Prompt 2).
 *
 * A browser that installed the offline service worker earlier picks up this
 * self-removing version at its next update check. It deletes only this
 * application's shell caches and unregisters itself. It never opens
 * IndexedDB, so no local record of the device is touched. */
"use strict";

const PREFIX = "{{ cache_prefix }}";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name.startsWith(PREFIX)).map((name) => caches.delete(name))))
      .then(() => self.registration.unregister())
  );
});
