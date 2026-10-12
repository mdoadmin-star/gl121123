"""Accuracy training: learn the station's 3D camera model from a master part.

The operator places ONE master part (true dimensions known from CMM /
micrometer) 10-20 times at different positions and angles. Every capture
gives pixel features. From all captures together we fit:

    k       mm per pixel at the height of the part axis
    H       height of the lens (projection centre) above the part axis
    cx, cy  optical centre in the image (pixels)
    + constant offsets for the two diameters (edge/lighting bias)

so that the corrected values agree with the true values at EVERY position.
Then per-parameter offsets remove what is left on average.

Honest accuracy: every capture is predicted by a model fitted WITHOUT that
capture (leave-one-out), so the reported accuracy is what the station will
really do on a new placement.

No scipy needed - small Levenberg-Marquardt in numpy.
"""
import math
from datetime import datetime

import numpy as np

DIA_KEYS = ("d12", "d14", "thread_od")
H_INV_MAX = 1000.0 / 100.0      # fitted lens height above part axis >= 100 mm
ANGLE_SCATTER_MAX = 0.15        # deg - angle does not depend on scale/perspective
DEFAULT_FIT_KEYS = ["thread_len", "shoulder", "bend_start", "d12", "d14", "angle", "drop"]


def band_of(p):
    return max(abs(p["tol_minus"]) + abs(p["tol_plus"]), 0.02)


class Trainer:
    def __init__(self, engine, captures, true, params, k0, img_size, fit_keys=None,
                 cx0=None, cy0=None, fit_scale=True):
        """captures: list of feature dicts (Engine.measure()['features'])
        true: {key: true value of master}   params: config parameter list
        k0: current mm/px     img_size: (W, H) pixels"""
        self.eng = engine
        self.caps = captures
        self.params = {p["key"]: p for p in params}
        keys = fit_keys or DEFAULT_FIT_KEYS
        self.keys = [k for k in keys if k in true and true[k] is not None]
        self.true = true
        self.k0 = float(k0)
        self.W, self.H = img_size
        self.cx0 = self.W / 2.0 if cx0 is None else float(cx0)
        self.cy0 = self.H / 2.0 if cy0 is None else float(cy0)
        self.fit_scale = fit_scale
        self.dkeys = [k for k in self.keys if k in DIA_KEYS]
        self.tol = np.array([band_of(self.params[k]) if k in self.params else 0.1
                             for k in self.keys])
        self.tvec = np.array([true[k] for k in self.keys], float)

    # -- parameter vector: [scale ppm*1e3, 1000/H, dcx (W/10), dcy (W/10), dia offsets]
    def model(self, q):
        invh = max(q[1], 0.0)
        return {"k": self.k0 * (1.0 + q[0] * 1e-3),
                "H": (1000.0 / invh) if invh > 1e-4 else 0.0,
                "cx": self.cx0 + q[2] * self.W / 10.0,
                "cy": self.cy0 + q[3] * self.W / 10.0}

    def values(self, q, idx=None):
        m = self.model(q)
        idx = range(len(self.caps)) if idx is None else idx
        out = []
        for i in idx:
            r = self.eng.compute(self.caps[i], m)
            row = []
            for k in self.keys:
                v = r.get(k)
                if v is not None and k in self.dkeys:
                    v = v + q[4 + self.dkeys.index(k)]
                row.append(np.nan if v is None else v)
            out.append(row)
        return np.array(out, float)

    def residuals(self, q, idx):
        V = self.values(q, idx)
        R = (V - self.tvec[None, :]) / self.tol[None, :]
        R = np.nan_to_num(R, nan=0.0).ravel()
        prior = [q[0] / (20.0 if self.fit_scale else 0.01),   # scale: weak prior
                 q[2] / 1.5, q[3] / 1.5]                        # centre near middle
        return np.concatenate([R, prior])

    def lm(self, q, idx, iters=25):
        q = np.array(q, float)
        lam = 1e-2
        r = self.residuals(q, idx)
        cost = float(r @ r)
        steps = np.array([1e-2, 1e-3, 1e-3, 1e-3] + [1e-4] * len(self.dkeys))
        for _ in range(iters):
            J = np.empty((len(r), len(q)))
            for j in range(len(q)):
                dq = q.copy()
                dq[j] += steps[j]
                J[:, j] = (self.residuals(dq, idx) - r) / steps[j]
            A = J.T @ J
            g = J.T @ r
            improved = False
            for _ in range(8):
                try:
                    delta = -np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), g)
                except np.linalg.LinAlgError:
                    lam *= 10
                    continue
                qn = q + delta
                qn[1] = min(max(qn[1], 0.0), H_INV_MAX)       # camera >= ~110 mm
                rn = self.residuals(qn, idx)
                cn = float(rn @ rn)
                if cn < cost:
                    q, r, cost = qn, rn, cn
                    lam = max(lam / 3, 1e-7)
                    improved = True
                    break
                lam *= 5
            if not improved or np.abs(delta).max() < 1e-6:
                break
        return q, cost

    def fit(self, idx=None, starts=(1000 / 150, 1000 / 400, 0.3, 0.0), q_init=None):
        idx = list(range(len(self.caps))) if idx is None else list(idx)
        nq = 4 + len(self.dkeys)
        best = None
        inits = [q_init] if q_init is not None else [
            np.array([0, s, 0, 0] + [0] * len(self.dkeys), float) for s in starts]
        for q0 in inits:
            q, c = self.lm(q0, idx, iters=25 if q_init is None else 8)
            if best is None or c < best[1]:
                best = (q, c)
        return best[0] if best else np.zeros(nq)


