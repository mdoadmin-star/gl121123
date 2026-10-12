/* GL121123 "STUD SHOCKER MTG" – stand-alone tablet inspection app.
   All measuring runs on the tablet (Python engine in a background worker).
   Same engine as the PC station. */
"use strict";
const APP_VERSION = "T1.1";
const $ = (id) => document.getElementById(id);
const KEYS_TRUE = ["thread_len", "shoulder", "bend_start", "d12", "d14", "angle", "drop"];

// =====================================================================
// configuration (stored on the tablet)
// =====================================================================
const DEFAULT_CFG = {
  params: DEFAULT_PARAMS,
  insp: { roi: null, delay: 2.0, motion: 1.5, minarea: 0.5, thmode: "otsu", thfix: 110, avg: 3,
          save: "fail", beep: true, operator: "", auto: true, zoom: false },
  cam: { deviceId: "", res: "4096x3072", demo: false, locked: false, ctl: {} },
  cal: { k: 0.06, w: 0, h: 0, method: "NOT CALIBRATED", date: "", model: null },
  master_true: {},
  password: "1234",
  counters: { total: 0, ok: 0, ng: 0 },
  serial: 0,
};
const clone = (o) => JSON.parse(JSON.stringify(o));
function loadCfg() {
  let c = null;
  try { c = JSON.parse(localStorage.getItem("gl_cfg") || "null"); } catch (e) { c = null; }
  const d = clone(DEFAULT_CFG);
  if (!c) return d;
  for (const k of Object.keys(d)) {
    if (c[k] === undefined) c[k] = d[k];
    else if (d[k] && typeof d[k] === "object" && !Array.isArray(d[k])) c[k] = Object.assign({}, d[k], c[k]);
  }
  const have = new Set(c.params.map((p) => p.key));
  for (const p of d.params) if (!have.has(p.key)) c.params.push(p);
  return c;
}
let cfg = loadCfg();
function saveCfg() { try { localStorage.setItem("gl_cfg", JSON.stringify(cfg)); } catch (e) { toast("Could not save settings: " + e); } }
const nowStr = () => { const d = new Date(); return d.toLocaleDateString("en-GB") + " " + d.toTimeString().slice(0, 5); };

// =====================================================================
// small helpers
// =====================================================================
let toastT = null;
function toast(t, ms = 3000) {
  const e = $("toast"); e.textContent = t; e.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => e.classList.remove("show"), ms);
}
function modal(html) { $("m-body").innerHTML = html; $("modal").classList.remove("hidden"); }
$("m-close").onclick = () => $("modal").classList.add("hidden");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, unit) => (v === null || v === undefined || !isFinite(v)) ? "–" :
  (unit === "deg" ? v.toFixed(2) + "°" : v.toFixed(3));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let audio = null;
function beep(ok) {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = ok ? 1200 : 420; o.type = ok ? "sine" : "square";
    g.gain.value = 0.25; o.connect(g); g.connect(audio.destination);
    o.start(); o.stop(audio.currentTime + (ok ? 0.12 : 0.6));
  } catch (e) { /* no sound */ }
}
document.addEventListener("pointerdown", () => { if (!audio) try { audio = new AudioContext(); } catch (e) {} }, { once: true });

// =====================================================================
// measuring worker
// =====================================================================
let worker = null, workerReady = false, wid = 0;
const pending = new Map();
function startWorker() {
  worker = new Worker("worker.js");
  worker.onmessage = (ev) => {
    const m = ev.data;
    if (m.type === "status") { $("ld-text").textContent = m.text; $("ld-fill").style.width = m.text.includes("OpenCV") ? "60%" : "30%"; }
    else if (m.type === "ready") {
      workerReady = true; $("ld-fill").style.width = "100%";
      setTimeout(() => $("loader").classList.add("gone"), 300);
      updateUI();
    } else if (m.type === "fatal") {
      $("ld-text").innerHTML = "<span style='color:#f87171'>Engine could not start:<br>" + esc(m.text) +
        "</span><br><br>Close the app completely and open it again. If it repeats: connect to internet once and reopen.";
    } else if (m.type === "progress") {
      const p = pending.get(m.id); if (p && p.prog) p.prog(m.text);
    } else if (m.type === "result") {
      const p = pending.get(m.id); pending.delete(m.id);
      if (!p) return;
      if (m.error) p.rej(new Error(m.error)); else p.res(m.out);
    }
  };
  worker.onerror = (e) => { $("ld-text").textContent = "Worker error: " + e.message; };
}
function wcall(msg, transfer = [], prog = null) {
  return new Promise((res, rej) => {
    const id = ++wid; pending.set(id, { res, rej, prog });
    msg.id = id; worker.postMessage(msg, transfer);
  });
}

// =====================================================================
// image source: tablet camera or demo (simulated parts)
// =====================================================================
const src = { kind: "none", w: 0, h: 0, el: null, ready: false, stream: null, track: null };
const video = document.createElement("video");
video.muted = true; video.playsInline = true; video.setAttribute("playsinline", "");
const demoCanvas = document.createElement("canvas");

async function stopCamera() {
  if (src.stream) src.stream.getTracks().forEach((t) => t.stop());
  src.stream = null; src.track = null; src.ready = false;
}
async function startCamera() {
  await stopCamera();
  if (cfg.cam.demo) return startDemo();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    stageMsg("Camera not available. The app must be opened from its https:// address in Chrome.");
    return;
  }
  const [rw, rh] = cfg.cam.res.split("x").map(Number);
  const v = { width: { ideal: rw }, height: { ideal: rh }, frameRate: { ideal: 15, max: 30 } };
  if (cfg.cam.deviceId) v.deviceId = { exact: cfg.cam.deviceId }; else v.facingMode = { ideal: "environment" };
  stageMsg("Starting camera …");
  try {
    src.stream = await navigator.mediaDevices.getUserMedia({ video: v, audio: false });
  } catch (e) {
    if (cfg.cam.deviceId) { cfg.cam.deviceId = ""; saveCfg(); return startCamera(); }
    stageMsg("Camera blocked: " + e.name + ".\nAllow the camera: tap the lock icon in the address bar (or Android Settings → Apps → Chrome → Permissions → Camera → Allow), then reopen.");
    return;
  }
  src.track = src.stream.getVideoTracks()[0];
  video.srcObject = src.stream;
  await video.play().catch(() => {});
  await new Promise((r) => { if (video.videoWidth) r(); else video.onloadedmetadata = () => r(); });
  src.kind = "camera"; src.el = video; src.w = video.videoWidth; src.h = video.videoHeight; src.ready = true;
  stageMsg("");
  await applyCamCtl();
  buildCamCtrls();
  await listCameras();
  onSourceChanged();
}
async function listCameras() {
  try {
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    const cur = src.track ? src.track.getSettings().deviceId : "";
    $("cam-select").innerHTML = devs.map((d, i) =>
      `<option value="${esc(d.deviceId)}" ${d.deviceId === cur ? "selected" : ""}>${esc(d.label || "Camera " + (i + 1))}</option>`).join("");
  } catch (e) { /* ignore */ }
}
// --- demo source --------------------------------------------------------
const DEMO = { w: 2448, h: 2048, k0: 0.055, camh: 300, seed: 1 };
async function startDemo() {
  demoCanvas.width = DEMO.w; demoCanvas.height = DEMO.h;
  demoBlank();
  src.kind = "demo"; src.el = demoCanvas; src.w = DEMO.w; src.h = DEMO.h; src.ready = true;
  stageMsg(""); $("cam-info").textContent = `Demo mode – simulated ${DEMO.w}×${DEMO.h} camera. Tap "Next part" under the picture.`;
  $("cam-ctrls").innerHTML = "";
  onSourceChanged();
}
function demoBlank() { const c = demoCanvas.getContext("2d"); c.fillStyle = "rgb(232,232,232)"; c.fillRect(0, 0, DEMO.w, DEMO.h); }
async function demoNext() {
  if (src.kind !== "demo" || !workerReady) return;
  demoBlank(); await sleep(700);
  const master = activeTab === "train";
  const out = await wcall({ cmd: "demo", seed: DEMO.seed++, master, cam_h: DEMO.camh, k0: DEMO.k0, w: DEMO.w, h: DEMO.h });
  const g = new Uint8Array(out.buf), id = new ImageData(DEMO.w, DEMO.h), d = id.data;
  for (let i = 0, j = 0; i < g.length; i++, j += 4) { d[j] = d[j + 1] = d[j + 2] = g[i]; d[j + 3] = 255; }
  demoCanvas.getContext("2d").putImageData(id, 0, 0);
  demoTruth = out.truth;
}
let demoTruth = null;

