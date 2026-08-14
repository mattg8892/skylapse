"""Frame -> files. Debayer + JPEG for every frame; bayer -> DNG for keepers.

The pipeline owns all processing so both drivers produce identical output.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from ...config import IMAGE_ROOT
from ..drivers.base import BayerPattern, Frame

_CV_BAYER = {
    BayerPattern.RGGB: cv2.COLOR_BayerBG2BGR,   # OpenCV naming is inverted
    BayerPattern.BGGR: cv2.COLOR_BayerRG2BGR,
    BayerPattern.GRBG: cv2.COLOR_BayerGB2BGR,
    BayerPattern.GBRG: cv2.COLOR_BayerGR2BGR,
}


def _as_array(frame: Frame) -> np.ndarray:
    dtype = np.uint16 if frame.bit_depth > 8 else np.uint8
    return np.frombuffer(frame.data, dtype=dtype).reshape(frame.height, frame.width)


def mean_brightness(frame: Frame) -> float:
    """Mean of the raw buffer scaled to 0-255, for the auto-exposure loop."""
    arr = _as_array(frame)
    scale = 255.0 / (65535.0 if frame.bit_depth > 8 else 255.0)
    return float(arr.mean()) * scale


def day_folder(ts: float, camera_id: str) -> Path:
    """images/<camera_id>/YYYY-MM-DD — camera dimension is in the path from
    day one so multi-camera rigs never need a store migration. Date rolls
    over at local noon so one night stays in one folder."""
    local = datetime.fromtimestamp(ts).astimezone()
    if local.hour < 12:
        local -= timedelta(days=1)
    folder = IMAGE_ROOT / camera_id / local.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def to_bgr(frame: Frame) -> np.ndarray:
    """Raw bayer -> 8-bit BGR. Shared by the JPEG writer and the focus preview
    so the live view shows exactly what a saved frame would look like."""
    arr = _as_array(frame)
    img = arr if frame.bayer == BayerPattern.MONO \
        else cv2.demosaicing(arr, _CV_BAYER[frame.bayer])
    if frame.bit_depth > 8:
        img = (img.astype(np.float32) / 257.0).astype(np.uint8)
    return img


def write_preview(frame: Frame, path: Path, quality: int = 85) -> Path:
    """Full-resolution JPEG to an arbitrary path, no sidecar.

    Used for the focus live view, which writes to /run (a tmpfs) — focus
    frames must never reach the image store, and keeping them off the SD card
    also keeps them off its write budget.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.jpg")
    cv2.imwrite(str(tmp), to_bgr(frame), [cv2.IMWRITE_JPEG_QUALITY, quality])
    tmp.replace(path)        # atomic: the API never serves a half-written frame
    return path


def save_jpeg(frame: Frame, camera_id: str, quality: int = 92,
              overlay: bool = False, stars: int | None = None) -> Path:
    img = to_bgr(frame)
    if overlay:
        from .analyze import burn_overlay
        img = burn_overlay(np.ascontiguousarray(img), frame.timestamp,
                           frame.exposure_us, frame.gain, frame.sensor_temp_c)

    folder = day_folder(frame.timestamp, camera_id)
    stamp = datetime.fromtimestamp(frame.timestamp).strftime("%Y%m%d_%H%M%S")
    path = folder / f"img_{stamp}.jpg"
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    _write_sidecar(path, frame)
    return path


def write_dng(frame: Frame, path: Path) -> Path:
    """Raw bayer -> DNG at an explicit path. Opens directly in
    Lightroom/Siril/PixInsight.

    Split out from save_dng so the writer can be exercised without the image
    store's date-folder layout — see tests/test_dng.py. That test exists
    because this call sequence is version-sensitive: pidng 4.x takes the tags
    through options() and its convert() accepts only (image, filename).
    Passing tags= to convert() raises TypeError at runtime, which is exactly
    how every DNG write on the rig failed while the caller logged success.
    """
    from pidng.core import RAW2DNG, DNGTags, Tag
    from pidng.defs import CFAPattern, PhotometricInterpretation

    cfa = {
        BayerPattern.RGGB: CFAPattern.RGGB, BayerPattern.BGGR: CFAPattern.BGGR,
        BayerPattern.GRBG: CFAPattern.GRBG, BayerPattern.GBRG: CFAPattern.GBRG,
    }

    tags = DNGTags()
    tags.set(Tag.ImageWidth, frame.width)
    tags.set(Tag.ImageLength, frame.height)
    tags.set(Tag.BitsPerSample, frame.bit_depth)
    if frame.bayer != BayerPattern.MONO:
        tags.set(Tag.CFAPattern, cfa[frame.bayer])
        tags.set(Tag.PhotometricInterpretation, PhotometricInterpretation.Color_Filter_Array)
    tags.set(Tag.Make, "Skylapse")

    stem = path.with_suffix("")          # pidng appends .dng itself
    path.parent.mkdir(parents=True, exist_ok=True)
    dng = RAW2DNG()
    dng.options(tags, path=str(stem.parent), compress=False)
    dng.convert(_as_array(frame), filename=stem.name)
    return stem.with_suffix(".dng")


def save_dng(frame: Frame, camera_id: str) -> Path:
    """Raw bayer -> DNG in the image store, beside the night's JPEGs."""
    folder = day_folder(frame.timestamp, camera_id)
    stamp = datetime.fromtimestamp(frame.timestamp).strftime("%Y%m%d_%H%M%S")
    return write_dng(frame, folder / f"img_{stamp}.dng")


def _write_sidecar(image_path: Path, frame: Frame) -> None:
    meta = {
        "timestamp": frame.timestamp,
        "exposure_us": frame.exposure_us,
        "gain": frame.gain,
        "sensor_temp_c": frame.sensor_temp_c,
        "bayer": frame.bayer.value,
        "bit_depth": frame.bit_depth,
    }
    if getattr(frame, "_stars", None) is not None:
        meta["stars"] = frame._stars
    image_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
