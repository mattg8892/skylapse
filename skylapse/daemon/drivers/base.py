"""Camera driver interface.

Every driver returns raw bayer data + metadata. The pipeline owns all image
processing (debayer -> JPEG, bayer -> DNG) so output is identical regardless
of which camera captured it.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class BayerPattern(str, Enum):
    RGGB = "RGGB"
    BGGR = "BGGR"
    GRBG = "GRBG"
    GBRG = "GBRG"
    MONO = "MONO"


@dataclass
class CameraInfo:
    name: str
    camera_id: str                   # stable hardware id: registry key + image folder
    driver: str                      # "zwo" | "picam"
    width: int
    height: int
    bayer: BayerPattern
    bit_depth: int                   # 8, 12, 14, 16
    max_exposure_us: int
    min_exposure_us: int
    max_gain: int
    supports_cooling: bool = False


@dataclass
class Frame:
    """One raw capture. `data` is the raw bayer buffer as bytes."""
    data: bytes
    width: int
    height: int
    bayer: BayerPattern
    bit_depth: int
    exposure_us: int
    gain: int
    timestamp: float                 # unix time at capture start
    sensor_temp_c: float | None = None
    meta: dict = field(default_factory=dict)


class CameraError(Exception):
    """Raised on device-level failures (disconnect, timeout, ...)."""


class CameraDriver(ABC):
    """Lifecycle: probe() -> open() -> [set_controls()/capture()]* -> close()."""

    @staticmethod
    @abstractmethod
    def probe() -> bool:
        """Return True if a camera this driver handles is attached. Cheap; no state."""

    @abstractmethod
    def open(self) -> CameraInfo: ...

    @abstractmethod
    def set_controls(self, exposure_us: int, gain: int) -> None: ...

    @abstractmethod
    def capture(self) -> Frame:
        """Blocking single capture with current controls. Raises CameraError."""

    @abstractmethod
    def close(self) -> None: ...


def driver_of(camera_id: str) -> str:
    """Driver name from a camera_id.

    Every id is built as "<driver>-<hardware identity>" — "zwo-asi676mc",
    "picam-imx477" — so the prefix is the driver. That convention is what lets
    `active_camera` in the config select hardware without the config having to
    carry a second, redundant field that could disagree with the id.
    """
    return (camera_id or "").split("-", 1)[0]


def detect_camera(prefer: str = "") -> "CameraDriver":
    """Pick a camera driver.

    Default order is simulator (SKYLAPSE_SIM=1), then ZWO on USB, then Pi CSI.
    ZWO leads because it is the primary target: a rig with both attached is
    almost always a ZWO imaging camera on a Pi that happens to have a module.

    `prefer` is a camera_id (or bare driver name) from `config.active_camera`,
    for the rig where that assumption is wrong and the Pi module is the one
    pointed at the sky. A preference that is not actually present falls back to
    the normal order rather than failing — unplugging the preferred camera
    should degrade to capturing with the other one, not stop the night.
    """
    from .sim import SimDriver
    from .zwo import ZwoDriver
    from .picam import PiCamDriver

    drivers = [("sim", SimDriver), ("zwo", ZwoDriver), ("picam", PiCamDriver)]

    wanted = driver_of(prefer)
    if wanted:
        for name, cls in drivers:
            if name == wanted:
                if cls.probe():
                    log.info("Using %s (selected by active_camera=%s)", name, prefer)
                    return cls()
                log.warning("active_camera=%s requested the %s driver, but no such "
                            "camera is attached; falling back to probe order",
                            prefer, name)
                break
        else:
            log.warning("active_camera=%s names an unknown driver %r; ignoring",
                        prefer, wanted)

    for name, cls in drivers:
        if cls.probe():
            return cls()
    raise CameraError("No supported camera found (checked: ZWO USB, Pi CSI)")