// --- camera controls (focus / exposure / zoom / torch) --------------------
const CTL = [["zoom", "Zoom"], ["focusDistance", "Focus distance"], ["exposureCompensation", "Brightness (exposure comp.)"],
  ["exposureTime", "Exposure time"], ["iso", "ISO"], ["colorTemperature", "White balance K"]];
function caps() { try { return (src.track && src.track.getCapabilities) ? src.track.getCapabilities() : {}; } catch (e) { return {}; } }
async function setCtl(obj) {
  if (!src.track) return false;
  try { await src.track.applyConstraints({ advanced: [obj] }); return true; } catch (e) { return false; }
}
function buildCamCtrls() {
  const c = caps(), s = src.track ? src.track.getSettings() : {};
  let h = "";
  for (const [k, name] of CTL) {
    if (!c[k] || c[k].min === undefined || c[k].max === undefined || c[k].max <= c[k].min) continue;
    const v = s[k] !== undefined ? s[k] : c[k].min;
    h += `<label class="grid1 small">${name}: <b id="cv-${k}">${+(+v).toFixed(3)}</b><br>
      <input type="range" style="width:100%" data-ctl="${k}" min="${c[k].min}" max="${c[k].max}" step="${c[k].step || (c[k].max - c[k].min) / 100}" value="${v}"></label>`;
  }
  if (c.torch) h += `<div class="row"><label class="tgl"><input type="checkbox" id="chk-torch" ${s.torch ? "checked" : ""}><span>Torch (flash light) – only if no backlight</span></label></div>`;
  $("cam-ctrls").innerHTML = h || "<p class='small muted'>This camera does not allow manual focus / exposure from the browser – it stays automatic.</p>";
  document.querySelectorAll("[data-ctl]").forEach((el) => {
    el.oninput = async () => {
      const k = el.dataset.ctl, v = +el.value; $("cv-" + k).textContent = +v.toFixed(3);
      const o = { [k]: v };
      if (k === "focusDistance") o.focusMode = "manual";
      if (k === "exposureTime" || k === "iso") o.exposureMode = "manual";
      if (k === "colorTemperature") o.whiteBalanceMode = "manual";
      await setCtl(o);
      cfg.cam.ctl[k] = v; saveCfg();
    };
  });
  if ($("chk-torch")) $("chk-torch").onchange = (e) => setCtl({ torch: e.target.checked });
  const st = src.track.getSettings();
  $("cam-info").textContent = `Camera gives ${src.w}×${src.h} px` + (st.frameRate ? ` @ ${Math.round(st.frameRate)} fps` : "") +
    `. Focus: ${st.focusMode || "?"}, exposure: ${st.exposureMode || "?"}` + (cfg.cam.locked ? " (locked)" : "");
}
async function applyCamCtl() {
  for (const [k, v] of Object.entries(cfg.cam.ctl || {})) {
    const o = { [k]: v };
    if (k === "focusDistance") o.focusMode = "manual";
    if (k === "exposureTime" || k === "iso") o.exposureMode = "manual";
    if (k === "colorTemperature") o.whiteBalanceMode = "manual";
    await setCtl(o);
  }
  if (cfg.cam.locked) await lockCam(true);
}
async function lockCam(quiet) {
  const c = caps(), done = [];
  if ((c.focusMode || []).includes("manual")) { if (await setCtl({ focusMode: "manual" })) done.push("focus"); }
  if ((c.exposureMode || []).includes("manual")) { if (await setCtl({ exposureMode: "manual" })) done.push("exposure"); }
  if ((c.whiteBalanceMode || []).includes("manual")) { if (await setCtl({ whiteBalanceMode: "manual" })) done.push("white balance"); }
  cfg.cam.locked = true; saveCfg();
  if (!quiet) toast(done.length ? "Locked: " + done.join(", ") : "This camera does not support locking from the browser (stays automatic).", 4000);
  if (src.track) buildCamCtrls();
}
async function unlockCam() {
  for (const k of ["focusMode", "exposureMode", "whiteBalanceMode"]) await setCtl({ [k]: "continuous" });
  cfg.cam.locked = false; cfg.cam.ctl = {}; saveCfg();
  toast("Focus and exposure set to automatic"); if (src.track) buildCamCtrls();
}

// =====================================================================
// frame grabbing
// =====================================================================
const full = document.createElement("canvas");
const fullCtx = full.getContext("2d", { willReadFrequently: true });
const small = document.createElement("canvas");
const smallCtx = small.getContext("2d", { willReadFrequently: true });
function nextFrame() {
  return new Promise((r) => {
    if (src.kind === "camera" && video.requestVideoFrameCallback) {
      let done = false; video.requestVideoFrameCallback(() => { done = true; r(); });
      setTimeout(() => { if (!done) r(); }, 150);
    } else setTimeout(r, 40);
  });
}
function rgbaToGray(d, out) {
  for (let i = 0, j = 0; j < out.length; i += 4, j++) out[j] = (77 * d[i] + 150 * d[i + 1] + 29 * d[i + 2]) >> 8;
  return out;
}
/** full-resolution grey image, average of n frames */
async function grabGray(n = 1) {
  const w = src.w, h = src.h;
  if (full.width !== w || full.height !== h) { full.width = w; full.height = h; }
  const N = w * h, out = new Uint8Array(N);
  if (n <= 1 || src.kind === "demo") {
    fullCtx.drawImage(src.el, 0, 0, w, h);
    return rgbaToGray(fullCtx.getImageData(0, 0, w, h).data, out);
  }
  const acc = new Uint16Array(N), tmp = new Uint8Array(N);
  for (let f = 0; f < n; f++) {
    if (f) await nextFrame();
    fullCtx.drawImage(src.el, 0, 0, w, h);
    rgbaToGray(fullCtx.getImageData(0, 0, w, h).data, tmp);
    for (let i = 0; i < N; i++) acc[i] += tmp[i];
  }
  for (let i = 0; i < N; i++) out[i] = Math.round(acc[i] / n);
  return out;
}
function roiPx() {
  const r = cfg.insp.roi;
  if (!r) return [0, 0, src.w, src.h];
  const x = Math.max(0, Math.round(r[0] * src.w)), y = Math.max(0, Math.round(r[1] * src.h));
  return [x, y, Math.min(src.w - x, Math.round(r[2] * src.w)), Math.min(src.h - y, Math.round(r[3] * src.h))];
}
/** small grey copy of the inspection area for the auto trigger */
function grabSmall() {
  const [x, y, w, h] = roiPx();
  const sw = 240, sh = Math.max(16, Math.min(480, Math.round(240 * h / w)));
  if (small.width !== sw || small.height !== sh) { small.width = sw; small.height = sh; }
  smallCtx.drawImage(src.el, x, y, w, h, 0, 0, sw, sh);
  const g = new Uint8Array(sw * sh);
  rgbaToGray(smallCtx.getImageData(0, 0, sw, sh).data, g);
  return { g, w: sw, h: sh };
}

