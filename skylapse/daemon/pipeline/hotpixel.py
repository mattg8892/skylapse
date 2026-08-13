"""Automatic hot-pixel correction. Zero user effort by design.

No lens-cap dark sessions: hot pixels don't move, stars do. Track the
element-wise MINIMUM across the night's frames — sky rotation sweeps stars
away, leaving only defects standing above the local background. The map is
bucketed by exposure (defect count grows with exposure/temperature),
persisted to disk, and applied during capture by replacing each hot pixel
with the mean of its same-bayer-color neighbors.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("skylapse.hotpixel")

MIN_FRAMES = 20          # frames min-stacked before a map is trusted
DETECT_MARGIN = 1500     # 16-bit counts above local median => defect
MEDIAN_KSIZE = 5


def exposure_bucket(exposure_us: int) -> str:
    """Coarse buckets — defect population scales with exposure, not exact value."""
    for label, limit in (("sub1s", 1_000_000), ("1-8s", 8_000_000),
                         ("8-30s", 30_000_000)):
        if exposure_us < limit:
            return label
    return "30s+"


class HotPixelMap:
    """Builds and applies one map per exposure bucket."""

    def __init__(self, calib_dir: Path) -> None:
        self.calib_dir = calib_dir
        self._min_stack: dict[str, np.ndarray] = {}
        self._counts: dict[str, int] = {}
        self._maps: dict[str, np.ndarray] = {}       # bool masks of defects
        self._load()

    # -- build (called with every raw frame; cheap) ------------------------

    def observe(self, arr: np.ndarray, exposure_us: int) -> None:
        b = exposure_bucket(exposure_us)
        if b not in self._min_stack:
            self._min_stack[b] = arr.copy()
            self._counts[b] = 1
            return
        np.minimum(self._min_stack[b], arr, out=self._min_stack[b])
        self._counts[b] += 1
        if self._counts[b] == MIN_FRAMES or self._counts[b] % 100 == 0:
            self._detect(b)

    def _detect(self, bucket: str) -> None:
        stack = self._min_stack[bucket]
        # Compare each pixel against neighbors of the SAME bayer color: median
        # the four subplanes independently so per-channel gains (R/B cast)
        # can't skew the background estimate and mass-flag one color.
        mask = np.zeros(stack.shape, dtype=bool)
        for oy in (0, 1):
            for ox in (0, 1):
                plane = stack[oy::2, ox::2]
                background = cv2.medianBlur(plane, MEDIAN_KSIZE)
                mask[oy::2, ox::2] = plane.astype(np.int32) > \
                    background.astype(np.int32) + DETECT_MARGIN
        self._maps[bucket] = mask
        self._save(bucket, mask)
        log.info("Hot pixel map [%s]: %d defects from %d frames",
                 bucket, int(mask.sum()), self._counts[bucket])

    # -- apply -------------------------------------------------------------

    def correct(self, arr: np.ndarray, exposure_us: int) -> np.ndarray:
        """Replace defects with the mean of same-color bayer neighbors (±2 px).
        Returns arr unchanged if no trusted map exists for this bucket yet.
        """
        mask = self._maps.get(exposure_bucket(exposure_us))
        if mask is None or not mask.any():
            return arr
        out = arr.copy()
        ys, xs = np.nonzero(mask)
        h, w = arr.shape
        for y, x in zip(ys, xs):
            neighbors = []
            for dy, dx in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and not mask[ny, nx]:
                    neighbors.append(int(arr[ny, nx]))
            if neighbors:
                out[y, x] = int(sum(neighbors) / len(neighbors))
        return out

    # -- persistence -------------------------------------------------------

    def _save(self, bucket: str, mask: np.ndarray) -> None:
        self.calib_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self.calib_dir / f"hotpixels_{bucket}.npz", mask=mask)

    def _load(self) -> None:
        if not self.calib_dir.exists():
            return
        for f in self.calib_dir.glob("hotpixels_*.npz"):
            bucket = f.stem.replace("hotpixels_", "")
            try:
                self._maps[bucket] = np.load(f)["mask"]
                log.info("Loaded hot pixel map [%s]: %d defects",
                         bucket, int(self._maps[bucket].sum()))
            except Exception:
                log.warning("Corrupt hot pixel map %s ignored", f)
