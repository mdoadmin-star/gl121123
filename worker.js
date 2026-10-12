/* Background worker: runs Python (Pyodide) + the measuring engine so the
   screen never freezes while a part is measured (3-6 s on a tablet). */
importScripts("runtime/pyodide.js");

let py = null, xlsxLoaded = false;
// note: no "__init__.py" download - GitHub Pages hides files starting with "_"
const PY_FILES = ["core/measure.py", "core/training.py",
  "core/trigger.py", "core/simpart.py", "core/params.py", "bridge.py"];

async function init() {
  postMessage({ type: "status", text: "Starting Python …" });
  py = await loadPyodide({ indexURL: "runtime/" });
  postMessage({ type: "status", text: "Loading OpenCV …" });
  await py.loadPackage(["numpy", "opencv-python"]);
  py.FS.mkdirTree("/app/core");
  py.FS.writeFile("/app/core/__init__.py", "");
  for (const f of PY_FILES) {
    const r = await fetch("py/" + f);
    if (!r.ok) throw new Error("File py/" + f + " missing on the web site (HTTP " + r.status + ") - upload the complete folder again");
    py.FS.writeFile("/app/" + f, await r.text());
  }
  py.runPython("import sys; sys.path.insert(0, '/app'); import bridge");
  postMessage({ type: "ready" });
}

const ready = init().catch(e => postMessage({ type: "fatal", text: String(e) }));

function call(fn, ...args) {
  const f = py.globals.get("bridge")[fn];
  const res = f(...args);
  f.destroy && f.destroy();
  return res;
}

onmessage = async (ev) => {
  await ready;
  const m = ev.data;
  try {
    let out;
    if (m.cmd === "measure") {
      out = JSON.parse(call("measure", new Uint8Array(m.buf), m.w, m.h, m.ch, JSON.stringify(m.args)));
    } else if (m.cmd === "stats") {
      out = JSON.parse(call("stats_image", new Uint8Array(m.buf), m.w, m.h, m.ch));
    } else if (m.cmd === "checkerboard") {
      out = JSON.parse(call("checkerboard", new Uint8Array(m.buf), m.w, m.h, m.ch, m.cols, m.rows, m.square));
    } else if (m.cmd === "scale_from_master") {
      out = JSON.parse(call("scale_from_master", JSON.stringify(m.features), JSON.stringify(m.true), m.k));
    } else if (m.cmd === "train") {
      const prog = (t) => postMessage({ type: "progress", id: m.id, text: String(t) });
      out = JSON.parse(call("train", JSON.stringify(m.caps), JSON.stringify(m.true),
        JSON.stringify(m.params), m.k, m.w, m.h, m.model ? JSON.stringify(m.model) : "", prog));
    } else if (m.cmd === "excel") {
      if (!xlsxLoaded) {
        await py.loadPackage(["et_xmlfile-2.0.0-py3-none-any.whl", "openpyxl-3.1.5-py2.py3-none-any.whl"]
          .map((f) => new URL("runtime/" + f, self.location.href).href));
        xlsxLoaded = true;
      }
      const b = call("excel", JSON.stringify(m.rows), JSON.stringify(m.params), m.title);
      out = { bytes: b.toJs() }; b.destroy();
    } else if (m.cmd === "demo") {
      const r = call("demo_image", m.seed, m.master, m.cam_h, m.k0, m.w, m.h);
      const arr = r.get(0).toJs(); const truth = JSON.parse(r.get(1)); r.destroy();
      out = { buf: arr.buffer, w: m.w, h: m.h, truth };
    }
    postMessage({ type: "result", id: m.id, out }, out && out.buf ? [out.buf] : []);
  } catch (e) {
    postMessage({ type: "result", id: m.id, error: String(e) });
  }
};