// =====================================================================
// auto trigger (same logic as PC station core/trigger.py)
// =====================================================================
class AutoTrigger {
  constructor() { this.state = "EMPTY"; this.prev = null; this.stable = null; this.emptySince = null; this.big = 0;
    this.remaining = 0; this.partPct = 0; this.motionPct = 0; this.enabled = true; }
  mask(g) {
    const hist = new Uint32Array(256); for (let i = 0; i < g.length; i++) hist[g[i]]++;
    let t;
    if (cfg.insp.thmode === "fixed") t = cfg.insp.thfix;
    else {
      const pct = (q) => { let c = 0, lim = q * g.length; for (let i = 0; i < 256; i++) { c += hist[i]; if (c >= lim) return i; } return 255; };
      if (pct(0.99) - pct(0.01) < 40) return new Uint8Array(g.length);
      let sum = 0; for (let i = 0; i < 256; i++) sum += i * hist[i];
      let sB = 0, wB = 0, best = -1; t = 0;
      for (let i = 0; i < 256; i++) {
        wB += hist[i]; if (!wB) continue; const wF = g.length - wB; if (!wF) break;
        sB += i * hist[i]; const mB = sB / wB, mF = (sum - sB) / wF, v = wB * wF * (mB - mF) * (mB - mF);
        if (v > best) { best = v; t = i; }
      }
    }
    const m = new Uint8Array(g.length); for (let i = 0; i < g.length; i++) m[i] = g[i] <= t ? 1 : 0;
    return m;
  }
  update(g, w, h, now) {
    const m = this.mask(g); let n = 0; for (let i = 0; i < m.length; i++) n += m[i];
    this.partPct = 100 * n / m.length;
    const present = this.partPct >= cfg.insp.minarea;
    let edge = false;
    for (let x = 0; x < w && !edge; x++) edge = m[x] || m[(h - 1) * w + x];
    for (let y = 0; y < h && !edge; y++) edge = m[y * w] || m[y * w + w - 1];
    if (this.prev && this.prev.length === m.length && n > 0) {
      let c = 0; for (let i = 0; i < m.length; i++) c += m[i] ^ this.prev[i];
      this.motionPct = 100 * c / Math.max(n, 1);
    } else this.motionPct = 100;
    this.prev = m;
    if (this.state === "INSPECTING") return false;
    if (!present) {
      if (this.state === "RESULT") { this.emptySince = this.emptySince || now; if (now - this.emptySince > 400) this.state = "EMPTY"; }
      else this.state = "EMPTY";
      this.stable = null; return false;
    }
    this.emptySince = null;
    if (this.state === "RESULT") {
      this.big = this.motionPct > 10 * cfg.insp.motion ? this.big + 1 : 0;
      if (this.big >= 3) { this.state = "MOVING"; this.stable = null; }
      return false;
    }
    if (edge) { this.state = "EDGE"; this.stable = null; }
    else if (this.motionPct > cfg.insp.motion) { this.state = "MOVING"; this.stable = null; }
    else {
      if (this.stable === null) this.stable = now;
      this.remaining = cfg.insp.delay * 1000 - (now - this.stable);
      this.state = "COUNTDOWN";
      if (this.remaining <= 0 && this.enabled) { this.state = "INSPECTING"; return true; }
    }
    return false;
  }
  done() { this.state = "RESULT"; this.big = 0; }
}
const trig = new AutoTrigger();

// =====================================================================
// display
// =====================================================================
const view = $("view"), ovl = $("ovl"), vctx = view.getContext("2d"), octx = ovl.getContext("2d");
let roiEdit = false, roiDrag = null, last = null, busy = false, activeTab = "inspect";
function stageMsg(t) { $("stage-msg").textContent = t; }
function viewRect() {
  if (cfg.insp.zoom && cfg.insp.roi && !roiEdit) {
    const [x, y, w, h] = roiPx(), p = 0.06 * Math.max(w, h);
    const x0 = Math.max(0, x - p), y0 = Math.max(0, y - p);
    return [x0, y0, Math.min(src.w, x + w + p) - x0, Math.min(src.h, y + h + p) - y0];
  }
  return [0, 0, src.w, src.h];
}
let T = { sc: 1, ox: 0, oy: 0, vr: [0, 0, 1, 1] };
function layout() {
  const st = $("stage"), dpr = window.devicePixelRatio || 1;
  const W = Math.round(st.clientWidth * dpr), H = Math.round(st.clientHeight * dpr);
  for (const c of [view, ovl]) if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
  const vr = viewRect(), sc = Math.min(W / vr[2], H / vr[3]);
  T = { sc, ox: (W - vr[2] * sc) / 2, oy: (H - vr[3] * sc) / 2, vr, dpr };
}
const I2S = (p) => [T.ox + (p[0] - T.vr[0]) * T.sc, T.oy + (p[1] - T.vr[1]) * T.sc];
const S2I = (x, y) => [(x - T.ox) / T.sc + T.vr[0], (y - T.oy) / T.sc + T.vr[1]];

function render() {
  requestAnimationFrame(render);
  if (!src.ready) return;
  layout();
  const vr = T.vr;
  vctx.fillStyle = "#000"; vctx.fillRect(0, 0, view.width, view.height);
  vctx.drawImage(src.el, vr[0], vr[1], vr[2], vr[3], T.ox, T.oy, vr[2] * T.sc, vr[3] * T.sc);
  drawOverlay();
}
function drawOverlay() {
  const c = octx, d = T.dpr;
  c.clearRect(0, 0, ovl.width, ovl.height);
  // inspection area
  const r = roiDrag || (cfg.insp.roi ? roiPx() : null);
  if (r) {
    const a = I2S([r[0], r[1]]), b = I2S([r[0] + r[2], r[1] + r[3]]);
    c.setLineDash([10 * d, 6 * d]); c.lineWidth = 2 * d; c.strokeStyle = roiEdit ? "#38bdf8" : "#facc15";
    c.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]); c.setLineDash([]);
  }
  if (roiEdit) {
    c.fillStyle = "#38bdf8"; c.font = `${18 * d}px sans-serif`;
    c.fillText("Drag a box around the part area – then tap  Set area  again", 12 * d, 28 * d);
  }
  // training capture positions
  if (activeTab === "train") {
    for (const cp of trainCaps) {
      const ct = cp.features.centroid; if (!ct) continue;
      const p = I2S(ct); c.fillStyle = "#38bdf8"; c.beginPath(); c.arc(p[0], p[1], 7 * d, 0, 7); c.fill();
    }
  }
  // last result
  const show = last && last.geom && (trig.state === "RESULT" || trig.state === "INSPECTING" || !cfg.insp.auto);
  if (show) {
    const g = last.geom;
    c.lineWidth = 2.5 * d; c.strokeStyle = last.ok ? "#22c55e" : "#ef4444";
    c.beginPath();
    g.contour.forEach((p, i) => { const s = I2S(p); i ? c.lineTo(s[0], s[1]) : c.moveTo(s[0], s[1]); });
    c.closePath(); c.stroke();
    const dot = (p, col, rad = 6) => { if (!p) return; const s = I2S(p); c.fillStyle = col; c.beginPath(); c.arc(s[0], s[1], rad * d, 0, 7); c.fill(); };
    const failed = new Set((last.rows || []).filter((x) => !x.ok).map((x) => x.key));
    (g.thread_pts || []).forEach((p) => dot(p, failed.has("thread_len") ? "#ef4444" : "#a3e635", 5));
    (g.shoulder_pts || []).forEach((p) => dot(p, failed.has("shoulder") ? "#ef4444" : "#38bdf8", 5));
    dot(g.tip, "#f472b6"); dot(g.bend_start_pt, failed.has("bend_start") ? "#ef4444" : "#fb923c");
    dot(g.corner, failed.has("drop") ? "#ef4444" : "#c084fc"); dot(g.P, "#ffffff", 4);
  }
}

