"""Automatic inspection trigger.

Watches the inspection area on a small copy of every frame:
  EMPTY      - no part in area                         -> "PLACE PART"
  EDGE       - something touches the area border (hand) -> "REMOVE HAND"
  MOVING     - part present but still moving
  COUNTDOWN  - part still; inspection after auto_delay_s (default 2 s)
  INSPECTING - measurement running
  RESULT     - result shown until the part is removed (or replaced)
"""
import time

import cv2
import numpy as np

EMPTY, EDGE, MOVING, COUNTDOWN, INSPECTING, RESULT = (
    "EMPTY", "EDGE", "MOVING", "COUNTDOWN", "INSPECTING", "RESULT")


class AutoTrigger:
    def __init__(self, cfg):
        self.update_cfg(cfg)
        self.state = EMPTY
        self.prev = None
        self.stable_since = None
        self.empty_since = None
        self.big_motion_frames = 0
        self.remaining = 0.0
        self.part_pct = 0.0
        self.motion_pct = 0.0
        self.enabled = True

    def update_cfg(self, cfg):
        self.delay = float(cfg.get("auto_delay_s", 2.0))
        self.motion_th = float(cfg.get("stable_motion_pct", 1.5))
        self.min_area = float(cfg.get("min_part_area_pct", 0.5))
        self.dark_part = bool(cfg.get("dark_part", True))
        self.mode = cfg.get("threshold_mode", "otsu")
        self.fixed = float(cfg.get("fixed_threshold", 110))

    def _mask(self, small):
        g = small if self.dark_part else 255 - small
        if self.mode == "fixed":
            t = self.fixed
        else:
            t, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            # Otsu on an empty bright area picks noise -> require real contrast
            if float(np.percentile(g, 99)) - float(np.percentile(g, 1)) < 40:
                return np.zeros_like(g, dtype=bool)
        return g <= t

    def update(self, roi_gray_small, now=None):
        """roi_gray_small: small grey image of the inspection area.
        Returns True when an inspection must start now."""
        now = now or time.time()
        m = self._mask(roi_gray_small)
        part_px = int(m.sum())
        self.part_pct = 100.0 * part_px / m.size
        present = self.part_pct >= self.min_area
        edge = bool(m[0].any() or m[-1].any() or m[:, 0].any() or m[:, -1].any())
        if self.prev is not None and self.prev.shape == m.shape and part_px > 0:
            changed = int(np.count_nonzero(m ^ self.prev))
            self.motion_pct = 100.0 * changed / max(part_px, 1)
        else:
            self.motion_pct = 100.0
        self.prev = m
        fire = False

        if self.state == INSPECTING:
            return False
        if not present:
            if self.state == RESULT:
                self.empty_since = self.empty_since or now
                if now - self.empty_since > 0.4:
                    self.state = EMPTY
            else:
                self.state = EMPTY
            self.stable_since = None
            return False
        self.empty_since = None

        if self.state == RESULT:
            # part replaced without emptying area completely?
            self.big_motion_frames = self.big_motion_frames + 1 \
                if self.motion_pct > 10 * self.motion_th else 0
            if self.big_motion_frames >= 3:
                self.state = MOVING
                self.stable_since = None
            return False

        if edge:
            self.state = EDGE
            self.stable_since = None
        elif self.motion_pct > self.motion_th:
            self.state = MOVING
            self.stable_since = None
        else:
            if self.stable_since is None:
                self.stable_since = now
            self.remaining = self.delay - (now - self.stable_since)
            self.state = COUNTDOWN
            if self.remaining <= 0 and self.enabled:
                self.state = INSPECTING
                fire = True
        return fire

    def done(self):
        self.state = RESULT
        self.big_motion_frames = 0

    def reset(self):
        self.state = EMPTY
        self.stable_since = None
