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
SMOOTH_FRAMES = 3            # rolling mean applied to the reported score

# Live-view defaults. Short enough to feel responsive while turning a ring,
# hot enough to show faint stars; both are overridable per frame from the UI.
DEFAULT_EXPOSURE_MS = 500
DEFAULT_GAIN = 250


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
        self.history: list[float] = []      # raw scores, one per frame
        self.smoothed: list[float] = []     # what the UI is shown

    def update(self, score: float) -> dict:
        """Fold in one frame's raw score and report the smoothed view.

        Raw variance-of-Laplacian jitters several percent frame to frame on
        sensor noise alone, which is the same order as the change you get from
        a small nudge of the focus ring — so the raw number is unusable for
        judging whether you just improved things. Everything reported here
        (score, best, trend) is derived from a rolling mean of the last
        SMOOTH_FRAMES frames, which means `best` lags the raw peak slightly.
        That is the intended trade: a stable number you can actually chase.
        """
        self.history.append(score)
        window = self.history[-SMOOTH_FRAMES:]
        smoothed = sum(window) / len(window)
        self.smoothed.append(smoothed)
        self.best = max(self.best, smoothed)

        recent = self.smoothed[-5:]
        trend = "improving" if len(recent) >= 2 and recent[-1] > recent[0] \
            else "worsening" if len(recent) >= 2 and recent[-1] < recent[0] \
            else "flat"
        return {"score": round(smoothed, 1), "best": round(self.best, 1),
                "trend": trend, "frames": len(self.history),
                # Raw value alongside, for the sparkline's per-frame detail.
                "score_raw": round(score, 1)}
