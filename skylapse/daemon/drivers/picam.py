"""Raspberry Pi camera driver via picamera2/libcamera.

Functional stub: probe + open + capture are wired for the HQ camera (IMX477)
raw stream; full control mapping (per-sensor raw formats, long-exposure modes)
is the first post-scaffold task. Tracked in GitHub issue #1.
"""
from __future__ import annotations

import logging
import time

from .base import BayerPattern, CameraDriver, CameraError, CameraInfo, Frame

log = logging.getLogger(__name__)

_BAYER_FROM_FORMAT = {
    "SRGGB12": BayerPattern.RGGB, "SBGGR12": BayerPattern.BGGR,
    "SGRBG12": BayerPattern.GRBG, "SGBRG12": BayerPattern.GBRG,
    "SRGGB10": BayerPattern.RGGB, "SBGGR10": BayerPattern.BGGR,
}


class PiCamDriver(CameraDriver):
    def __init__(self) -> None:
        self._picam = None
        self._info: CameraInfo | None = None
        self._exposure_us = 100_000
        self._gain = 1

    @staticmethod
    def probe() -> bool:
        try:
            from picamera2 import Picamera2
            return len(Picamera2.global_camera_info()) > 0
        except Exception:
            return False

    def open(self) -> CameraInfo:
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraError("picamera2 not installed") from exc

        self._picam = Picamera2()
        sensor = self._picam.sensor_resolution
        raw_format = str(self._picam.sensor_format)
        bayer = next((v for k, v in _BAYER_FROM_FORMAT.items() if k in raw_format),
                     BayerPattern.RGGB)

        config = self._picam.create_still_configuration(
            raw={"size": sensor}, buffer_count=2)
        self._picam.configure(config)
        self._picam.start()

        model = self._picam.camera_properties.get("Model", "picam")
        self._info = CameraInfo(
            name=self._picam.camera_properties.get("Model", "Pi Camera"),
            camera_id=f"picam-{str(model).lower().replace(' ', '-')}",
            driver="picam",
            width=sensor[0],
            height=sensor[1],
            bayer=bayer,
            bit_depth=16,   # unpacked raw is delivered as 16-bit container
            max_exposure_us=200_000_000,  # sensor-dependent; refined in issue #1
            min_exposure_us=100,
            max_gain=22,
            supports_cooling=False,
        )
        log.info("Opened %s (%dx%d)", self._info.name, *sensor)
        return self._info

    def set_controls(self, exposure_us: int, gain: int) -> None:
        assert self._picam
        self._picam.set_controls({
            "ExposureTime": exposure_us,
            "AnalogueGain": max(1.0, float(gain)),
            "AeEnable": False,
        })
        self._exposure_us, self._gain = exposure_us, gain

    def capture(self) -> Frame:
        assert self._picam and self._info
        ts = time.time()
        try:
            raw = self._picam.capture_array("raw")
        except Exception as exc:
            raise CameraError(f"Pi camera capture failed: {exc}") from exc
        return Frame(
            data=raw.tobytes(),
            width=self._info.width,
            height=self._info.height,
            bayer=self._info.bayer,
            bit_depth=self._info.bit_depth,
            exposure_us=self._exposure_us,
            gain=self._gain,
            timestamp=ts,
        )

    def close(self) -> None:
        if self._picam:
            self._picam.stop()
            self._picam.close()
            self._picam = None
