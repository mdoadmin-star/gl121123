"""Synthetic backlit images of the GL121123 stud with exactly known dimensions.

Used by the built-in simulator camera (to try the software without the real
camera) and by selftest.py (to prove the measuring accuracy of the algorithm).
"""
import math
import random

import cv2
import numpy as np

NOMINAL = {"thread_len": 17.5, "shoulder": 49.2, "bend_start": 59.0,
           "d12": 12.0, "d14": 14.0, "angle": 41.5, "drop": 37.7,
           "thread_major": 9.9, "thread_minor": 8.65, "pitch": 1.25,
           "R": 14.0}


def leg2_for_drop(g):
    th = math.radians(g["angle"])
    R, r = g["R"], g["d14"] / 2
    return (g["drop"] - R * (1 - math.cos(th)) - r * math.cos(th)) / math.sin(th)


def outline_mm(g):
    """Polygon (Nx2, mm) of the silhouette. Thread tip at (0,0), axis +x,
    bend toward +y. Also returns the true axial length."""
    th = math.radians(g["angle"])
    R = g["R"]
    L2 = leg2_for_drop(g)
    bs = g["bend_start"]
    arc = R * th
    Ls = bs + arc + L2
    rM, rm, pitch = g["thread_major"] / 2, g["thread_minor"] / 2, g["pitch"]
    r12, r14 = g["d12"] / 2, g["d14"] / 2

    def center(s):
        if s <= bs:
            return np.array([s, 0.0]), np.array([1.0, 0.0])
        if s <= bs + arc:
            a = (s - bs) / R
            return (np.array([bs + R * math.sin(a), R - R * math.cos(a)]),
                    np.array([math.cos(a), math.sin(a)]))
        e = np.array([bs + R * math.sin(th), R - R * math.cos(th)])
        d = np.array([math.cos(th), math.sin(th)])
        return e + (s - bs - arc) * d, d

    def tri(x):
        f = x - math.floor(x)
        return 1 - abs(2 * f - 1)

    def radius(s, side):
        if s < g["thread_len"]:
            ph = 0.0 if side > 0 else 0.5
            r = rm + (rM - rm) * tri(s / pitch + ph)
            r = min(r, rM - 1.0 + s)            # 1x45 chamfer at tip
        elif s < g["shoulder"]:
            r = r12
        else:
            r = r14
        r = min(r, r14 - 1.0 + (Ls - s))        # 1x45 chamfer at bent end
        return max(r, 0.05)

    s_list = list(np.arange(0, Ls, 0.02))
    eps = 1e-6
    for b in (g["thread_len"], g["shoulder"]):
        s_list += [b - eps, b + eps]
    s_list.append(Ls)
    s_list = np.array(sorted(s_list))
    left, right = [], []
    for s in s_list:
        p, t = center(s)
        n = np.array([-t[1], t[0]])
        left.append(p + radius(s, +1) * n)
        right.append(p - radius(s, -1) * n)
    poly = np.array(left + right[::-1])
    far, d = center(Ls)
    return poly, float(far[0]), Ls


def _inset(pts, dist):
    area = 0.5 * np.sum(pts[:, 0] * np.roll(pts[:, 1], -1)
                        - np.roll(pts[:, 0], -1) * pts[:, 1])
    nxt, prv = np.roll(pts, -1, axis=0), np.roll(pts, 1, axis=0)
    t = nxt - prv
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-12)
    n = np.stack([t[:, 1], -t[:, 0]], axis=1)   # right-hand normal
    if area < 0:
        n = -n
    return pts - dist * n * -1 if False else pts + dist * (-n)


