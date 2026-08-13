"""Focus assist: turn the ring until the number peaks.

Sharpness = variance of the Laplacian over the center crop (center 25% of
the frame — the optical sweet spot; fisheye edges are always soft). The
score is relative: the UI shows current, session best, and trend so you
know which direction you just turned. Focus frames are never saved to disk.
"""
from __future__ import annotations

import cv2
import numpy as np

CENTER_FRACTION = 0.5        # center 50% per axis = center 25% by area
TIMEOUT_S = 15 * 60          # auto-exit: you WILL walk away and forget


def sharpness(arr: np.ndarray) -> float:
    """Variance of Laplacian on the center crop. Higher = sharper.
    Works directly on the raw bayer mosaic — the bayer pattern adds a
    constant texture floor, but the *relative* peak is what matters and
    skipping debayer keeps the loop fast for live feedback."""
    h, w = arr.shape
    ch, cw = int(h * CENTER_FRACTION), int(w * CENTER_FRACTION)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    crop = arr[y0:y0 + ch, x0:x0 + cw]
    if crop.dtype != np.uint8:                     # scale 16-bit to 8 for cv2
        crop = (crop >> 8).astype(np.uint8)
    return float(cv2.Laplacian(crop, cv2.CV_64F).var())


class FocusSession:
    """Tracks best-seen and trend across a focus run."""

    def __init__(self) -> None:
        self.best = 0.0
        self.history: list[float] = []

    def update(self, score: float) -> dict:
        self.best = max(self.best, score)
        self.history.append(score)
        recent = self.history[-5:]
        trend = "improving" if len(recent) >= 2 and recent[-1] > recent[0] \
            else "worsening" if len(recent) >= 2 and recent[-1] < recent[0] \
            else "flat"
        return {"score": round(score, 1), "best": round(self.best, 1),
                "trend": trend, "frames": len(self.history)}