// --- inspection area by touch-drag --------------------------------------------
const stageEl = $("stage");
stageEl.addEventListener("pointerdown", (e) => {
  if (!roiEdit || !src.ready) return;
  stageEl.setPointerCapture(e.pointerId);
  const b = stageEl.getBoundingClientRect(), p = S2I((e.clientX - b.left) * T.dpr, (e.clientY - b.top) * T.dpr);
  roiDrag = [p[0], p[1], 0, 0]; roiDrag.start = p;
});
stageEl.addEventListener("pointermove", (e) => {
  if (!roiDrag) return;
  const b = stageEl.getBoundingClientRect(), p = S2I((e.clientX - b.left) * T.dpr, (e.clientY - b.top) * T.dpr);
  const s = roiDrag.start, x0 = Math.max(0, Math.min(s[0], p[0])), y0 = Math.max(0, Math.min(s[1], p[1]));
  const x1 = Math.min(src.w, Math.max(s[0], p[0])), y1 = Math.min(src.h, Math.max(s[1], p[1]));
  const st = roiDrag.start; roiDrag = [x0, y0, x1 - x0, y1 - y0]; roiDrag.start = st;
});
stageEl.addEventListener("pointerup", () => {
  if (!roiDrag) return;
  const r = roiDrag; roiDrag = null;
  if (r[2] < src.w * 0.05 || r[3] < src.h * 0.05) { toast("Area too small – drag a bigger box"); return; }
  cfg.insp.roi = [r[0] / src.w, r[1] / src.h, r[2] / src.w, r[3] / src.h]; saveCfg();
  trig.prev = null; toast("Inspection area saved");
});
$("btn-roi").onclick = () => {
  roiEdit = !roiEdit; $("btn-roi").classList.toggle("on", roiEdit);
  $("btn-roi").textContent = roiEdit ? "Done" : "Set area";
};
$("btn-roi-clear").onclick = () => { cfg.insp.roi = null; saveCfg(); trig.prev = null; toast("Whole picture is used"); };
$("btn-zoom").onclick = () => { cfg.insp.zoom = !cfg.insp.zoom; saveCfg(); $("btn-zoom").classList.toggle("on", cfg.insp.zoom); };
$("btn-full").onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {});
};

// =====================================================================
// banner + results
// =====================================================================
const MSG = { EMPTY: ["PLACE PART", ""], EDGE: ["REMOVE HAND", "part touches the area border"],
  MOVING: ["HOLD STILL", ""], COUNTDOWN: ["", ""], INSPECTING: ["INSPECTING", ""] };
function setBanner(cls, text, sub = "") {
  const b = $("banner"); b.className = cls; $("banner-text").textContent = text; $("banner-sub").textContent = sub;
}
function bannerFromTrigger() {
  if (busy || trig.state === "RESULT") return;
  if (!workerReady) return setBanner("b-wait", "LOADING", "measuring engine");
  if (!src.ready) return setBanner("b-wait", "NO CAMERA", "");
  if (calWrongSize()) return setBanner("b-warn", "RECALIBRATE", "camera resolution changed");
  const s = trig.state;
  if (s === "COUNTDOWN") {
    if (!trig.enabled) return setBanner("b-wait", "PART READY", activeTab === "train" ? "tap Capture now" : "tap Inspect now");
    return setBanner("b-busy", (Math.max(0, trig.remaining) / 1000).toFixed(1) + " s", activeTab === "train" ? "capturing" : "inspecting soon");
  }
  const m = MSG[s] || [s, ""];
  setBanner("b-wait", m[0], m[1]);
}
function showRows(rows) {
  const tb = $("res-table").tBodies[0];
  const ps = cfg.params.filter((p) => p.enabled);
  const byKey = {}; (rows || []).forEach((r) => (byKey[r.key] = r));
  tb.innerHTML = ps.map((p) => {
    const r = byKey[p.key], lo = p.nominal - Math.abs(p.tol_minus), hi = p.nominal + Math.abs(p.tol_plus);
    const v = r ? r.value : null, ok = r ? r.ok : null;
    const pos = (v !== null && v !== undefined && hi > lo) ? Math.max(-5, Math.min(105, (v - lo) / (hi - lo) * 100)) : null;
    const cls = ok === null ? "" : ok ? "p" : "f";
    const st = ok === null ? "" : ok ? "<span class='st' style='color:#4ade80'>OK</span>" :
      `<span class='st' style='color:#f87171'>${esc(r.note || "NG")}</span>`;
    return `<tr class="${cls}"><td>${esc(p.name)}${p.assumed ? "*" : ""}</td><td class="v">${fmt(v, p.unit)}` +
      (pos !== null ? `<div class="bar"><i style="left:calc(${pos}% - 1px)"></i></div>` : "") +
      `</td><td class="l">${fmt(lo, p.unit)} – ${fmt(hi, p.unit)}</td><td>${st}</td></tr>`;
  }).join("");
}
function updateCounters() {
  const c = cfg.counters;
  $("c-total").textContent = c.total; $("c-ok").textContent = c.ok; $("c-ng").textContent = c.ng;
  $("c-yield").textContent = c.total ? (100 * c.ok / c.total).toFixed(1) + "%" : "–";
}

// =====================================================================
// inspection
// =====================================================================
function measArgs(useModel = true) {
  const m = cfg.cal.model;
  return { roi: roiPx(), k: cfg.cal.k, model: (useModel && m) ? { H: m.H, cx: m.cx, cy: m.cy } : null,
           params: cfg.params, threshold_mode: cfg.insp.thmode, fixed_threshold: cfg.insp.thfix };
}
async function measureNow(nAvg, useModel = true, params = null) {
  const g = await grabGray(nAvg);
  const a = measArgs(useModel); if (params) a.params = params;
  return wcall({ cmd: "measure", buf: g.buffer, w: src.w, h: src.h, ch: 1, args: a }, [g.buffer]);
}
function calWrongSize() { return cfg.cal.w && src.ready && (cfg.cal.w !== src.w || cfg.cal.h !== src.h); }