def render(g=None, mm_per_px=0.06, size=(2448, 2048), angle_deg=0.0,
           center_px=None, flip=False, blur_px=0.8, noise=2.0, bg=215, fg=30,
           ss=4):
    """Render a grey uint8 image of the part. Returns (image, truth dict)."""
    g = dict(NOMINAL, **(g or {}))
    poly, axial, _ = outline_mm(g)
    poly = poly - poly.mean(axis=0)
    if flip:
        poly[:, 1] *= -1
    a = math.radians(angle_deg)
    Rm = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    pts = poly @ Rm.T / mm_per_px
    W, H = size
    if center_px is None:
        center_px = (W / 2, H / 2)
    pts += np.array(center_px)
    # fillPoly also paints boundary pixels (~0.5 hi-res px outside the
    # polygon) -> inset the polygon by that amount so edges are unbiased
    pts = _inset(pts, 0.5 / ss)
    # render bounding box at ss x resolution then down-sample (anti-aliasing)
    x0, y0 = np.floor(pts.min(axis=0)).astype(int) - 8
    x1, y1 = np.ceil(pts.max(axis=0)).astype(int) + 8
    bw, bh = x1 - x0, y1 - y0
    canvas = np.full((bh * ss, bw * ss), 255, np.uint8)
    p = np.round(((pts - [x0, y0] + 0.5) * ss - 0.5) * 16).astype(np.int32)
    cv2.fillPoly(canvas, [p], 0, lineType=cv2.LINE_8, shift=4)
    patch = cv2.resize(canvas, (bw, bh), interpolation=cv2.INTER_AREA)
    img = np.full((H, W), 255, np.uint8)
    xa, ya = max(0, x0), max(0, y0)
    xb, yb = min(W, x1), min(H, y1)
    img[ya:yb, xa:xb] = patch[ya - y0:yb - y0, xa - x0:xb - x0]
    f = img.astype(np.float32) / 255.0
    out = fg + (bg - fg) * f
    if blur_px > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur_px)
    if noise > 0:
        out += np.random.normal(0, noise, out.shape).astype(np.float32)
    out = np.clip(out, 0, 255).astype(np.uint8)
    truth = {k: g[k] for k in ("thread_len", "shoulder", "bend_start", "d12",
                               "d14", "angle", "drop")}
    truth["axial_len"] = axial
    return out, truth


def random_part(fail_ratio=0.3, rng=random):
    """Random dimensions: mostly good parts, sometimes one defect."""
    g = {"thread_len": rng.uniform(17.55, 17.95), "shoulder": rng.uniform(49.24, 49.36),
         "bend_start": rng.uniform(59.05, 59.25), "d12": rng.uniform(11.97, 12.03),
         "d14": rng.uniform(13.9, 14.1), "angle": rng.uniform(41.2, 41.8),
         "drop": rng.uniform(37.5, 37.9)}
    defect = None
    if rng.random() < fail_ratio:
        defect = rng.choice(["thread_len", "shoulder", "d12", "angle", "drop"])
        g[defect] = {"thread_len": rng.choice([16.8, 18.4]),
                     "shoulder": rng.choice([48.9, 49.6]),
                     "d12": rng.choice([11.88, 12.12]),
                     "angle": rng.choice([39.5, 43.5]),
                     "drop": rng.choice([36.9, 38.5])}[defect]
    return g, defect


# ---------------------------------------------------------------------------
# 3D rendering with a real (perspective) camera
# ---------------------------------------------------------------------------
def _geometry(g):
    """Centerline / radius functions of the part (tip at s=0)."""
    th = math.radians(g["angle"])
    R = g["R"]
    L2 = leg2_for_drop(g)
    bs = g["bend_start"]
    arc = R * th
    Ls = bs + arc + L2
    rM, rm, pitch = g["thread_major"] / 2, g["thread_minor"] / 2, g["pitch"]
    r12, r14 = g["d12"] / 2, g["d14"] / 2

    def center(s):
        if s <= bs:
            return np.array([s, 0.0]), np.array([1.0, 0.0])
        if s <= bs + arc:
            a = (s - bs) / R
            return (np.array([bs + R * math.sin(a), R - R * math.cos(a)]),
                    np.array([math.cos(a), math.sin(a)]))
        e = np.array([bs + R * math.sin(th), R - R * math.cos(th)])
        d = np.array([math.cos(th), math.sin(th)])
        return e + (s - bs - arc) * d, d

    def radius(s):
        if s < g["thread_len"]:
            f = s / pitch - math.floor(s / pitch)
            r = rm + (rM - rm) * (1 - abs(2 * f - 1))
            r = min(r, rM - 1.0 + s)
        elif s < g["shoulder"]:
            r = r12
        else:
            r = r14
        r = min(r, r14 - 1.0 + (Ls - s))
        return max(r, 0.05)
    return center, radius, Ls


