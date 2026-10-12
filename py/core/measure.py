"""Measurement engine for GL121123 bent stud (backlit silhouette).

Pipeline
  1. Threshold the inspection area -> part mask (largest dark blob).
  2. Skeleton of the mask -> rough centerline from thread end to bent end.
  3. Fit straight axis to leg 1 (thread side) and leg 2 (bent leg), refined
     with SUB-PIXEL edge points from grey-level profiles across the rod.
  4. Thread length / shoulder: found as sub-pixel edge crossings along lines
     parallel to the axis (between the two radii) - very precise.
  5. Diameters: median of sub-pixel widths across the rod.
  6. Angle between the two axes, bend start = axis intersection - R*tan(a/2),
     drop = distance from leg-1 axis to the theoretical outer corner of the
     bent end (chamfer ignored, same as drawing).

All results are in mm (angle in degrees). Image must be grey uint8.
"""
import math
import time
from collections import deque

import cv2
import numpy as np


class MeasureError(Exception):
    """Raised when the part cannot be measured (no part, wrong part ...)."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _skeletonize(mask):
    """Zhang-Suen thinning (vectorised numpy). Returns bool skeleton.

    Gives a connected, 1-pixel-wide centre line. Built in so no extra
    library (scikit-image / scipy) is needed.
    """
    img = np.pad((mask > 0).astype(np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            P = img
            p2, p3 = P[:-2, 1:-1], P[:-2, 2:]
            p4, p5 = P[1:-1, 2:], P[2:, 2:]
            p6, p7 = P[2:, 1:-1], P[2:, :-2]
            p8, p9 = P[1:-1, :-2], P[:-2, :-2]
            c = P[1:-1, 1:-1]
            nb = [p2, p3, p4, p5, p6, p7, p8, p9]
            B = sum(n.astype(np.int16) for n in nb)
            A = sum(((nb[i] == 0) & (nb[(i + 1) % 8] == 1)).astype(np.int16)
                    for i in range(8))
            if step == 0:
                m = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                m = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            rem = (c == 1) & (B >= 2) & (B <= 6) & (A == 1) & m
            if rem.any():
                changed = True
                img = img.copy()
                img[1:-1, 1:-1][rem] = 0
        if not changed:
            break
    return img[1:-1, 1:-1] > 0


def _longest_path(skel):
    ys, xs = np.nonzero(skel)
    n = len(xs)
    if n < 10:
        raise MeasureError("Part shape not found")
    idx = -np.ones(skel.shape, dtype=np.int64)
    idx[ys, xs] = np.arange(n)
    H, W = skel.shape
    nbrs = [[] for _ in range(n)]
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            y2, x2 = ys + dy, xs + dx
            ok = (y2 >= 0) & (y2 < H) & (x2 >= 0) & (x2 < W)
            j = np.full(n, -1, dtype=np.int64)
            j[ok] = idx[y2[ok], x2[ok]]
            for i in np.nonzero(j >= 0)[0]:
                nbrs[i].append(int(j[i]))

    def bfs(start):
        dist = np.full(n, -1, dtype=np.int64)
        prev = np.full(n, -1, dtype=np.int64)
        dist[start] = 0
        q = deque([start])
        while q:
            u = q.popleft()
            for v in nbrs[u]:
                if dist[v] < 0:
                    dist[v] = dist[u] + 1
                    prev[v] = u
                    q.append(v)
        return int(np.argmax(dist)), prev

    a, _ = bfs(0)
    b, prev = bfs(a)
    path = [b]
    while path[-1] != a and prev[path[-1]] >= 0:
        path.append(int(prev[path[-1]]))
    path = np.array(path)
    return np.stack([xs[path], ys[path]], axis=1).astype(np.float64)


def _fit_line(pts):
    pts = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
    if len(pts) < 3:
        raise MeasureError("Not enough edge points for axis fit")
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    d = np.array([vx, vy], dtype=np.float64)
    return np.array([x0, y0], dtype=np.float64), d / np.linalg.norm(d)


def _sample(gray_f, xs, ys):
    """Bilinear sampling of gray_f at float coordinates (same shape arrays)."""
    mx = np.ascontiguousarray(xs, dtype=np.float32)
    my = np.ascontiguousarray(ys, dtype=np.float32)
    if mx.ndim == 1:
        mx, my = mx[None, :], my[None, :]
    out = cv2.remap(gray_f, mx, my, interpolation=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE)
    return out


def _first_cross_up(v, T, start):
    """First sub-pixel index >= start where v rises to >= T (dark->bright)."""
    seg = v[start:]
    hit = np.nonzero(seg >= T)[0]
    if len(hit) == 0 or hit[0] == 0:
        return None
    i = start + hit[0]
    a, b = v[i - 1], v[i]
    return (i - 1) + (T - a) / (b - a) if b != a else float(i)


def _first_cross_down(v, T):
    """First sub-pixel index where v falls below T (bright->dark)."""
    hit = np.nonzero(v < T)[0]
    if len(hit) == 0 or hit[0] == 0:
        return None
    i = hit[0]
    a, b = v[i - 1], v[i]
    return (i - 1) + (a - T) / (a - b) if a != b else float(i)


def _normal(d):
    return np.array([-d[1], d[0]])


class Engine:
    def __init__(self, geometry):
        g = geometry
        self.thread_major = float(g.get("thread_major", 10.0))
        self.d12 = float(g.get("d12", 12.0))
        self.d14 = float(g.get("d14", 14.0))
        self.R = float(g.get("bend_radius", 14.0))
        self.leg2_fit = float(g.get("leg2_fit_mm", 25.0))
        # leg-1 axis is fitted on the smooth Ø12/Ø14 part only (thread helix
        # makes the thread silhouette asymmetric)
        self.fit_start = float(g.get("leg1_fit_start_mm", 20.0))

    # ----------------------------------------------------------------------
    def segment(self, gray, roi, threshold_mode="otsu", fixed_threshold=110,
                dark_part=True):
        """Return (roi_gray_float, mask, T_edge, (x0, y0)) of the ROI."""
        H, W = gray.shape[:2]
        if roi:
            x, y, w, h = [int(round(v)) for v in roi]
            x, y = max(0, x), max(0, y)
            w, h = min(w, W - x), min(h, H - y)
        else:
            x, y, w, h = 0, 0, W, H
        if w < 20 or h < 20:
            raise MeasureError("Inspection area too small")
        g = gray[y:y + h, x:x + w]
        if not dark_part:
            g = 255 - g
        if threshold_mode == "fixed":
            t = float(fixed_threshold)
        else:
            t, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        dark = g <= t
        if dark.sum() < 50 or (~dark).sum() < 50:
            raise MeasureError("No part in inspection area")
        mask = dark.astype(np.uint8) * 255
        # grey levels well inside part / background (edge pixels excluded)
        ker = np.ones((7, 7), np.uint8)
        inner = cv2.erode(mask, ker, iterations=2) > 0
        outer = cv2.dilate(mask, ker, iterations=2) == 0
        if inner.sum() < 50 or outer.sum() < 50:
            raise MeasureError("No part in inspection area")
        fg, bg = float(np.median(g[inner])), float(np.median(g[outer]))
        if bg - fg < 25:
            raise MeasureError("Contrast too low - check backlight / exposure")
        T_edge = 0.5 * (fg + bg)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n < 2:
            raise MeasureError("No part in inspection area")
        k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = (lab == k).astype(np.uint8) * 255
        # fill holes (reflections) inside the part
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        mask = np.zeros_like(mask)
        cv2.drawContours(mask, cnts, -1, 255, -1)
        return g.astype(np.float32), mask, T_edge, (x, y), cnts[0]

    # ----------------------------------------------------------------------
    def measure(self, gray, mm_per_px, roi=None, threshold_mode="otsu",
                fixed_threshold=110, dark_part=True, model=None):
        """Measure one image.

        mm_per_px  scale (mm per pixel at the height of the part's axis)
        model      optional 3D camera model from Accuracy Training:
                   {"H": camera height above part axis (mm),
                    "cx", "cy": optical centre (pixels)}.  Without it the
                   measurement is a plain 2D (telecentric) measurement.
        """
        t0 = time.perf_counter()
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        k = float(mm_per_px)                 # mm per pixel
        px = lambda mm: mm / k               # noqa: E731
        G, mask, T, (ox, oy), contour = self.segment(
            gray, roi, threshold_mode, fixed_threshold, dark_part)
        h, w = mask.shape

        # touching border?  (part partly outside the area / hand in view)
        b = 2
        if (mask[:b].any() or mask[-b:].any() or mask[:, :b].any()
                or mask[:, -b:].any()):
            raise MeasureError("Part touches edge of inspection area")

        area_mm2 = mask.sum() / 255.0 * k * k
        exp_area = (17.5 * 9.3 + 31.7 * self.d12 + (59 - 49.2 + 10 + 44) * self.d14)
        if not (0.4 * exp_area < area_mm2 < 3.0 * exp_area):
            raise MeasureError(
                f"Object size {area_mm2:.0f} mm² does not match part "
                f"(~{exp_area:.0f} mm²) - wrong part or wrong calibration")

        # ---- rough centerline from skeleton (on a reduced image) ----------
        # work on the part's bounding box, ~0.2 mm per pixel (rough only;
        # every dimension is refined later with sub-pixel edges)
        bx, by, bw_, bh_ = cv2.boundingRect(mask)
        pad = 4
        bx0, by0 = max(0, bx - pad), max(0, by - pad)
        crop = mask[by0:by + bh_ + pad, bx0:bx + bw_ + pad]
        ds = max(1.0, 0.2 / k)
        small = cv2.resize(crop, (max(8, int(crop.shape[1] / ds)),
                                  max(8, int(crop.shape[0] / ds))),
                           interpolation=cv2.INTER_AREA)
        small = (small > 127).astype(np.uint8)
        r_open = max(1, int(round(0.6 / (k * ds))))   # remove thread teeth
        small = cv2.morphologyEx(small, cv2.MORPH_OPEN, cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * r_open + 1, 2 * r_open + 1)))
        dt = cv2.distanceTransform(small, cv2.DIST_L2, 5)
        path = _longest_path(_skeletonize(small))
        seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
        s_path = np.concatenate([[0], np.cumsum(seg)]) * ds * k   # mm
        L = s_path[-1]
        if L < 50:
            raise MeasureError("Part too short / not recognised")

        def mean_r(sel):
            p = path[sel].astype(int)
            return float(dt[p[:, 1], p[:, 0]].mean()) * ds * k

        rA = mean_r((s_path > 3) & (s_path < 10))
        rB = mean_r((s_path > L - 10) & (s_path < L - 3))
        if rA > rB:   # make path start at thread end (thinner end)
            path = path[::-1]
            s_path = L - s_path[::-1]
        path_full = path * ds + np.array([bx0, by0])   # ROI pixel coordinates

        # ---- internal ruler: the part's own Ø14 -------------------------
        # Feature finding uses mm thresholds (e.g. "thread ends where the
        # width passes 11 mm"). If the scale calibration is wrong those trip
        # at the wrong place. So the detection scale is taken from the
        # bent leg itself (Ø14 by drawing); the OUTPUT still uses the
        # calibrated scale. scale_check reports how far calibration is off.
        # largest typical radius along the middle of the part = Ø14
        # (ends excluded: near an end face the distance map shrinks)
        mid = (s_path > 0.12 * L) & (s_path < 0.88 * L)
        pm = path[mid].astype(int)
        r_end = float(np.percentile(dt[pm[:, 1], pm[:, 0]], 90)) * ds * k \
            - 0.5 * ds * k                                # dt over-reads ~0.5 px
        f_det = (self.d14 / 2.0) / r_end if r_end > 0 else 1.0
        if not (0.55 < f_det < 1.8):
            raise MeasureError(f"Part size {100 / f_det:.0f}% of expected - wrong part "
                               "or scale calibration far off")
        k_out = k
        k = k * f_det                                     # detection scale
        s_path = s_path * f_det
        L = s_path[-1]

        # ---- leg 1 initial axis ------------------------------------------
        # skip the ends: the skeleton can branch into the corners there
        sel1 = (s_path > 6) & (s_path < 45)
        sel2 = (s_path > L - self.leg2_fit - 3) & (s_path < L - 9)
        if sel1.sum() < 5 or sel2.sum() < 5:
            raise MeasureError("Part shape not recognised")
        p1, d1 = _fit_line(path_full[sel1])
        if np.dot(path_full[sel1][-1] - path_full[sel1][0], d1) < 0:
            d1 = -d1
        p2, d2 = _fit_line(path_full[sel2])
        if np.dot(path_full[sel2][-1] - path_full[sel2][0], d2) < 0:
            d2 = -d2

        # ---- sub-pixel tools ---------------------------------------------
        hw = px(max(self.d14, 10) * 0.85)          # half profile length
        step = 0.25                                 # px
        nh = int(math.ceil(hw / step))
        tt = np.arange(-nh, nh + 1) * step         # symmetric, tt[ic] == 0
        ic = nh

        def profiles(origin, d, s_mm):
            """Widths and mid offsets across axis at positions s (mm)."""
            n = _normal(d)
            c = origin[None, :] + (np.asarray(s_mm)[:, None] / k) * d[None, :]
            X = c[:, 0:1] + tt[None, :] * n[0]
            Y = c[:, 1:2] + tt[None, :] * n[1]
            V = _sample(G, X, Y)
            widths = np.full(len(s_mm), np.nan)
            mids = np.full((len(s_mm), 2), np.nan)
            for i, v in enumerate(V):
                if v[ic] >= T:
                    continue
                r = _first_cross_up(v, T, ic)
                lft = _first_cross_up(v[::-1], T, len(v) - 1 - ic)
                if r is None or lft is None:
                    continue
                r_off = (r - ic) * step
                l_off = (lft - (len(v) - 1 - ic)) * step
                widths[i] = (r_off + l_off) * k
                mids[i] = c[i] + n * (r_off - l_off) / 2.0
            return widths, mids

        def end_point(origin, d, s_guess_mm, outward, face_r_mm=0.0):
            """Sub-pixel end face along axis. outward=+1 searches beyond +d.

            face_r_mm > 0: the flat end face is scanned on 7 parallel lines
            (within ±45 % of its radius) and the MEDIAN position is used ->
            much less noise from a soft / shaded / dirty edge."""
            span = 15.0
            ss = np.arange(0, px(span), 0.25)
            n = _normal(d)
            lats = np.linspace(-0.45, 0.45, 7) * face_r_mm if face_r_mm > 0 else [0.0]
            pts = origin + (s_guess_mm / k) * d
            start = pts + outward * px(span * 0.66) * d
            found = []
            for la in lats:
                st = start + (la / k) * n
                X = st[0] - outward * ss * d[0]
                Y = st[1] - outward * ss * d[1]
                v = _sample(G, X, Y)[0]
                c = _first_cross_down(v, T)
                if c is not None:
                    found.append(c)
            if len(found) < max(1, len(lats) // 2 + 1):
                raise MeasureError("Part end not found")
            c = float(np.median(found))
            return start - outward * c * 0.25 * d

        # leg 1: iterate tip + axis refinement
        s_tip0 = float(np.min((contour.reshape(-1, 2) - p1) @ d1)) * k
        try:
            tip = end_point(p1, d1, s_tip0 + 1.0, -1)
        except MeasureError:
            tip = end_point(p1, d1, np.dot(path_full[0] - p1, d1) * k, -1)
        bs_nom = 59.0
        for _ in range(2):
            s_fit = np.arange(self.fit_start, bs_nom - 4.0, 0.25)
            _, mids = profiles(tip, d1, s_fit)
            ok = ~np.isnan(mids[:, 0])
            if ok.sum() < 20:
                raise MeasureError("Leg 1 edges not found")
            p1, d1n = _fit_line(mids[ok])
            d1 = d1n if np.dot(d1n, d1) > 0 else -d1n
            # project tip onto refined axis and re-detect
            tip = p1 + np.dot(tip - p1, d1) * d1
            tip = end_point(tip, d1, 0.0, -1)

        # leg 2: far end + axis refinement
        cpts = contour.reshape(-1, 2).astype(np.float64)
        s_far0 = float(np.max((cpts - p2) @ d2)) * k            # outline extent
        try:
            far = end_point(p2, d2, s_far0 - 1.0, +1)
        except MeasureError:
            far = end_point(p2, d2, np.dot(path_full[-1] - p2, d2) * k, +1)
        for _ in range(2):
            u_fit = np.arange(2.0, self.leg2_fit, 0.25)
            _, mids = profiles(far, -d2, u_fit)
            ok = ~np.isnan(mids[:, 0])
            if ok.sum() < 10:
                raise MeasureError("Bent leg edges not found")
            p2, d2n = _fit_line(mids[ok])
            d2 = d2n if np.dot(d2n, d2) > 0 else -d2n
            far = p2 + np.dot(far - p2, d2) * d2
            far = end_point(far, d2, 0.0, +1, self.d14 / 2 - 1.0)

        # ---- angle / intersection / bend start ----------------------------
        cosang = float(np.clip(np.dot(d1, d2), -1, 1))
        angle = math.degrees(math.acos(cosang))
        if not (10 < angle < 80):
            raise MeasureError(f"Bend angle {angle:.1f}° - part not recognised")
        A = np.array([d1, -d2]).T
        a_b = np.linalg.solve(A, far - tip)
        P = tip + a_b[0] * d1
        s_P = a_b[0] * k
        bend_start = s_P - self.R * math.tan(math.radians(angle) / 2)

        # ---- width profile along leg 1 ------------------------------------
        s_w = np.arange(0.5, min(bend_start, s_P) - 1.0, 0.1)
        w1, _ = profiles(tip, d1, s_w)

        def coarse_step(level, s_from):
            sel = (s_w > s_from) & (w1 > level)
            idx = np.nonzero(sel)[0]
            for i in idx:   # need 0.5 mm of consecutive samples above level
                j = min(i + 5, len(w1))
                if np.all(w1[i:j] > level):
                    return float(s_w[i])
            return None

        lvl_thread = 0.5 * (self.thread_major + self.d12)
        lvl_shoulder = 0.5 * (self.d12 + self.d14)
        te_c = coarse_step(lvl_thread, 2.0)
        sh_c = coarse_step(lvl_shoulder, (te_c or 2.0) + 2.0)

        def axial_cross(radius_mm, s_c):
            """Sub-pixel position of a radial step along lines parallel to axis."""
            if s_c is None:
                return None, []
            n = _normal(d1)
            res, pts = [], []
            ss = np.arange(s_c - 3.0, s_c + 3.0, 0.25 * k)
            for side in (+1, -1):
                base = tip + side * (radius_mm / k) * n
                X = base[0] + (ss / k) * d1[0]
                Y = base[1] + (ss / k) * d1[1]
                v = _sample(G, X, Y)[0]
                c = _first_cross_down(v, T)
                if c is not None:
                    sv = ss[0] + c * 0.25 * k
                    res.append(sv)
                    pts.append(base + (sv / k) * d1)
            if not res:
                return None, []
            return float(np.mean(res)), pts

        thread_len, te_pts = axial_cross(lvl_thread / 2.0, te_c)
        shoulder, sh_pts = axial_cross(lvl_shoulder / 2.0, sh_c)

        # ---- width groups (pixels) ----------------------------------------
        off = np.array([ox, oy], dtype=np.float64)

        def group(sel, ws, base, d, s_arr, pct=None):
            sel = sel & ~np.isnan(ws)
            if sel.sum() <= 10:
                return None
            v = np.percentile(ws[sel], pct) if pct else np.median(ws[sel])
            pos = base + (float(np.mean(s_arr[sel])) / k) * d + off
            return [float(v) / k, pos.tolist(), int(sel.sum())]

        w12 = w_od = None
        w14 = []
        if thread_len is not None and shoulder is not None:
            w12 = group((s_w > thread_len + 1.5) & (s_w < shoulder - 1.5), w1, tip, d1, s_w)
        if thread_len is not None:
            w_od = group((s_w > 1.5) & (s_w < thread_len - 1.0), w1, tip, d1, s_w, 95)
        u_w = np.arange(2.0, self.leg2_fit, 0.25)
        w2, _ = profiles(far, -d2, u_w)
        g2 = group(np.ones(len(u_w), bool), w2, far, -d2, u_w)
        if g2:
            g2.append("leg2")
            w14.append(g2)
        if shoulder is not None:
            g1 = group((s_w > shoulder + 1.0) & (s_w < bend_start - 1.5), w1, tip, d1, s_w)
            if g1:
                g1.append("leg1")
                w14.append(g1)

        feats = {
            "tip": (tip + off).tolist(), "far": (far + off).tolist(),
            "d1": d1.tolist(), "d2": d2.tolist(),
            "te_pts": [(p + off).tolist() for p in te_pts],
            "sh_pts": [(p + off).tolist() for p in sh_pts],
            "lvl_thread": lvl_thread / 2.0, "lvl_shoulder": lvl_shoulder / 2.0,
            "w12": w12, "w14": w14, "w_od": w_od,
            "img_size": [int(gray.shape[1]), int(gray.shape[0])],
            "centroid": (np.mean(contour.reshape(-1, 2), axis=0) + off).tolist(),
        }
        mdl = dict(model or {})
        mdl["k"] = k_out
        raw = self.compute(feats, mdl)
        raw["scale_check"] = (raw["d14"] / self.d14) if raw.get("d14") else None
        k_det, k = k, k_out

        r14 = (raw["d14"] if raw.get("d14") else self.d14) / 2.0
        n2 = _normal(d2)
        n1 = _normal(d1)
        c_a, c_b = far + (r14 / k) * n2, far - (r14 / k) * n2
        del k_det
        corner = c_a if abs(np.dot(c_a - tip, n1)) > abs(np.dot(c_b - tip, n1)) else c_b
        geom = {
            "contour": (contour.reshape(-1, 2) + off.astype(int)),
            "tip": tip + off, "far": far + off, "P": P + off,
            "corner": corner + off,
            "bend_start_pt": tip + off + (raw["bend_start"] / k) * d1,
            "thread_pts": [p + off for p in te_pts],
            "shoulder_pts": [p + off for p in sh_pts],
            "d1": d1, "d2": d2,
        }
        return {"raw": raw, "geom": geom, "features": feats, "mm_per_px": k,
                "time_ms": (time.perf_counter() - t0) * 1000.0}

    # ----------------------------------------------------------------------
    def compute(self, f, model):
        """Turn pixel features into mm values.

        model: k (mm/px at axis height), optional H (camera height above the
        part axis, mm) + cx, cy (optical centre, px). With H the 3D shape of
        the round part is taken into account:
          * end faces / steps are seen from above at their top or bottom
            rim -> they appear further out the further they are from the
            optical centre (perspective) -> pulled back
          * diameters seen at an angle look wider -> exact tangent geometry
        """
        k = float(model["k"])
        H = model.get("H") or 0.0
        W_, H_ = f.get("img_size", [0, 0])
        c = np.array([model.get("cx", W_ / 2.0), model.get("cy", H_ / 2.0)], float)
        d1, d2 = np.array(f["d1"], float), np.array(f["d2"], float)

        def mm(p):
            return (np.asarray(p, float) - c) * k

        PHI = np.linspace(0, 2 * math.pi, 241)
        CPH, SPH = np.cos(PHI), np.sin(PHI)

        def fix_end(X, e, cands, w=0.0):
            """Exact perspective correction of an end face / step seen on a
            scan line (direction e, lateral offset w from the axis) through
            the measured edge point X (axis-plane mm, origin = optical centre).

            cands: [(axial setback, rim radius)] round discs forming the end
            (face + chamfer). A rim point at angle phi is seen magnified by
            sig = H/(H - rho sin phi). Where the scan line crosses a projected
            disc does not depend on the face position, so the outermost
            crossing is linear in it -> closed-form solution:
                face shift s = min_j (xe / sig_j - xe - setback_j)
            """
            if not H:
                return X
            n = np.array([-e[1], e[0]])
            xe, xn = float(np.dot(X, e)), float(np.dot(X, n))
            best = None
            for dl, rho in cands:
                sig = H / (H - rho * SPH)
                g = sig * (xn - w + rho * CPH) - xn
                cr = np.nonzero(g[:-1] * g[1:] <= 0)[0]
                for i in cr:
                    if g[i] == g[i + 1]:
                        continue
                    fr = g[i] / (g[i] - g[i + 1])
                    sg = sig[i] + fr * (sig[i + 1] - sig[i])
                    sv = xe / sg - xe - dl
                    best = sv if best is None else min(best, sv)
            if best is None:                     # scan line misses model
                return X
            return X + best * e

        def true_dia(w_px, pos, d):
            W = w_px * k
            if not H:
                return W
            n = np.array([-d[1], d[0]])
            L = abs(float(np.dot(mm(pos), n)))

            def width(r):
                u = tangents(r, L)
                return abs(u[1] - u[0])
            lo, hi = 0.2 * W, 0.6 * W
            for _ in range(50):
                mid = 0.5 * (lo + hi)
                if width(mid) < W:
                    lo = mid
                else:
                    hi = mid
            return lo + hi            # = 2 r

        def tangents(r, L):
            """Apparent edge positions (relative to the true axis) of a round
            rod of radius r whose axis is at lateral distance -L from the
            optical centre (exact tangent rays, projected to axis plane)."""
            dd = math.hypot(L, H)
            al = math.asin(min(0.999, r / dd))
            ph = math.atan2(-H, -L)
            return [L - H * math.cos(t) / math.sin(t) for t in (ph - al, ph + al)]

        rM = self.thread_major / 2.0
        r12, r14n = self.d12 / 2.0, self.d14 / 2.0
        def fix_axis(X, d, r):
            """Silhouette centre of a rod seen at an angle lies slightly
            further out than its axis -> move X sideways onto the true axis."""
            n = np.array([-d[1], d[0]])
            l_app = float(np.dot(X, n))
            l = l_app
            for _ in range(4):
                u = tangents(r, -l)
                l = l_app - 0.5 * (u[0] + u[1])
            return X + (l - l_app) * n

        if H:
            # leg-1 axis was fitted on Ø12 (s 20-49) + Ø14 (49-55): use one
            # effective radius for both points so no artificial rotation
            r_eff1 = math.sqrt((29.2 * (self.d12 / 2) ** 2 + 5.8 * (self.d14 / 2) ** 2) / 35.0)
            t0 = mm(f["tip"])
            a1 = fix_axis(t0 + 50.0 * d1, d1, r_eff1)
            b1 = fix_axis(t0 + 25.0 * d1, d1, r_eff1)
            dn1 = (a1 - b1) / np.linalg.norm(a1 - b1)
            f0 = mm(f["far"])
            a2 = fix_axis(f0, d2, self.d14 / 2)
            b2 = fix_axis(f0 - 20.0 * d2, d2, self.d14 / 2)
            dn2 = (a2 - b2) / np.linalg.norm(a2 - b2)
            tip_ax = b1 + float(np.dot(t0 - b1, dn1)) * dn1
            far_ax = b2 + float(np.dot(f0 - b2, dn2)) * dn2
            d1, d2 = dn1, dn2
        else:
            tip_ax, far_ax = mm(f["tip"]), mm(f["far"])
        ch = [(-0.25 * i, None) for i in range(5)]          # 1x45 chamfers
        tip = fix_end(tip_ax, -d1, [(dl, rM - 1.0 - dl) for dl, _ in ch])
        far = fix_end(far_ax, d2, [(dl, r14n - 1.0 - dl) for dl, _ in ch])

        tip0 = tip_ax
        n1_ = np.array([-d1[1], d1[0]])

        def step(pts, r_out):
            if not pts:
                return None
            vals = []
            for p in pts:
                X = mm(p)
                w = float(np.dot(X - tip0, n1_))          # lateral offset of scan line
                Xc = fix_end(X, -d1, [(0.0, r_out)], w=-w)
                vals.append(float(np.dot(Xc - tip, d1)))
            return float(np.mean(vals))

        thread_len = step(f["te_pts"], r12)
        shoulder = step(f["sh_pts"], r14n)

        cosang = float(np.clip(np.dot(d1, d2), -1, 1))
        angle = math.degrees(math.acos(cosang))
        A = np.array([d1, -d2]).T
        a_b = np.linalg.solve(A, far - tip)
        s_P = float(a_b[0])
        bend_start = s_P - self.R * math.tan(math.radians(angle) / 2)

        d12 = true_dia(f["w12"][0], f["w12"][1], d1) if f.get("w12") else None
        thread_od = true_dia(f["w_od"][0], f["w_od"][1], d1) if f.get("w_od") else None
        d14 = None
        if f.get("w14"):
            vals, wts = [], []
            for g_ in f["w14"]:
                dd = d2 if g_[3] == "leg2" else d1
                vals.append(true_dia(g_[0], g_[1], dd))
                wts.append(g_[2])
            d14 = float(np.average(vals, weights=wts))
        r14 = (d14 if d14 else self.d14) / 2.0
        n1, n2 = np.array([-d1[1], d1[0]]), np.array([-d2[1], d2[0]])
        drop = max(abs(float(np.dot(far + r14 * n2 - tip, n1))),
                   abs(float(np.dot(far - r14 * n2 - tip, n1))))
        axial_len = float(np.dot(far - tip, d1))
        return {"thread_len": thread_len, "shoulder": shoulder,
                "bend_start": bend_start, "d12": d12, "d14": d14,
                "angle": angle, "drop": drop, "thread_od": thread_od,
                "axial_len": axial_len}


# --------------------------------------------------------------------------
def evaluate(raw, params):
    """Apply offsets and limits. Returns (overall_ok, rows)."""
    rows, ok_all = [], True
    for p in params:
        if not p.get("enabled", True):
            continue
        lo = p["nominal"] - abs(p["tol_minus"])
        hi = p["nominal"] + abs(p["tol_plus"])
        v = raw.get(p["key"])
        if v is None:
            rows.append({"key": p["key"], "name": p["name"], "value": None,
                         "lo": lo, "hi": hi, "nominal": p["nominal"],
                         "ok": False, "dev": None, "used_pct": None,
                         "note": "NOT FOUND", "unit": p["unit"]})
            ok_all = False
            continue
        v = v + float(p.get("offset", 0.0))
        ok = lo - 1e-9 <= v <= hi + 1e-9
        band = hi - lo
        used = (v - lo) / band * 100.0 if band > 0 else None
        rows.append({"key": p["key"], "name": p["name"], "value": v, "lo": lo,
                     "hi": hi, "nominal": p["nominal"], "ok": ok,
                     "dev": v - p["nominal"], "used_pct": used,
                     "note": "" if ok else ("LOW" if v < lo else "HIGH"),
                     "unit": p["unit"]})
        ok_all &= ok
    return ok_all, rows
