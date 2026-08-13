"""Simulated camera for development without hardware (SKYLAPSE_SIM=1).

Renders a synthetic night sky as a 16-bit RGGB bayer mosaic. Scene radiance
is fixed; measured signal scales with exposure * gain plus shot/read noise,
so the auto-exposure loop converges against it exactly like a real sensor.
Includes a slow-moving satellite streak so timelapses aren't static.
"""
from __future__ import annotations

import time

import numpy as np

from .base import BayerPattern, CameraDriver, CameraInfo, Frame

WIDTH, HEIGHT = 1280, 960
N_STARS = 350
# Scene tuned so ~5s exposure at gain 100 hits a mean around 90/255.
RADIANCE_SCALE = 1.05e-9
FULL_WELL = 65535


class SimDriver(CameraDriver):
    def __init__(self, seed: int = 42) -> None:
        rng = np.random.default_rng(seed)
        self._star_x = rng.uniform(0, WIDTH, N_STARS)
        self._star_y = rng.uniform(0, HEIGHT, N_STARS)
        self._star_mag = rng.power(0.4, N_STARS)          # few bright, many dim
        self._rng = rng
        self._exposure_us = 1_000_000
        self._gain = 100
        self._t0 = time.time()

    @staticmethod
    def probe() -> bool:
        import os
        return os.environ.get("SKYLAPSE_SIM") == "1"

    def open(self) -> CameraInfo:
        return CameraInfo(
            name="Simulated ASI (dev)", camera_id="sim-asi-dev", driver="sim",
            width=WIDTH, height=HEIGHT, bayer=BayerPattern.RGGB, bit_depth=16,
            max_exposure_us=60_000_000, min_exposure_us=32, max_gain=500)

    def set_controls(self, exposure_us: int, gain: int) -> None:
        self._exposure_us = max(32, min(exposure_us, 60_000_000))
        self._gain = max(1, min(gain, 500))

    def capture(self) -> Frame:
        ts = time.time()
        # Scene radiance: sky glow gradient + gaussian star PSFs.
        yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
        scene = 4.0 + 2.0 * (yy / HEIGHT)                 # light pollution gradient
        # Stars drift like real sky rotation (~2 px/s here, sped up for dev)
        drift = (ts - self._t0) * 2.0
        for sx, sy, mag in zip(self._star_x, self._star_y, self._star_mag):
            r2 = (xx - (sx + drift) % WIDTH) ** 2 + (yy - (sy + drift * 0.3) % HEIGHT) ** 2
            scene += 4000.0 * mag * np.exp(-r2 / 3.2)
        # Satellite streak drifting over minutes.
        t = (ts - self._t0) % 240 / 240
        sy = HEIGHT * 0.25 + HEIGHT * 0.5 * t
        streak = np.exp(-((yy - sy) ** 2) / 2.5) * np.exp(
            -((xx - WIDTH * t) ** 2) / (WIDTH * 8.0))
        scene += 900.0 * streak

        signal = scene * self._exposure_us * self._gain * RADIANCE_SCALE * FULL_WELL
        noise = self._rng.normal(0, 60 + np.sqrt(np.maximum(signal, 0)) * 0.5)
        img = signal + noise + 300                         # +300 = bias floor

        # Mosaic to RGGB with a subtle warm cast; clip AFTER channel gains so
        # saturated star cores can't overflow uint16 and wrap to cyan.
        bayer = img.copy()
        bayer[0::2, 0::2] *= 1.06                          # R
        bayer[1::2, 1::2] *= 0.97                          # B
        bayer = np.clip(bayer, 0, FULL_WELL)

        # Simulate an aging sensor: 12 stuck-high pixels at fixed positions,
        # so the hot-pixel correction pipeline has real defects to find.
        hot_rng = np.random.default_rng(7)
        hy = hot_rng.integers(0, HEIGHT, 12)
        hx = hot_rng.integers(0, WIDTH, 12)
        bayer[hy, hx] = FULL_WELL

        return Frame(
            data=bayer.astype(np.uint16).tobytes(),
            width=WIDTH, height=HEIGHT, bayer=BayerPattern.RGGB, bit_depth=16,
            exposure_us=self._exposure_us, gain=self._gain, timestamp=ts,
            sensor_temp_c=22.5)

    def close(self) -> None:
        pass
