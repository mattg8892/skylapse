"""Per-frame analysis: star count (doubles as a cloud detector) and the
optional JPEG overlay burn-in."""
from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np


def star_count(arr: np.ndarray) -> int:
    """Threshold + blob count on the raw mosaic. Clouds crater the number;
    the per-night chart of this IS the sky quality trend."""
    img = (arr >> 8).astype(np.uint8) if arr.dtype != np.uint8 else arr
    background = cv2.medianBlur(img, 5)
    peaks = cv2.subtract(img, background)
    _, binary = cv2.threshold(peaks, 20, 255, cv2.THRESH_BINARY)
    n, _ = cv2.connectedComponents(binary)
    return max(0, n - 1)                     # minus the background component


def burn_overlay(bgr: np.ndarray, timestamp: float, exposure_us: int,
                 gain: int, temp_c: float | None) -> np.ndarray:
    """Timestamp/exposure/gain/temp in the bottom-left corner. Called only
    when the overlay setting is on; default off for purists."""
    stamp = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    parts = [stamp, f"{exposure_us / 1e6:.1f}s", f"gain {gain}"]
    if temp_c is not None:
        parts.append(f"{temp_c:.0f}C")
    text = "  ".join(parts)
    y = bgr.shape[0] - 14
    # Black outline + white text = readable on any sky.
    cv2.putText(bgr, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(bgr, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (255, 255, 255), 1, cv2.LINE_AA)
    return bgr
