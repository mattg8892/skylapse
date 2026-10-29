"""Frame annotation: the optional burned-in timestamp/exposure/gain/temperature
overlay. Default off for purists.

Per-frame star counting used to live here too (the "sky quality" chart). It
was removed on 2026-10-29: the number meant nothing to the person reading it,
the chart read as broken on every night it was right, and the morphological
pass behind it was some of the heaviest per-frame CPU the daemon did.
"""
from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np


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
