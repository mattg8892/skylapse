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

# ZWO's white-balance controls are digital gains applied to the RAW buffer, not
# preview-only. 50 is the neutral midpoint of the 1-99 range.
NEUTRAL_WB = 50

# Exposure polling. zwoasi's own capture() waits on the sensor forever, so a
# wedged USB link hangs the capture loop instead of raising — and the daemon's
# reopen-with-backoff recovery never gets a chance to run.
_POLL_INTERVAL_S = 0.01
_CAPTURE_TIMEOUT_MARGIN_S = 15.0


def sdk_path() -> str:
    """Where the vendor library is, or "" if it has not been installed.

    Public because the API's "install ZWO support" button needs to answer the
    same question this driver does, and two independent notions of "is the SDK
    here" would eventually disagree — with the settings screen offering to
    install something already present, or claiming a working rig has nothing.
    """
    for path in _SDK_PATHS:
        if path and os.path.exists(path):
            return path
    return ""


def _load_sdk():
    import zwoasi
    path = sdk_path()
    if path:
        try:
            zwoasi.init(path)
        except zwoasi.ZWO_Error:
            pass                # already initialised is fine
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

        # Force neutral white balance. ZWO ships a non-neutral factory WB
        # (55/75 on the ASI676MC) and applies it to the RAW16 buffer itself:
        # measured on hardware, B/R was 1.69 at the factory values and 1.10 at
        # neutral, on an unchanged scene. Left alone it tints every JPEG blue
        # and — worse — bakes a vendor cast into DNGs that Siril/PixInsight
        # expect to be untouched sensor data. The pipeline owns colour, not the
        # camera firmware.
        for name in ("ASI_WB_R", "ASI_WB_B"):
            control = getattr(self._asi, name, None)
            if control is None:
                continue
            try:
                self._cam.set_control_value(control, NEUTRAL_WB, auto=False)
            except Exception as exc:
                log.warning("Could not set %s to neutral: %s", name, exc)

        # Stable id: SDK camera ID (user-settable flash id) or serial where
        # supported; older models (ASI120 era) fall back to sanitized name.
        cam_id = ""
        try:
            cam_id = str(self._cam.get_id() or "").strip()
        except Exception:
            pass
        if not cam_id:
            cam_id = props["Name"].lower().replace(" ", "-").replace("(", "").replace(")", "")
        # The name fallback already carries the vendor ("ZWO ASI676MC"), so the
        # f"zwo-{cam_id}" below would yield "zwo-zwo-asi676mc" — and camera_id
        # is both the config registry key and the image folder name. (The
        # ASI676MC raises ZWO_IOError from get_id(), so this path is not rare:
        # any model without a writable flash id lands here.)
        if cam_id.startswith("zwo-"):
            cam_id = cam_id[len("zwo-"):]

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

    def _expose(self) -> bytearray:
        """Bounded stand-in for zwoasi's capture().

        Same sequence, but the wait for the sensor can time out. zwoasi loops
        on ASI_EXP_WORKING with no deadline, so a camera that stops responding
        mid-exposure (brownout on a USB3 rig is the classic cause) would block
        the capture loop forever. Timing out instead turns that into a
        CameraError, which is what the daemon's reopen-with-backoff expects.
        """
        asi = self._asi
        self._cam.start_exposure()
        deadline = (time.time() + self._exposure_us / 1_000_000
                    + _CAPTURE_TIMEOUT_MARGIN_S)
        while True:
            status = self._cam.get_exposure_status()
            if status != asi.ASI_EXP_WORKING:
                break
            if time.time() > deadline:
                try:
                    self._cam.stop_exposure()
                except Exception:
                    pass                      # already wedged; nothing to save
                raise CameraError(
                    f"Exposure timed out ({self._exposure_us / 1_000_000:.1f}s "
                    f"+ {_CAPTURE_TIMEOUT_MARGIN_S:.0f}s margin)")
            time.sleep(_POLL_INTERVAL_S)
        if status != asi.ASI_EXP_SUCCESS:
            raise CameraError(f"Exposure failed (status {status})")
        return self._cam.get_data_after_exposure(None)

    def capture(self) -> Frame:
        assert self._cam and self._info
        ts = time.time()
        try:
            data = self._expose()       # blocking, bounded; raw bayer buffer
        except CameraError:
            raise
        except Exception as exc:  # zwoasi raises its own hierarchy
            raise CameraError(f"ZWO capture failed: {exc}") from exc

        temp = None
        try:
            # SDK reports temperature in tenths of a degree C.
            temp = self._cam.get_control_value(self._asi.ASI_TEMPERATURE)[0] / 10.0
        except Exception:
            pass

        return Frame(
            data=bytes(data),
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