async function inspect() {
  if (busy || !workerReady || !src.ready) return;
  busy = true; trig.state = "INSPECTING";
  setBanner("b-busy", "INSPECTING", "");
  const t0 = performance.now();
  let r;
  try { r = await measureNow(cfg.insp.avg); }
  catch (e) { r = { ok: false, error: "Software error: " + e.message, rows: [] }; }
  const dt = performance.now() - t0;
  busy = false; trig.done();
  last = r;
  const failed = (r.rows || []).filter((x) => !x.ok).map((x) => x.name);
  if (r.error) {
    setBanner("b-ng", "NG", r.error);
  } else {
    setBanner(r.ok ? "b-ok" : "b-ng", r.ok ? "OK" : "NG", r.ok ? "" : failed.join(", "));
  }
  showRows(r.rows);
  cfg.counters.total++; r.ok ? cfg.counters.ok++ : cfg.counters.ng++; cfg.serial++;
  saveCfg(); updateCounters();
  $("res-info").textContent = `#${cfg.serial} · ${nowStr()} · ${(dt / 1000).toFixed(1)} s` +
    (r.raw && r.raw.scale_check ? ` · scale check ${((r.raw.scale_check - 1) * 100).toFixed(1)}%` : "") +
    (demoTruth && src.kind === "demo" ? " · demo" : "");
  if (r.raw && r.raw.scale_check && Math.abs(r.raw.scale_check - 1) > 0.04 && !r.error)
    toast("Ø14 reads " + ((r.raw.scale_check - 1) * 100).toFixed(1) + "% off – calibration may be wrong (camera moved?)", 5000);
  if (cfg.insp.beep && !r.ok) beep(false);
  // history
  const values = {}; (r.rows || []).forEach((x) => { if (x.value !== null && x.value !== undefined) values[x.key] = x.value; });
  const rec = { ts: Date.now(), date: new Date().toLocaleDateString("en-GB"), time: new Date().toTimeString().slice(0, 8),
    serial: cfg.serial, ok: !!r.ok, failed: r.error ? r.error : failed.join(", "), values, operator: cfg.insp.operator || "" };
  if (cfg.insp.save === "all" || (cfg.insp.save === "fail" && !r.ok)) rec.img = await snapshot(r);
  dbAdd(rec).catch((e) => toast("History not saved: " + e));
}
/** jpeg of the inspection area with the measured outline (for NG proof) */
async function snapshot(r) {
  try {
    const [x, y, w, h] = roiPx(), s = Math.min(1, 1000 / Math.max(w, h));
    const c = document.createElement("canvas"); c.width = Math.round(w * s); c.height = Math.round(h * s);
    const g = c.getContext("2d"); g.drawImage(src.el, x, y, w, h, 0, 0, c.width, c.height);
    if (r.geom) {
      g.strokeStyle = r.ok ? "#16a34a" : "#dc2626"; g.lineWidth = 2; g.beginPath();
      r.geom.contour.forEach((p, i) => { const q = [(p[0] - x) * s, (p[1] - y) * s]; i ? g.lineTo(q[0], q[1]) : g.moveTo(q[0], q[1]); });
      g.closePath(); g.stroke();
    }
    g.fillStyle = "#000a"; g.fillRect(0, 0, c.width, 26); g.fillStyle = "#fff"; g.font = "16px sans-serif";
    g.fillText(`#${cfg.serial} ${nowStr()} ${r.ok ? "OK" : "NG"} ${r.error || ""}`, 6, 19);
    return await new Promise((res) => c.toBlob(res, "image/jpeg", 0.75));
  } catch (e) { return null; }
}
$("btn-inspect").onclick = () => { trig.state = "INSPECTING"; inspect(); };
$("chk-auto").onchange = (e) => { cfg.insp.auto = e.target.checked; saveCfg(); };
$("btn-reset-count").onclick = () => { if (confirm("Reset OK / NG counters?")) { cfg.counters = { total: 0, ok: 0, ng: 0 }; saveCfg(); updateCounters(); } };
$("operator").onchange = (e) => { cfg.insp.operator = e.target.value.trim(); saveCfg(); };

// trigger loop
setInterval(() => {
  if (!src.ready || !workerReady) { bannerFromTrigger(); return; }
  if (busy) return;
  const autoOn = activeTab === "train" ? $("chk-capauto").checked : (cfg.insp.auto && (activeTab === "inspect" || activeTab === "history"));
  trig.enabled = autoOn && !roiEdit && !calWrongSize();
  let s;
  try { s = grabSmall(); } catch (e) { return; }
  const fire = trig.update(s.g, s.w, s.h, performance.now());
  bannerFromTrigger();
  $("trig-info").textContent = `${trig.partPct.toFixed(0)}% · ${trig.motionPct > 99 ? "–" : trig.motionPct.toFixed(1) + "%"}` +
    (activeTab === "setup" || activeTab === "settings" ? " · paused" : "");
  if (fire) { if (activeTab === "train") trainCapture(true); else inspect(); }
}, 120);

