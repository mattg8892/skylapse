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

# libcamera applies a control change several frames later, and the queue keeps
# serving frames exposed at the old settings meanwhile — measured at six on an
# IMX477. Taking the first frame after a change files it under the new exposure,
# which is how 100ms, 500ms and 2s captures came back byte-for-byte identical.
SETTLE_FRAMES = 8

# And never longer than this in wall-clock, whatever that works out to in
# frames. Discarding eight frames is free at 100ms and costs five and a half
# minutes at forty seconds; a frame exposed at nearly the right settings beats
# no frame at all.
SETTLE_MAX_S = 90.0

# A control change smaller than this is applied without waiting for it. The
# frame that arrives is within a few percent of what was asked for, which is
# well inside the tolerance _settled() would have accepted anyway.
SETTLE_WORTH_WAITING = 0.10

# The sensor quantises exposure to its line time (100000us is honoured as
# 99954us), so settling is judged on a tolerance, never on equality.
SETTLE_TOLERANCE = 0.02

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
        self._gain_value = 1.0
        self._settling = False

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
        # Only a change worth waiting for starts a settle. Discarding frames is
        # how the sensor is given time to apply new settings, and at a 25 second
        # exposure that is expensive — measured across a real night, 18% of it
        # went on settling. A nudge from gain 14 to 15, or a 2% exposure change,
        # produces a frame indistinguishable from the one being waited for, so
        # waiting for it buys nothing and costs a minute.
        exposure_moved = abs(exposure_us - self._exposure_us)             > max(1, self._exposure_us) * SETTLE_WORTH_WAITING
        gain_moved = abs(gain_value - self._gain_value)             > max(1.0, self._gain_value) * SETTLE_WORTH_WAITING
        if exposure_moved or gain_moved:
            self._settling = True
        self._exposure_us, self._gain_value = exposure_us, gain_value
        self._gain = int(gain_value)

    def _settled(self, meta: dict) -> bool:
        """Whether this frame was actually exposed at the settings we asked for."""
        exposure = meta.get("ExposureTime")
        gain = meta.get("AnalogueGain")
        if exposure is None or gain is None:
            return True                  # nothing to check against; take it
        # Both tolerances are relative, and the gain one was not — it was a
        # flat 0.05, which at gain 17 is a third of a percent. Analogue gain is
        # quantised in hardware, so a requested 17.0 comes back as whatever the
        # sensor could actually make: 16.9, 17.2. That never matched, so the
        # loop discarded frames until it gave up, and every gain change through
        # a night cost the full timeout — about 90 seconds, spent waiting for
        # an exactness the sensor is incapable of.
        #
        # The signature is unmistakable once you know it: a gap with no control
        # change across it, because after giving up the frame taken still
        # carries the old settings.
        exposure_tolerance = max(2.0, self._exposure_us * SETTLE_TOLERANCE)
        gain_tolerance = max(0.05, self._gain_value * SETTLE_TOLERANCE)
        return (abs(exposure - self._exposure_us) <= exposure_tolerance
                and abs(gain - self._gain_value) <= gain_tolerance)

    def _settled_request(self):
        """A request whose metadata reflects the current controls.

        Only pays the cost after a control change — in steady state, including
        every frame of a manual-exposure night, the first request is taken.
        """
        budget = SETTLE_FRAMES if self._settling else 0
        # Bounded by time as well as by frames. Eight discards is nothing at a
        # 100ms exposure and five and a half minutes at forty seconds — which is
        # how a night lost 40% of its frames to a loop that kept nudging the
        # controls. Past the deadline the next frame is taken and labelled with
        # what the sensor actually did, which is a slightly stale exposure
        # rather than a missing one.
        deadline = time.monotonic() + SETTLE_MAX_S
        for _ in range(budget):
            request = self._picam.capture_request()
            if self._settled(request.get_metadata()):
                self._settling = False
                return request
            request.release()
            if time.monotonic() > deadline:
                log.warning("Controls unsettled after %.0fs; taking the next "
                            "frame rather than spending more of the night",
                            SETTLE_MAX_S)
                break
        # Out of budget: take the next frame and report what it really was
        # rather than blocking a night waiting for the sensor to agree.
        if self._settling:
            log.warning("Controls did not settle within %d frames; recording the "
                        "exposure the sensor reports", SETTLE_FRAMES)
            self._settling = False
        return self._picam.capture_request()

    def capture(self) -> Frame:
        assert self._picam and self._info
        ts = time.time()
        try:
            request = self._settled_request()
        except Exception as exc:
            raise CameraError(f"Pi camera capture failed: {exc}") from exc

        try:
            meta = request.get_metadata()
            buf = request.make_array("raw")
            # Bytes are shaped by the stride. Reinterpret as 16-bit, then drop
            # the row padding — skipping this shears the image down the frame.
            arr = buf.view(np.uint16).reshape(self._info.height, self._stride // 2)
            arr = np.ascontiguousarray(arr[:, :self._info.width])
        finally:
            request.release()

        temp = meta.get("SensorTemperature")
        return Frame(
            data=arr.tobytes(),
            width=self._info.width,
            height=self._info.height,
            bayer=self._info.bayer,
            bit_depth=self._info.bit_depth,
            # What the sensor actually did, not what we asked for: it quantises
            # to its line time, and the sidecar should record the truth.
            exposure_us=int(meta.get("ExposureTime", self._exposure_us)),
            gain=int(round(meta.get("AnalogueGain", self._gain_value))),
            timestamp=ts,
            sensor_temp_c=float(temp) if temp is not None else None,
        )

    def close(self) -> None:
        if self._picam:
            self._picam.stop()
            self._picam.close()
            self._picam = None
