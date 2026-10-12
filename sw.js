/* Offline cache: every file of the app (incl. the 26 MB Python engine) is kept on
   the tablet after the first start, so the app works without internet. */
const CACHE = "gl121123-db56b6b6ef";
const FILES = [
 "./",
 ".nojekyll",
 "app.js",
 "defaults.js",
 "docs/GL121123_measurement_definitions.png",
 "docs/GUIDE.html",
 "icons/icon-192.png",
 "icons/icon-512.png",
 "icons/icon-maskable-512.png",
 "index.html",
 "manifest.webmanifest",
 "py/bridge.py",
 "py/core/measure.py",
 "py/core/params.py",
 "py/core/simpart.py",
 "py/core/training.py",
 "py/core/trigger.py",
 "runtime/et_xmlfile-2.0.0-py3-none-any.whl",
 "runtime/numpy-2.2.5-cp313-cp313-pyodide_2025_0_wasm32.whl",
 "runtime/opencv_python-4.11.0.86-cp313-cp313-pyodide_2025_0_wasm32.whl",
 "runtime/openpyxl-3.1.5-py2.py3-none-any.whl",
 "runtime/pyodide-lock.json",
 "runtime/pyodide.asm.js",
 "runtime/pyodide.asm.wasm",
 "runtime/pyodide.js",
 "runtime/pyodide.mjs",
 "runtime/python_stdlib.zip",
 "style.css",
 "worker.js"
];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((r) => r || fetch(e.request)));
});