// =====================================================================
// history (IndexedDB on the tablet)
// =====================================================================
let dbP = null;
function db() {
  dbP = dbP || new Promise((res, rej) => {
    const r = indexedDB.open("gl121123", 1);
    r.onupgradeneeded = () => { const s = r.result.createObjectStore("results", { keyPath: "id", autoIncrement: true }); s.createIndex("ts", "ts"); };
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  return dbP;
}
async function dbAdd(rec) {
  const d = await db();
  return new Promise((res, rej) => { const t = d.transaction("results", "readwrite"); t.objectStore("results").add(rec); t.oncomplete = res; t.onerror = () => rej(t.error); });
}
async function dbList(sinceTs) {
  const d = await db();
  return new Promise((res, rej) => {
    const out = [], t = d.transaction("results"), idx = t.objectStore("results").index("ts");
    const rq = idx.openCursor(IDBKeyRange.lowerBound(sinceTs || 0));
    rq.onsuccess = () => { const c = rq.result; if (c) { const v = c.value; out.push(v); c.continue(); } else res(out); };
    rq.onerror = () => rej(rq.error);
  });
}
async function dbClear() { const d = await db(); return new Promise((res) => { const t = d.transaction("results", "readwrite"); t.objectStore("results").clear(); t.oncomplete = res; }); }
async function dbGet(id) { const d = await db(); return new Promise((res) => { const r = d.transaction("results").objectStore("results").get(id); r.onsuccess = () => res(r.result); }); }
function rangeStart() {
  const v = $("hist-range").value, d = new Date();
  if (v === "today") { d.setHours(0, 0, 0, 0); return d.getTime(); }
  if (v === "7") return Date.now() - 7 * 864e5;
  return 0;
}
async function showHistory() {
  const rows = await dbList(rangeStart());
  const ok = rows.filter((r) => r.ok).length;
  $("hist-sum").textContent = `${rows.length} inspections · ${ok} OK · ${rows.length - ok} NG` +
    (rows.length ? ` · yield ${(100 * ok / rows.length).toFixed(1)} %` : "");
  $("hist-table").tBodies[0].innerHTML = rows.slice(-300).reverse().map((r) =>
    `<tr class="${r.ok ? "p" : "f"}"><td>${r.serial}</td><td class="l">${esc(r.date)}<br>${esc(r.time)}</td>` +
    `<td class="v">${r.ok ? "OK" : "NG"}</td><td class="small">${esc(r.failed || "")}</td>` +
    `<td>${r.img ? `<button data-img="${r.id}">Photo</button>` : ""}</td></tr>`).join("");
  document.querySelectorAll("[data-img]").forEach((b) => (b.onclick = async () => {
    const r = await dbGet(+b.dataset.img); if (!r || !r.img) return;
    const u = URL.createObjectURL(r.img);
    const vals = cfg.params.filter((p) => p.enabled).map((p) => `${esc(p.name)}: <b>${fmt(r.values[p.key], p.unit)}</b>`).join(" · ");
    modal(`<img src="${u}"><p class="small">#${r.serial} ${esc(r.date)} ${esc(r.time)} – ${r.ok ? "OK" : "NG"} ${esc(r.failed || "")}<br>${vals}</p>`);
  }));
}
function download(blob, name) {
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 20000);
}
async function shareOrDownload(blob, name) {
  const f = new File([blob], name, { type: blob.type });
  if (navigator.canShare && navigator.canShare({ files: [f] }) && confirm("Share the file (WhatsApp, e-mail, Drive …)?\nCancel = save to Downloads.")) {
    try { await navigator.share({ files: [f], title: name }); return; } catch (e) { /* fall back */ }
  }
  download(blob, name); toast("Saved to Downloads: " + name, 4000);
}
const stamp = () => new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "").replace(/^(\d{8})/, "$1_");
$("btn-xlsx").onclick = async () => {
  const rows = await dbList(rangeStart());
  if (!rows.length) return toast("No inspections in this period");
  toast("Making Excel file …");
  try {
    const out = await wcall({ cmd: "excel", rows: rows.map(({ img, ...r }) => r), params: cfg.params,
      title: "GL121123 STUD SHOCKER MTG – inspection report " + nowStr() });
    await shareOrDownload(new Blob([out.bytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }), `GL121123_inspection_${stamp()}.xlsx`);
  } catch (e) { toast("Excel failed: " + e.message, 5000); }
};
$("btn-csv").onclick = async () => {
  const rows = await dbList(rangeStart());
  if (!rows.length) return toast("No inspections in this period");
  const ps = cfg.params.filter((p) => p.enabled);
  const lines = [["No", "Date", "Time", "Operator", "Result", "Failed", ...ps.map((p) => p.name)].join(",")];
  for (const r of rows) lines.push([r.serial, r.date, r.time, r.operator, r.ok ? "OK" : "NG", `"${(r.failed || "").replace(/"/g, "'")}"`,
    ...ps.map((p) => r.values[p.key] === undefined ? "" : r.values[p.key].toFixed(4))].join(","));
  await shareOrDownload(new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv" }), `GL121123_inspection_${stamp()}.csv`);
};
$("hist-range").onchange = showHistory;
$("btn-hist-clear").onclick = async () => {
  const pw = prompt("Delete ALL saved inspections and photos?\nEnter the settings password:");
  if (pw === null) return; if (pw !== cfg.password) return toast("Wrong password");
  await dbClear(); showHistory(); toast("History deleted");
};

// =====================================================================
// calibration
// =====================================================================
function calStatus() {
  const c = cfg.cal, fov = src.ready ? ` · picture covers ${(src.w * c.k).toFixed(0)} × ${(src.h * c.k).toFixed(0)} mm` : "";
  const bad = c.method.startsWith("NOT");
  $("cal-status").innerHTML = `<b>${c.k.toFixed(5)} mm/px</b> · ${esc(c.method)} ${esc(c.date || "")}${fov}` +
    (c.model ? `<br>3D correction ON (camera ${c.model.H ? c.model.H.toFixed(0) + " mm above part" : "far"})` : "<br>3D correction off") +
    (c.w ? `<br>calibrated at ${c.w}×${c.h}` : "") + (bad ? "<br><span style='color:#fbbf24'>Not calibrated – results are not real mm yet.</span>" : "");
  $("cal-k").value = c.k.toFixed(6);
  const w = $("cal-warn");
  if (calWrongSize()) { w.textContent = `Calibrated at ${c.w}×${c.h} but the camera now gives ${src.w}×${src.h}. Select the same resolution in Setup or calibrate again.`; w.classList.remove("hidden"); }
  else if (bad) { w.textContent = "Not calibrated yet – go to Setup → Calibration."; w.classList.remove("hidden"); }
  else w.classList.add("hidden");
}
function setCal(k, method) {
  cfg.cal = { k, w: src.w, h: src.h, method, date: nowStr(), model: null };
  cfg.params.forEach((p) => (p.offset = 0));
  saveCfg(); calStatus();
}
$("btn-cal-master").onclick = async () => {
  const tr = trueValues();
  if (Object.keys(tr).length < 2) return toast("First enter the master's true values in the Training tab", 4000);
  if (!src.ready || !workerReady) return;
  if (!confirm("Set the scale from the master part?\nThis also switches off the 3D correction and clears offsets (do Training afterwards).")) return;
  $("cal-out").textContent = "Measuring master …";
  try {
    const r = await measureNow(3, false);
    if (r.error) { $("cal-out").textContent = "Master not measured: " + r.error; return; }
    const s = await wcall({ cmd: "scale_from_master", features: r.features, true: tr, k: cfg.cal.k });
    if (!s.ok) { $("cal-out").textContent = "Could not compute scale."; return; }
    setCal(s.k, "master part");
    $("cal-out").innerHTML = `<div class="good">Scale set: ${s.k.toFixed(5)} mm/px (changed ${s.change_pct.toFixed(2)} %). ` +
      `Next: Training tab → capture the master 10–20 times → Analyse → Apply.</div>`;
  } catch (e) { $("cal-out").textContent = "Error: " + e.message; }
};
$("btn-cal-cb").onclick = async () => {
  if (!src.ready || !workerReady) return;
  $("cal-out").textContent = "Searching checkerboard …";
  try {
    const g = await grabGray(3);
    const cols = +$("cb-cols").value, rows = +$("cb-rows").value, sq = +$("cb-sq").value, hc = +$("cb-h").value;
    const r = await wcall({ cmd: "checkerboard", buf: g.buffer, w: src.w, h: src.h, ch: 1, cols, rows, square: sq }, [g.buffer]);
    if (!r.ok) { $("cal-out").innerHTML = `<div class="err">Checkerboard not found. Whole board must be visible, flat, sharp, no reflections. Inner corners = squares − 1 (A4 sheet: 9 × 6).</div>`; return; }
    // board lies on the table, part axis is 7 mm higher (Ø14 / 2) -> slightly bigger in the picture
    const k = hc > 20 ? r.k * (hc - 7) / hc : r.k;
    setCal(k, "checkerboard");
    $("cal-out").innerHTML = `<div class="${r.tilt_pct > 1 ? "warn" : "good"}">Scale ${k.toFixed(5)} mm/px (board ${r.k.toFixed(5)}, corrected to part height).` +
      (r.tilt_pct > 1 ? ` Tablet is tilted (${r.tilt_pct.toFixed(1)} % difference X/Y) – make the tablet parallel to the table.` : " Tablet is parallel – good.") +
      ` Next: master scale or Training.</div>`;
  } catch (e) { $("cal-out").textContent = "Error: " + e.message; }
};
$("btn-cal-k").onclick = () => { const k = +$("cal-k").value; if (k > 0.001 && k < 1) { setCal(k, "manual"); toast("Saved"); } else toast("Value must be 0.001 … 1"); };

// image check
$("btn-check").onclick = async () => {
  if (!src.ready || !workerReady) return;
  const g = await grabGray(1);
  const s = await wcall({ cmd: "stats", buf: g.buffer, w: src.w, h: src.h, ch: 1 }, [g.buffer]);
  const tips = [];
  if (s.bg < 150) tips.push("Background too dark → use a white LED backlight panel under the part / increase brightness.");
  if (s.sat > 20) tips.push("Over-exposed (" + s.sat.toFixed(0) + " % pure white) → reduce brightness (exposure comp.) – edges get eaten.");
  if (s.focus < 30) tips.push("Picture looks soft → tap Auto focus, then Lock when sharp; or adjust focus distance.");
  $("check-out").innerHTML = `Background ${s.bg.toFixed(0)} /255 · white-clipped ${s.sat.toFixed(1)} % · sharpness ${s.focus.toFixed(0)}` +
    (tips.length ? `<div class="warn">${tips.join("<br>")}</div>` : `<div class="good">Image looks good.</div>`);
  try {
    const r = await measureNow(1);
    $("check-out").innerHTML += r.error ? `<div class="warn">Part not measured: ${esc(r.error)}</div>` :
      `<div class="small">Part found · Ø14 reads ${fmt(r.raw.d14)} mm · measuring time ${(r.time_ms / 1000).toFixed(1)} s</div>`;
    last = r;
  } catch (e) { /* ignore */ }
};

// =====================================================================
// accuracy training
// =====================================================================
let trainCaps = [];
try { trainCaps = JSON.parse(localStorage.getItem("gl_caps") || "[]"); } catch (e) { trainCaps = []; }
let lastTrain = null;
function saveCaps() { try { localStorage.setItem("gl_caps", JSON.stringify(trainCaps)); } catch (e) { toast("Captures not saved: storage full"); } }
function buildTrueGrid() {
  $("true-grid").innerHTML = KEYS_TRUE.map((k) => {
    const p = cfg.params.find((x) => x.key === k) || { name: k, nominal: "" };
    const v = cfg.master_true[k];
    return `<label>${esc(p.name)} (nom ${p.nominal})<input type="number" step="0.001" data-true="${k}" value="${v === undefined ? "" : v}" placeholder="measured"></label>`;
  }).join("");
  document.querySelectorAll("[data-true]").forEach((el) => (el.onchange = () => {
    const v = parseFloat(el.value);
    if (isFinite(v)) cfg.master_true[el.dataset.true] = v; else delete cfg.master_true[el.dataset.true];
    saveCfg();
  }));
}
function trueValues() {
  const o = {}; for (const k of KEYS_TRUE) if (isFinite(cfg.master_true[k])) o[k] = cfg.master_true[k]; return o;
}
function capInfo() { $("cap-n").textContent = trainCaps.length; }
async function trainCapture(auto) {
  if (busy || !workerReady || !src.ready) return;
  busy = true; trig.state = "INSPECTING"; setBanner("b-busy", "CAPTURING", "");
  try {
    const r = await measureNow(cfg.insp.avg, true);
    if (r.error) { setBanner("b-ng", "NOT CAPTURED", r.error); }
    else {
      trainCaps.push({ features: r.features, ts: Date.now() }); saveCaps(); capInfo();
      setBanner("b-ok", "CAPTURED " + trainCaps.length, "now move the master to a new position");
      last = null;
      beep(true);
    }
  } catch (e) { setBanner("b-ng", "ERROR", e.message); }
  busy = false; trig.done();
  if (src.kind === "demo" && auto) setTimeout(demoNext, 800);
}
$("btn-cap").onclick = () => { trig.state = "INSPECTING"; trainCapture(false); };
$("btn-cap-undo").onclick = () => { trainCaps.pop(); saveCaps(); capInfo(); };
$("btn-cap-clear").onclick = () => { if (confirm("Delete all training captures?")) { trainCaps = []; saveCaps(); capInfo(); } };
$("btn-analyse").onclick = async () => {
  const tr = trueValues();
  if (Object.keys(tr).length < 3) return toast("Enter at least 3 true values of the master first", 4000);
  const caps = trainCaps.filter((c) => c.features.img_size[0] === src.w && c.features.img_size[1] === src.h);
  if (caps.length < trainCaps.length) toast(`${trainCaps.length - caps.length} captures were taken at another resolution – ignored`, 4000);
  if (caps.length < 3) return toast("Need at least 3 captures (10–20 recommended)");
  $("btn-analyse").disabled = true; $("btn-apply").disabled = true; $("an-out").innerHTML = "";
  const t0 = performance.now();
  try {
    const m = cfg.cal.model;
    const res = await wcall({ cmd: "train", caps: caps.map((c) => c.features), true: tr, params: cfg.params, k: cfg.cal.k,
      w: src.w, h: src.h, model: m ? { k: cfg.cal.k, H: m.H, cx: m.cx, cy: m.cy } : null }, [],
    (t) => ($("an-prog").textContent = t));
    lastTrain = res; showTrain(res);
    $("an-prog").textContent = `done in ${((performance.now() - t0) / 1000).toFixed(0)} s`;
  } catch (e) { $("an-out").innerHTML = `<div class="err">${esc(e.message)}</div>`; $("an-prog").textContent = ""; }
  $("btn-analyse").disabled = false;
};
function showTrain(r) {
  let h = "";
  if (r.stats) {
    h += r.valid ? `<div class="good">Training result is valid (${r.n} captures, ${r.excluded.length} ignored). Check the table, then tap <b>Apply</b>.</div>` : "";
    h += `<table class="stats"><tr><th>Parameter</th><th>True</th><th>Error before</th><th>Accuracy after (±3σ)</th><th>% of tol</th><th>Verdict</th></tr>` +
      r.stats.map((s) => `<tr><td>${esc(s.name)}</td><td>${fmt(s.true, s.unit)}</td><td>${s.before_bias >= 0 ? "+" : ""}${fmt(s.before_bias, s.unit)}</td>` +
        `<td>±${fmt(s.acc3s, s.unit)}</td><td>${s.pct_tol === null ? "–" : s.pct_tol.toFixed(0) + "%"}</td>` +
        `<td class="v-${String(s.verdict).split(" ")[0]}">${esc(s.verdict)}</td></tr>`).join("") + `</table>`;
    if (r.model) h += `<p class="small muted">Model: ${r.model.k.toFixed(5)} mm/px, camera ${r.model.H ? r.model.H.toFixed(0) + " mm above part" : "far"}, optical centre ${r.model.cx.toFixed(0)}, ${r.model.cy.toFixed(0)}</p>`;
  }
  for (const [lvl, t] of r.mistakes || []) h += `<div class="${lvl === "error" ? "err" : lvl === "warn" ? "warn" : "good"}">${esc(t)}</div>`;
  $("an-out").innerHTML = h;
  $("btn-apply").disabled = !(r.valid && r.model);
}
$("btn-apply").onclick = () => {
  const r = lastTrain; if (!r || !r.valid) return;
  if (!confirm("Use the trained camera model and offsets for inspection?")) return;
  localStorage.setItem("gl_cfg_before_training", JSON.stringify(cfg));
  cfg.cal = { k: r.model.k, w: src.w, h: src.h, method: `accuracy training (${r.n} captures)`, date: nowStr(),
    model: { H: r.model.H, cx: r.model.cx, cy: r.model.cy } };
  for (const p of cfg.params) if (r.offsets[p.key] !== undefined) p.offset = r.offsets[p.key];
  saveCfg(); calStatus(); $("btn-apply").disabled = true;
  toast("Training applied – inspection now uses it", 4000);
};
$("btn-undo-train").onclick = () => {
  const b = localStorage.getItem("gl_cfg_before_training");
  if (!b) return toast("Nothing to undo");
  if (!confirm("Go back to the calibration / offsets from before the last training?")) return;
  const old = JSON.parse(b); cfg.cal = old.cal; cfg.params = old.params; saveCfg(); calStatus(); toast("Restored");
};
$("btn-persp-off").onclick = () => { if (confirm("Switch off 3D correction?")) { cfg.cal.model = null; saveCfg(); calStatus(); } };

// =====================================================================
// settings (password protected)
// =====================================================================
$("btn-unlock-set").onclick = () => {
  if ($("pw").value !== cfg.password) return toast("Wrong password");
  $("pw").value = ""; $("set-lock").classList.add("hidden"); $("set-body").classList.remove("hidden"); buildSettings();
};
function buildSettings() {
  $("par-table").tBodies[0].innerHTML = cfg.params.map((p, i) =>
    `<tr><td><input type="checkbox" data-i="${i}" data-f="enabled" ${p.enabled ? "checked" : ""}></td><td>${esc(p.name)}${p.assumed ? "*" : ""}</td>` +
    ["nominal", "tol_minus", "tol_plus", "offset"].map((f) => `<td><input type="number" step="0.001" data-i="${i}" data-f="${f}" value="${+(+p[f]).toFixed(4)}"></td>`).join("") + "</tr>").join("");
  const I = cfg.insp;
  $("s-delay").value = I.delay; $("s-motion").value = I.motion; $("s-minarea").value = I.minarea; $("s-avg").value = I.avg;
  $("s-thmode").value = I.thmode; $("s-thfix").value = I.thfix; $("s-save").value = I.save; $("s-beep").value = I.beep ? "1" : "0";
}
$("btn-save-set").onclick = () => {
  document.querySelectorAll("#par-table [data-f]").forEach((el) => {
    const p = cfg.params[+el.dataset.i], f = el.dataset.f;
    if (f === "enabled") p.enabled = el.checked; else { const v = parseFloat(el.value); if (isFinite(v)) p[f] = f.startsWith("tol") ? Math.abs(v) : v; }
  });
  const I = cfg.insp, num = (id, lo, hi, d) => { const v = parseFloat($(id).value); return isFinite(v) ? Math.max(lo, Math.min(hi, v)) : d; };
  I.delay = num("s-delay", 0.3, 10, 2); I.motion = num("s-motion", 0.1, 20, 1.5); I.minarea = num("s-minarea", 0.05, 50, 0.5);
  I.avg = Math.round(num("s-avg", 1, 8, 3)); I.thmode = $("s-thmode").value; I.thfix = num("s-thfix", 1, 254, 110);
  I.save = $("s-save").value; I.beep = $("s-beep").value === "1";
  saveCfg(); buildSettings(); showRows(last && last.rows); toast("Settings saved");
};
$("btn-defaults").onclick = () => {
  if (!confirm("Reset all nominals and tolerances to the drawing values? (offsets are kept)")) return;
  const off = {}; cfg.params.forEach((p) => (off[p.key] = p.offset));
  cfg.params = clone(DEFAULT_PARAMS); cfg.params.forEach((p) => (p.offset = off[p.key] || 0));
  saveCfg(); buildSettings(); toast("Drawing values restored");
};
$("btn-pw").onclick = () => { const v = $("pw-new").value; if (v.length < 3) return toast("At least 3 characters"); cfg.password = v; saveCfg(); $("pw-new").value = ""; toast("Password changed"); };
$("btn-backup").onclick = () => {
  download(new Blob([JSON.stringify({ app: "GL121123", version: APP_VERSION, cfg, caps: trainCaps }, null, 1)], { type: "application/json" }), `GL121123_backup_${stamp()}.json`);
  toast("Backup saved to Downloads");
};
$("file-restore").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try {
    const o = JSON.parse(await f.text()); if (o.app !== "GL121123" || !o.cfg) throw new Error("not a GL121123 backup");
    if (!confirm("Restore settings, calibration and training from this backup?")) return;
    localStorage.setItem("gl_cfg", JSON.stringify(o.cfg)); cfg = loadCfg();
    trainCaps = o.caps || []; saveCaps(); initUI(); toast("Backup restored");
  } catch (err) { toast("Restore failed: " + err.message, 4000); }
  e.target.value = "";
};

