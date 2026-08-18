"""Per-frame analysis: star count (doubles as a cloud detector) and the
optional JPEG overlay burn-in."""
from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np

from ..drivers.base import BayerPattern

# OpenCV's Bayer naming is inverted relative to ours, exactly as in process.py.
_CV_BAYER_GRAY = {
    BayerPattern.RGGB: cv2.COLOR_BayerBG2GRAY,
    BayerPattern.BGGR: cv2.COLOR_BayerRG2GRAY,
    BayerPattern.GRBG: cv2.COLOR_BayerGB2GRAY,
    BayerPattern.GBRG: cv2.COLOR_BayerGR2GRAY,
}

# -- star detection ----------------------------------------------------------
#
# Tuned against a real night on the rig (2026-08-17, 2205 frames, IMX477 behind
# a 180-degree fisheye), because the previous version was not tuned against
# anything. It thresholded the raw Bayer mosaic at a fixed offset and counted
# every connected component, which on that night meant 215,553 "stars" in an
# 8:15 PM twilight frame with none visible, and 248,138 on a 2:23 AM frame so
# dew-covered you cannot see through it. It was counting the mosaic's own
# checkerboard, sensor noise, and water droplets.
#
# Four things fix that, in the order they matter:
#
# 1. Demosaic to grey first. Adjacent pixels in a mosaic are different colour
#    channels, so the mosaic has a built-in checkerboard that survives any
#    local-contrast test. This alone was most of the count.
# 2. Threshold at n sigma above the frame's OWN measured noise, not a fixed
#    number. Dew, warm sensors and bright skies all raise the noise floor, and
#    a fixed offset counts that rise as stars. Measured: sigma doubles on the
#    dewed frames, which is precisely why their counts must fall.
# 3. Shape. A star is a small round dot. Foliage edges, rooflines and droplet
#    rims are elongated or sprawling, and an area/aspect/fill filter removes
#    them without removing stars.
# 4. A dark local background. Stars are not visible against a lit sky, so a
#    detection sitting on bright background is not one — which rejects
#    streetlights, the moon's surround, and daylight.
#
# The numbers below are the measured separation on that night, not guesses:
# countable frames peaked at a background p90 of 189, while twilight and
# daylight frames sat at 250-254 (a saturated sky), so the gate at 200 has
# margin on both sides.

SIGMA_K = 7.0              # detection threshold, in sigma above the noise
BACKGROUND_KERNEL = 7      # px; must exceed a star and stay under a droplet
MIN_AREA = 2               # px; one bright pixel is a cosmic ray or a hot pixel
MAX_AREA = 30              # px; above this it is a droplet, a light, or the moon
MAX_ASPECT = 1.5           # long edge / short edge; stars are round
MIN_FILL = 0.5             # blob area / bounding box; rejects edge fragments
DARK_BACKGROUND = 60       # 8-bit; a star cannot be seen against a brighter sky
SKY_SATURATED = 200        # 8-bit background p90 above this = no stars, at all
SANITY_CAP = 20_000        # a real frame never has more; anything more is a bug


def _grey_plane(arr: np.ndarray, bayer: BayerPattern | None) -> np.ndarray:
    """An 8-bit single-channel view of the frame, free of the Bayer pattern.

    8-bit because that is the scale every constant here was measured at, and
    because a star faint enough to be lost by the shift is also faint enough to
    be indistinguishable from noise.
    """
    img = (arr >> 8).astype(np.uint8) if arr.dtype != np.uint8 else arr
    if img.ndim == 3:                       # already demosaiced: take green
        return img[:, :, 1]
    code = _CV_BAYER_GRAY.get(bayer) if bayer is not None else None
    return cv2.demosaicing(img, code) if code else img


def measure_noise(residual: np.ndarray) -> float:
    """Robust per-frame noise level from the residual.

    Median absolute deviation rather than a standard deviation: stars are a
    tiny fraction of the pixels but are enormous outliers, so an ordinary
    sigma would be set by the very things being detected. Floored at one
    count, since a threshold of zero counts everything.
    """
    residual = residual.astype(np.float32)
    mad = float(np.median(np.abs(residual - np.median(residual))))
    return max(1.4826 * mad, 1.0)


def star_count(arr: np.ndarray, bayer: BayerPattern | None = None) -> int:
    """How many star-like points are in this frame.

    Doubles as a cloud detector: the per-night chart of this IS the sky quality
    trend, and a count that craters on a clear night means something arrived
    between the camera and the sky — cloud, or dew on the dome.
    """
    plane = _grey_plane(arr, bayer)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (BACKGROUND_KERNEL, BACKGROUND_KERNEL))
    # Opening removes anything smaller than the kernel, so what is left is the
    # sky gradient, the horizon, and any droplet big enough to have its own
    # shape. Subtracting it leaves point sources standing alone.
    background = cv2.morphologyEx(plane, cv2.MORPH_OPEN, kernel)
    residual = cv2.subtract(plane, background)

    # No stars are visible through a lit sky, and pretending otherwise is what
    # produced six figures of them at dusk.
    if float(np.percentile(background, 90)) > SKY_SATURATED:
        return 0

    threshold = SIGMA_K * measure_noise(residual)
    binary = (residual.astype(np.float32) > threshold).astype(np.uint8)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8)
    if count <= 1:
        return 0

    area = stats[1:, cv2.CC_STAT_AREA]
    width = stats[1:, cv2.CC_STAT_WIDTH]
    height = stats[1:, cv2.CC_STAT_HEIGHT]
    long_edge = np.maximum(width, height)
    short_edge = np.maximum(1, np.minimum(width, height))

    keep = (area >= MIN_AREA) & (area <= MAX_AREA)
    keep &= (long_edge / short_edge) <= MAX_ASPECT
    keep &= (area / np.maximum(1, width * height)) >= MIN_FILL

    rows = np.clip(centroids[1:, 1].astype(int), 0, background.shape[0] - 1)
    cols = np.clip(centroids[1:, 0].astype(int), 0, background.shape[1] - 1)
    keep &= background[rows, cols] < DARK_BACKGROUND

    return int(min(keep.sum(), SANITY_CAP))


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