def render3d(g=None, k0=0.06, size=(2448, 2048), angle_deg=0.0, offset_mm=(0.0, 0.0),
             flip=False, cam_h=None, pp=None, blur_px=0.8, noise=2.0, bg=215, fg=30,
             ss=3, n_circ=96):
    """Render the part as a 3D solid (round sections, axis at height d14/2,
    lying on the table) seen by a pinhole camera whose lens centre is cam_h mm
    above the table, straight above principal point pp (pixels).

    k0        mm per pixel IN THE TABLE PLANE
    offset_mm part position (centre of its outline) relative to the optical
              centre, in table mm
    cam_h     None = ideal telecentric (no perspective)
    Returns (image, truth dict).
    """
    g = dict(NOMINAL, **(g or {}))
    W, H = size
    if pp is None:
        pp = (W / 2.0, H / 2.0)
    center, radius, Ls = _geometry(g)
    poly, axial, _ = outline_mm(g)
    ref = poly.mean(axis=0)
    a_ax = g["d14"] / 2.0                       # rests on the Ø14 sections
    s_list = list(np.arange(0, Ls, 0.02))
    for b in (g["thread_len"], g["shoulder"]):
        s_list += [b - 1e-6, b + 1e-6]
    s_list.append(Ls)
    s_list = np.array(sorted(set(s_list)))
    ang = math.radians(angle_deg)
    Rm = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    phi = np.linspace(0, 2 * math.pi, n_circ, endpoint=False)
    cphi, sphi = np.cos(phi), np.sin(phi)
    corr = 2.0 / (1.0 + math.cos(math.pi / n_circ))   # polygon -> true circle
    pts = np.empty((len(s_list), n_circ, 2))
    for i, s in enumerate(s_list):
        p, t = center(s)
        q = p - ref
        n2 = np.array([-t[1], t[0]])
        if flip:
            q = q * [1, -1]
            n2 = n2 * [1, -1]
        q = Rm @ q + np.asarray(offset_mm, float)
        n2 = Rm @ n2
        r = radius(s) * corr
        X = q[0] + r * cphi * n2[0]
        Y = q[1] + r * cphi * n2[1]
        Z = a_ax + r * sphi
        if cam_h:
            f = cam_h / (cam_h - Z)
            X, Y = X * f, Y * f
        pts[i, :, 0] = pp[0] + X / k0
        pts[i, :, 1] = pp[1] + Y / k0
    x0, y0 = (np.floor(pts.reshape(-1, 2).min(axis=0)) - 8).astype(int)
    x1, y1 = (np.ceil(pts.reshape(-1, 2).max(axis=0)) + 8).astype(int)
    bw, bh = x1 - x0, y1 - y0
    canvas = np.full((bh * ss, bw * ss), 255, np.uint8)
    hp = ((pts - [x0, y0] + 0.5) * ss - 0.5)
    for i in range(len(s_list) - 1):
        hull = cv2.convexHull(np.vstack([hp[i], hp[i + 1]]).astype(np.float32)).reshape(-1, 2)
        c = hull.mean(axis=0)
        d = hull - c
        nrm = np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
        hull = hull - d / nrm * 0.5             # fill paints ~0.5 px outside
        cv2.fillConvexPoly(canvas, np.round(hull * 16).astype(np.int32), 0,
                           lineType=cv2.LINE_8, shift=4)
    patch = cv2.resize(canvas, (bw, bh), interpolation=cv2.INTER_AREA)
    img = np.full((H, W), 255, np.uint8)
    xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if xb > xa and yb > ya:
        img[ya:yb, xa:xb] = patch[ya - y0:yb - y0, xa - x0:xb - x0]
    out = fg + (bg - fg) * (img.astype(np.float32) / 255.0)
    if blur_px > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur_px)
    if noise > 0:
        out += np.random.normal(0, noise, out.shape).astype(np.float32)
    out = np.clip(out, 0, 255).astype(np.uint8)
    truth = {k: g[k] for k in ("thread_len", "shoulder", "bend_start", "d12",
                               "d14", "angle", "drop")}
    truth["axial_len"] = axial
    return out, truth