// =====================================================================
// tabs + setup buttons
// =====================================================================
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => {
  activeTab = b.dataset.tab;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + activeTab));
  if (activeTab === "history") showHistory();
  if (activeTab === "setup") calStatus();
  if (activeTab !== "settings") { $("set-lock").classList.remove("hidden"); $("set-body").classList.add("hidden"); }
  $("btn-cap").disabled = false;
  if (trig.state === "RESULT") { trig.state = "EMPTY"; }
}));
$("btn-cam-start").onclick = () => { cfg.cam.deviceId = $("cam-select").value || ""; cfg.cam.res = $("cam-res").value; saveCfg(); startCamera(); };
$("cam-select").onchange = $("btn-cam-start").onclick;
$("cam-res").onchange = $("btn-cam-start").onclick;
$("chk-demo").onchange = (e) => { cfg.cam.demo = e.target.checked; saveCfg(); updateDemoBtn(); startCamera(); };
$("btn-lock").onclick = () => lockCam(false);
$("btn-unlock").onclick = unlockCam;
function updateDemoBtn() {
  let b = $("btn-demo-next");
  if (cfg.cam.demo && !b) {
    b = document.createElement("button"); b.id = "btn-demo-next"; b.textContent = "Next part";
    b.onclick = demoNext; $("stage-bar").insertBefore(b, $("trig-info"));
  } else if (!cfg.cam.demo && b) b.remove();
}
function onSourceChanged() { trig.prev = null; trig.state = "EMPTY"; last = null; calStatus(); }
function updateUI() { bannerFromTrigger(); }
function initUI() {
  $("chk-auto").checked = cfg.insp.auto; $("chk-demo").checked = cfg.cam.demo; $("cam-res").value = cfg.cam.res;
  $("operator").value = cfg.insp.operator || ""; $("btn-zoom").classList.toggle("on", !!cfg.insp.zoom);
  buildTrueGrid(); capInfo(); updateCounters(); showRows(null); calStatus(); updateDemoBtn();
  $("about").innerHTML = `GL121123 STUD SHOCKER MTG · drawing rev 00 · tablet app ${APP_VERSION}<br>Engine: same as PC station v7 (Python + OpenCV, runs on this tablet, works offline).<br><a href="docs/GUIDE.html" target="_blank" style="color:#5aa2ff">Open the full guide</a>`;
}

// keep the screen on while inspecting
let wakeLock = null;
async function keepAwake() { try { if (navigator.wakeLock && document.visibilityState === "visible") wakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* ignore */ } }
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { keepAwake(); if (src.kind === "camera" && (!src.track || src.track.readyState === "ended")) startCamera(); }
});

// offline support
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  const hadSW = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register("sw.js").then((reg) => {
    reg.onupdatefound = () => {
      const w = reg.installing;
      w.onstatechange = () => {
        if (w.state === "activated") toast(hadSW ? "App updated – close and reopen to use the new version" : "App saved on this tablet – works offline now", 5000);
      };
    };
  }).catch(() => {});
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist();
}

// start
initUI();
startWorker();
startCamera();
keepAwake();
render();
