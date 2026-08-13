"""ZWO ASI driver via the `zwoasi` Python bindings over ZWO's C SDK.

Requires the SDK shared library; the installer places it at
/usr/local/lib/libASICamera2.so and sets ZWO_ASI_LIB accordingly.
"""
from __future__ import annotations

import logging
import os
import time

from .base import BayerPattern, CameraDriver, CameraError, CameraInfo, Frame

log = logging.getLogger(__name__)

_SDK_PATHS = [
    os.environ.get("ZWO_ASI_LIB", ""),
    "/usr/local/lib/libASICamera2.so",
    "/usr/lib/libASICamera2.so",
]

_BAYER_MAP = {0: BayerPattern.RGGB, 1: BayerPattern.BGGR,
              2: BayerPattern.GRBG, 3: BayerPattern.GBRG}


def _load_sdk():
    import zwoasi
    for path in _SDK_PATHS:
        if path and os.path.exists(path):
            try:
                zwoasi.init(path)
                return zwoasi
            except zwoasi.ZWO_Error:
                # Already initialised is fine; re-raise anything else.
                return zwoasi
    raise CameraError("ZWO SDK library not found (set ZWO_ASI_LIB)")


class ZwoDriver(CameraDriver):
    def __init__(self) -> None:
        self._asi = None
        self._cam = None
        self._info: CameraInfo | None = None
        self._exposure_us = 100_000
        self._gain = 0

    @staticmethod
    def probe() -> bool:
        try:
            asi = _load_sdk()
            return asi.get_num_cameras() > 0
        except Exception:
            return False

    def open(self) -> CameraInfo:
        self._asi = _load_sdk()
        n = self._asi.get_num_cameras()
        if n == 0:
            raise CameraError("No ZWO camera on USB")
        self._cam = self._asi.Camera(0)
        props = self._cam.get_camera_property()
        controls = self._cam.get_controls()

        is_color = bool(props.get("IsColorCam"))
        bayer = _BAYER_MAP.get(props.get("BayerPattern", 0), BayerPattern.RGGB) \
            if is_color else BayerPattern.MONO

        # RAW16 when the sensor exceeds 8 bits, else RAW8.
        bit_depth = int(props.get("BitDepth", 12))
        img_type = self._asi.ASI_IMG_RAW16 if bit_depth > 8 else self._asi.ASI_IMG_RAW8
        self._cam.set_image_type(img_type)
        self._cam.disable_dark_subtract()

        # Stable id: SDK camera ID (user-settable flash id) or serial where
        # supported; older models (ASI120 era) fall back to sanitized name.
        cam_id = ""
        try:
            cam_id = str(self._cam.get_id() or "").strip()
        except Exception:
            pass
        if not cam_id:
            cam_id = props["Name"].lower().replace(" ", "-").replace("(", "").replace(")", "")

        exp = controls["Exposure"]
        gain = controls["Gain"]
        self._info = CameraInfo(
            name=props["Name"],
            camera_id=f"zwo-{cam_id}",
            driver="zwo",
            width=props["MaxWidth"],
            height=props["MaxHeight"],
            bayer=bayer,
            bit_depth=16 if bit_depth > 8 else 8,
            max_exposure_us=exp["MaxValue"],
            min_exposure_us=exp["MinValue"],
            max_gain=gain["MaxValue"],
            supports_cooling=bool(props.get("IsCoolerCam")),
        )
        log.info("Opened %s (%dx%d, %s)", self._info.name,
                 self._info.width, self._info.height, bayer.value)
        return self._info

    def set_controls(self, exposure_us: int, gain: int) -> None:
        assert self._cam and self._info
        exposure_us = max(self._info.min_exposure_us,
                          min(exposure_us, self._info.max_exposure_us))
        gain = max(0, min(gain, self._info.max_gain))
        self._cam.set_control_value(self._asi.ASI_EXPOSURE, exposure_us)
        self._cam.set_control_value(self._asi.ASI_GAIN, gain)
        self._exposure_us, self._gain = exposure_us, gain

    def capture(self) -> Frame:
        assert self._cam and self._info
        ts = time.time()
        try:
            data = self._cam.capture()  # blocking; returns numpy array
        except Exception as exc:  # zwoasi raises its own hierarchy
            raise CameraError(f"ZWO capture failed: {exc}") from exc

        temp = None
        try:
            # SDK reports temperature in tenths of a degree C.
            temp = self._cam.get_control_value(self._asi.ASI_TEMPERATURE)[0] / 10.0
        except Exception:
            pass

        return Frame(
            data=data.tobytes(),
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
        if self._cam:
            self._cam.close()
            self._cam = None
