"""Camera driver interface.

Every driver returns raw bayer data + metadata. The pipeline owns all image
processing (debayer -> JPEG, bayer -> DNG) so output is identical regardless
of which camera captured it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


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


def detect_camera() -> "CameraDriver":
    """Probe order: simulator (SKYLAPSE_SIM=1), then ZWO on USB, then Pi CSI."""
    from .sim import SimDriver
    from .zwo import ZwoDriver
    from .picam import PiCamDriver

    if SimDriver.probe():
        return SimDriver()
    if ZwoDriver.probe():
        return ZwoDriver()
    if PiCamDriver.probe():
        return PiCamDriver()
    raise CameraError("No supported camera found (checked: ZWO USB, Pi CSI)")
