"""Raspberry Pi camera driver via picamera2/libcamera.

Verified against an Arducam IMX477 (4056x3040) on a Pi 5, 2026-08-15. Four
things about this path are not guessable and cost a frame each if assumed:

1. **The default raw stream is not raw.** `create_still_configuration(raw=...)`
   without an explicit format yields `BGGR_PISP_COMP1` on a Pi 5 — the PiSP
   compressed format, about 1 byte per pixel. Handing that to a debayer as if
   it were 16-bit bayer produces garbage. We ask for an unpacked format and let
   libcamera tell us what it actually gave us.
2. **Rows are padded.** The 4056-pixel row arrives with a 8128-byte stride —
   4064 uint16, so eight pixels of padding per row. Reshaping to the nominal
   width shears the image progressively down the frame.
3. **The bayer order comes from the configured stream, not the sensor.** The
   sensor advertises SRGGB12_CSI2P; the stream we are handed is SBGGR16. Read
   the order off the wrong one and red and blue swap.
4. **Control limits depend on the configuration and must be read after it.**
   Before `configure()` this sensor reports a 66ms exposure ceiling; after,
   694 seconds. A hardcoded guess is wrong in whichever direction it was
   guessed — the original stub said 200s.

Measured and deliberately *not* changed: the 12-bit samples arrive
left-shifted into the 16-bit container (max 65520 = 4095 << 4, low nibble
always clear), so the pipeline's full-range 16-bit scaling is already correct.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from .base import BayerPattern, CameraDriver, CameraError, CameraInfo, Frame

log = logging.getLogger(__name__)

# Ask for an unpacked 12-bit bayer stream. libcamera answers with whatever it
# can actually deliver (SBGGR16 here), which is why the reply is authoritative.
RAW_FORMAT = "SRGGB12"

# Frame duration has to cover the exposure or libcamera clamps it, which is how
# a requested 30s sub silently becomes a 66ms one.
FRAME_DURATION_MARGIN_US = 1_000

_BAYER_CODES = (
    ("RGGB", BayerPattern.RGGB), ("BGGR", BayerPattern.BGGR),
    ("GRBG", BayerPattern.GRBG), ("GBRG", BayerPattern.GBRG),
)


def bayer_from_stream_format(fmt: str) -> BayerPattern:
    """Bayer order from a libcamera stream format such as 'SBGGR16'."""
    for code, pattern in _BAYER_CODES:
        if code in fmt.upper():
            return pattern
    raise CameraError(f"Unrecognised raw stream format: {fmt}")


class PiCamDriver(CameraDriver):
    def __init__(self) -> None:
        self._picam = None
        self._info: CameraInfo | None = None
        self._stride = 0
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
            raise CameraError(
                "picamera2 not installed — it comes from apt "
                "(python3-picamera2) and needs a venv built with "
                "--system-site-packages") from exc

        self._picam = Picamera2()
        sensor = self._picam.sensor_resolution
        config = self._picam.create_still_configuration(
            raw={"size": sensor, "format": RAW_FORMAT}, buffer_count=2)
        self._picam.configure(config)

        # What we asked for and what we get are different things; this is the
        # buffer geometry the capture path must actually honour.
        raw = self._picam.camera_configuration()["raw"]
        width, height = raw["size"]
        self._stride = raw["stride"]
        bayer = bayer_from_stream_format(raw["format"])

        self._picam.start()

        controls = self._picam.camera_controls
        exposure = controls.get("ExposureTime", (100, 200_000_000, None))
        gain = controls.get("AnalogueGain", (1.0, 16.0, None))

        model = str(self._picam.camera_properties.get("Model", "picam"))
        self._info = CameraInfo(
            name=f"Pi Camera ({model})",
            camera_id=f"picam-{model.lower().replace(' ', '-')}",
            driver="picam",
            width=width,
            height=height,
            bayer=bayer,
            # Samples are left-shifted into the 16-bit container, so the full
            # 16-bit range is the honest description for the pipeline.
            bit_depth=16,
            max_exposure_us=int(exposure[1]),
            min_exposure_us=int(exposure[0]),
            max_gain=int(gain[1]),
            supports_cooling=False,
        )
        log.info("Opened %s (%dx%d, %s, stride %d, exposure %d-%dus, gain <=%d)",
                 self._info.name, width, height, bayer.value, self._stride,
                 self._info.min_exposure_us, self._info.max_exposure_us,
                 self._info.max_gain)
        return self._info

    def set_controls(self, exposure_us: int, gain: int) -> None:
        assert self._picam and self._info
        exposure_us = max(self._info.min_exposure_us,
                          min(exposure_us, self._info.max_exposure_us))
        # AnalogueGain is a float multiplier here, not the ZWO's integer scale.
        gain_value = max(1.0, min(float(gain), float(self._info.max_gain)))
        duration = exposure_us + FRAME_DURATION_MARGIN_US
        self._picam.set_controls({
            "ExposureTime": exposure_us,
            "AnalogueGain": gain_value,
            "AeEnable": False,
            # Pinning the frame duration around the exposure is what actually
            # permits a long sub; without it the exposure is clamped to fit the
            # current frame rate.
            "FrameDurationLimits": (duration, duration),
        })
        self._exposure_us, self._gain = exposure_us, int(gain_value)

    def capture(self) -> Frame:
        assert self._picam and self._info
        ts = time.time()
        try:
            buf = self._picam.capture_array("raw")
        except Exception as exc:
            raise CameraError(f"Pi camera capture failed: {exc}") from exc

        # capture_array gives bytes shaped by the stride. Reinterpret as 16-bit,
        # then drop the row padding — skipping this shears the image.
        arr = buf.view(np.uint16).reshape(self._info.height, self._stride // 2)
        arr = np.ascontiguousarray(arr[:, :self._info.width])

        temp = None
        try:
            temp = float(self._picam.capture_metadata().get("SensorTemperature"))
        except Exception:
            pass

        return Frame(
            data=arr.tobytes(),
            width=self._info.width,
            height=self._info.height,
            bayer=self._info.bayer,
            bit_depth=self._info.bit_depth,
            exposure_us=self._exposure_us,
            gain=self._gain,
            timestamp=ts,
            sensor_temp_c=temp,
        )

    def close(self) -> None:
        if self._picam:
            self._picam.stop()
            self._picam.close()
            self._picam = None