def analyse(engine, captures, true, params, k0, img_size, current_model=None,
            fit_keys=None, cx0=None, cy0=None, progress=None):
    """Full training analysis. Returns a dict with model, stats, mistakes."""
    caps = [c for c in captures if c.get("features")]
    feats = [c["features"] for c in caps]
    n = len(feats)
    res = {"n": n, "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "mistakes": []}
    if n < 3:
        res["mistakes"].append(("error", "Need at least 3 good captures (10-20 recommended)."))
        return res
    pre = []                     # problems found before fitting
    plain = [engine.compute(f, {"k": k0}) for f in feats]
    use_keys = [k for k in (fit_keys or DEFAULT_FIT_KEYS) if k in true]
    names = {p["key"]: p["name"] for p in params}
    for k in list(use_keys):
        miss = sum(1 for r in plain if r.get(k) is None)
        if miss > 0.3 * n:
            use_keys.remove(k)
            pre.append(("error", f"{names.get(k, k)} was NOT FOUND in {miss} of {n} captures – "
                                 "the software cannot see this feature reliably (double-click a "
                                 "capture to see what was detected)."))
    # angle is independent of scale and perspective -> pure image quality test
    ang = np.array([r["angle"] for r in plain if r.get("angle") is not None])
    quality_bad = False
    if len(ang) >= 3:
        sc = 1.4826 * np.median(np.abs(ang - np.median(ang)))
        if sc > ANGLE_SCATTER_MAX:
            quality_bad = True
            pre.append(("error", f"Images are not stable: the bend angle of the SAME part scatters "
                                 f"±{3 * sc:.2f}° between captures (should be < ±0.3°). Check: part "
                                 "lies flat and still, no over-exposure, focus, clean backlight, nothing "
                                 "else in the area. Fix this first, then train again."))
    # scale: start from the master itself (checkerboard may be off)
    ratios = []
    for r in plain:
        for k in ("d14", "d12", "shoulder", "drop", "thread_len"):
            if k in true and r.get(k):
                ratios.append(true[k] / r[k])
    s_ratio = float(np.median(ratios)) if ratios else 1.0
    if abs(s_ratio - 1) > 0.03:
        pre.append(("warn", f"Scale calibration is {100 * (1 / s_ratio - 1):+.1f}% off (camera moved "
                            "after calibration, or wrong checkerboard square size?). Training corrects "
                            "it from the master - Apply saves the corrected scale."))
    k_init = k0 * s_ratio
    tr = Trainer(engine, feats, true, params, k_init, img_size, use_keys, cx0, cy0)
    if not tr.keys:
        res["mistakes"] += pre + [("error", "Enter the master part's true values first.")]
        return res
    keys = tr.keys

    # ---- before: what the station measures now ---------------------------
    cur = dict(current_model or {"k": k0})
    offs = {p["key"]: p.get("offset", 0.0) for p in params}
    before = np.array([[(engine.compute(f, cur).get(k) or np.nan) + offs.get(k, 0.0)
                        for k in keys] for f in feats])

    # ---- robust full fit (remove outlier captures, refit) ----------------
    if progress:
        progress("Fitting camera model ...")
    use = list(range(n))
    q = tr.fit(use)
    # robust: drop the worst capture while it clearly does not fit the rest
    # (wrong part, dirt, part not lying flat, hand in view ...)
    for _ in range(max(1, n // 4)):
        V = tr.values(q)
        E = (V - tr.tvec[None, :])
        med = np.nanmedian(E[use], axis=0)
        mad = 1.4826 * np.nanmedian(np.abs(E[use] - med), axis=0)
        floor = np.maximum(mad, 0.02 * tr.tol)              # avoid div by ~0
        Z = np.abs(E - med) / floor
        big = np.abs(E - med) > 0.1 * tr.tol                 # matters vs tolerance
        score = np.where(big, Z, 0)
        cand = [(np.nanmax(score[i]), i) for i in use]
        worst_z, worst = max(cand)
        if worst_z < 5 or len(use) - 1 < max(3, (2 * n) // 3):
            break
        use = [i for i in use if i != worst]
        q = tr.fit(use, q_init=q)
        q = tr.fit(use) if len(use) >= 6 else q            # fresh multi-start
    excluded = [i for i in range(n) if i not in use]
    model = tr.model(q)
    V = tr.values(q)
    new_off = {}
    for j, k in enumerate(keys):
        e = V[use, j] - tr.tvec[j]
        extra = q[4 + tr.dkeys.index(k)] if k in tr.dkeys else 0.0
        new_off[k] = float(extra - np.nanmean(e))       # offset to add to compute()

    # ---- leave-one-out (honest accuracy) ----------------------------------
    loo = np.full((n, len(keys)), np.nan)
    for i in range(n):
        if progress:
            progress(f"Checking accuracy {i + 1}/{n} ...")
        others = [j for j in use if j != i]
        if len(others) < 3:
            continue
        qi = tr.fit(others, q_init=q)
        Vi = tr.values(qi)
        for j, k in enumerate(keys):
            bias = np.nanmean(Vi[others, j] - tr.tvec[j])
            loo[i, j] = Vi[i, j] - bias - tr.tvec[j]

    # ---- statistics --------------------------------------------------------
    stats = []
    for j, k in enumerate(keys):
        p = tr.params.get(k, {"name": k, "unit": "mm", "tol_minus": 0.05, "tol_plus": 0.05})
        band = band_of(p)
        eb = before[:, j] - tr.tvec[j]
        ea = loo[use, j]
        sd_a = float(np.nanstd(ea, ddof=1)) if np.sum(~np.isnan(ea)) > 1 else float("nan")
        pct = 6 * sd_a / band * 100 if band > 0 else float("nan")
        verdict = ("GOOD" if pct < 10 else "ACCEPTABLE" if pct < 30 else "NOT CAPABLE") \
            if math.isfinite(pct) else "-"
        stats.append({
            "key": k, "name": p["name"], "unit": p.get("unit", "mm"), "true": tr.tvec[j],
            "band": band,
            "before_bias": float(np.nanmean(eb)), "before_sd": float(np.nanstd(eb, ddof=1)),
            "before_max": float(np.nanmax(np.abs(eb))),
            "after_bias": float(np.nanmean(ea)), "after_sd": sd_a,
            "after_max": float(np.nanmax(np.abs(ea))), "acc3s": 3 * sd_a,
            "pct_tol": pct, "verdict": verdict, "offset": new_off[k]})

    # ---- per-capture table ------------------------------------------------
    rows = []
    for i, c in enumerate(caps):
        after = V[i] + np.array([new_off[k] - (q[4 + tr.dkeys.index(k)] if k in tr.dkeys else 0)
                                 for k in keys])
        rows.append({"i": i, "centroid": c["features"].get("centroid"),
                     "dir": c["features"].get("d1"), "excluded": i in excluded,
                     "before": dict(zip(keys, before[i])), "after": dict(zip(keys, after)),
                     "loo": dict(zip(keys, loo[i])),
                     "worst_before": float(np.nanmax(np.abs(before[i] - tr.tvec) / tr.tol)),
                     "worst_after": float(np.nanmax(np.abs(loo[i] - 0) / tr.tol))
                     if not np.all(np.isnan(loo[i])) else float("nan")})

    res.update({"keys": keys, "model": model, "q": q.tolist(), "offsets": new_off,
                "stats": stats, "rows": rows, "excluded": excluded,
                "dia_bias": {k: q[4 + tr.dkeys.index(k)] for k in tr.dkeys}})
    bad_keys = [st["name"] for st in stats if math.isfinite(st["pct_tol"]) and st["pct_tol"] > 100]
    at_limit = q[1] >= H_INV_MAX * 0.98
    valid = not quality_bad and len(bad_keys) < 2 and not at_limit
    res["valid"] = valid
    res["scale_ratio"] = s_ratio
    head = []
    if not valid:
        why = []
        if quality_bad:
            why.append("images not stable")
        if len(bad_keys) >= 2:
            why.append("errors larger than the tolerance for " + ", ".join(bad_keys))
        if at_limit:
            why.append("captures do not fit any real camera geometry")
        head.append(("error", "RESULT NOT VALID – do NOT apply (" + "; ".join(why) + "). "
                              "Fix the problems below, then capture again."))
    res["mistakes"] = head + pre + find_mistakes(res, caps, tr, img_size, at_limit)
    return res


def find_mistakes(res, caps, tr, img_size, at_limit=False):
    """Plain-language problems found in the training data."""
    out = []
    n = len(caps)
    W, H = img_size
    if n < 8:
        out.append(("warn", f"Only {n} captures - use 10-20 for a reliable result."))
    cen = np.array([c["features"]["centroid"] for c in caps], float)
    span = np.ptp(cen, axis=0) / np.array([W, H])
    if span.max() < 0.15:
        out.append(("warn", "All captures are almost at the same place. Put the master also near "
                            "the corners / edges of the inspection area, otherwise the camera "
                            "height cannot be learned."))
    ang = np.degrees(np.arctan2([c["features"]["d1"][1] for c in caps],
                                [c["features"]["d1"][0] for c in caps]))
    if np.ptp(np.unwrap(np.radians(ang))) < math.radians(30) and n >= 5:
        out.append(("info", "The part was never rotated. Also capture it turned ~90° and ~180°."))
    for i in res["excluded"]:
        r = res["rows"][i]

        def z(k):
            v = r["loo"][k]
            return abs(v) / band_of(tr.params.get(k, {"tol_minus": .05, "tol_plus": .05})) \
                if np.isfinite(v) else -1
        worst = max(r["loo"], key=z)
        out.append(("warn", f"Capture #{i + 1} ignored: {tr.params.get(worst, {}).get('name', worst)} "
                            f"was {r['loo'][worst]:+.3f} off compared with all other captures "
                            f"(dirt / burr / part not lying flat / hand in view?)."))
    if "angle" in tr.keys:

        for i, r in enumerate(res["rows"]):
            v = r["loo"].get("angle")
            if v is not None and np.isfinite(v) and abs(v) > 0.4 and i not in res["excluded"]:
                out.append(("warn", f"Capture #{i + 1}: bend angle off by {v:+.2f}° - part was "
                                    "probably rocking / not lying flat."))

    m = res["model"]
    if at_limit:
        out.append(("warn", "The fitted camera height hit its lower limit - the captures are "
                            "inconsistent (usually wrong detections or a part not lying flat)."))
    elif m["H"] and m["H"] < 180:
        out.append(("info", f"Camera is close to the part (≈{m['H'] + 7:.0f} mm). 3D correction is "
                            "active; a longer lens (35-50 mm) would make the station even more robust."))
    if not m["H"]:
        out.append(("info", "No measurable perspective - camera behaves like a telecentric lens."))
    dc = math.hypot(m["cx"] - W / 2, m["cy"] - H / 2) / W
    if dc > 0.15:
        out.append(("warn", "Optical centre found far from the image centre - the camera is "
                            "probably tilted. Level the camera (spirit level) and train again."))
    for s in res["stats"]:
        if s["verdict"] == "NOT CAPABLE":
            out.append(("warn", f"{s['name']}: repeatability ±{s['acc3s']:.3f} uses "
                                f"{s['pct_tol']:.0f}% of the tolerance - not reliable with this "
                                "setup. Keep a manual gauge for it or improve lens/lighting."))
    if abs(res["model"]["k"] / tr.k0 - 1) > 0.01:
        out.append(("info", f"Scale fine-corrected by {100 * (res['model']['k'] / tr.k0 - 1):+.2f}% "
                            "(part axis is 7 mm above the table)."))
    if not out:
        out.append(("ok", "No problems found."))
    return out


def export_excel(res, path, part_name=""):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)
    m = res["model"]
    ws.append(["Accuracy training", part_name, res["date"]])
    ws.append(["Captures", res["n"], "ignored", len(res["excluded"])])
    ws.append(["Scale mm/px", round(m["k"], 7), "Camera height above axis (mm)",
               round(m["H"], 1) if m["H"] else "telecentric"])
    ws.append(["Optical centre px", round(m["cx"], 1), round(m["cy"], 1)])
    ws.append([])
    head = ["Parameter", "True", "Before: bias", "Before: std", "Before: max err",
            "After: bias", "After: std", "After: max err", "Accuracy ±3σ", "% of tolerance",
            "Verdict", "New offset"]
    ws.append(head)
    for c in ws[ws.max_row]:
        c.font = bold
    col = {"GOOD": "C6EFCE", "ACCEPTABLE": "FFEB9C", "NOT CAPABLE": "FFC7CE"}
    for s in res["stats"]:
        ws.append([s["name"], s["true"], round(s["before_bias"], 4), round(s["before_sd"], 4),
                   round(s["before_max"], 4), round(s["after_bias"], 4), round(s["after_sd"], 4),
                   round(s["after_max"], 4), round(s["acc3s"], 4), round(s["pct_tol"], 1),
                   s["verdict"], round(s["offset"], 4)])
        if s["verdict"] in col:
            ws.cell(ws.max_row, 11).fill = PatternFill("solid", fgColor=col[s["verdict"]])
    ws.append([])
    ws.append(["Findings"])
    ws[ws.max_row][0].font = bold
    for lvl, txt in res["mistakes"]:
        ws.append([lvl.upper(), txt])
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 14
    w2 = wb.create_sheet("Captures")
    keys = res["keys"]
    w2.append(["#", "x px", "y px", "ignored"] + [f"{k} before" for k in keys] +
              [f"{k} after" for k in keys] + [f"{k} error (LOO)" for k in keys])
    for r in res["rows"]:
        c = r["centroid"] or [None, None]
        w2.append([r["i"] + 1, round(c[0]), round(c[1]), "yes" if r["excluded"] else ""] +
                  [round(r["before"][k], 4) if np.isfinite(r["before"][k]) else None for k in keys] +
                  [round(r["after"][k], 4) if np.isfinite(r["after"][k]) else None for k in keys] +
                  [round(r["loo"][k], 4) if np.isfinite(r["loo"][k]) else None for k in keys])
    wb.save(path)
    return path
