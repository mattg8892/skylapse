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

# Filmstrip thumbnails. Canonical here because they are written at capture
# time; the API imports these rather than keeping its own copy.
THUMB_PX = 256
THUMB_PREFIX = "thumb_"
THUMB_QUALITY = 80

_CV_BAYER = {
    BayerPattern.RGGB: cv2.COLOR_BayerBG2BGR,   # OpenCV naming is inverted
    BayerPattern.BGGR: cv2.COLOR_BayerRG2BGR,
    BayerPattern.GRBG: cv2.COLOR_BayerGB2BGR,
    BayerPattern.GBRG: cv2.COLOR_BayerGR2BGR,
}


def _as_array(frame: Frame) -> np.ndarray:
    dtype = np.uint16 if frame.bit_depth > 8 else np.uint8
    return np.frombuffer(frame.data, dtype=dtype).reshape(frame.height, frame.width)


# Colour multipliers, green fixed at 1.0 as the reference. A plain tuple, so
# the pipeline never reaches into config — the daemon owns that lookup and
# passes the camera's own pair down.
NEUTRAL_WB = (1.0, 1.0)

# Rec. 601 luma. The point is the weighting, not the colour science: green is
# most of perceived brightness and blue is almost none of it, which is exactly
# the distinction a flat mosaic mean throws away.
LUMA_R, LUMA_G, LUMA_B = 0.30, 0.59, 0.11

# Where each colour sits in the 2x2 quad, per pattern: (red, (green, green), blue).
_PLANE_OFFSETS = {
    BayerPattern.RGGB: ((0, 0), ((0, 1), (1, 0)), (1, 1)),
    BayerPattern.BGGR: ((1, 1), ((0, 1), (1, 0)), (0, 0)),
    BayerPattern.GRBG: ((0, 1), ((0, 0), (1, 1)), (1, 0)),
    BayerPattern.GBRG: ((1, 0), ((0, 0), (1, 1)), (0, 1)),
}


def plane_means(frame: Frame) -> tuple[float, float, float]:
    """Mean of each colour plane, read straight off the mosaic as (R, G, B).

    Cheaper than debayering and closer to the truth: this is what the sensor
    measured, before any interpolation invented values between the photosites.
    The green figure averages the two green sites per quad rather than letting
    them outvote the others by weight of numbers.

    A mono frame reports the same value for all three, which keeps every caller
    downstream free of a special case.
    """
    arr = _as_array(frame)
    if frame.bayer == BayerPattern.MONO:
        m = float(arr.mean())
        return m, m, m
    (ry, rx), greens, (by, bx) = _PLANE_OFFSETS[frame.bayer]
    r = float(arr[ry::2, rx::2].mean())
    b = float(arr[by::2, bx::2].mean())
    g = float(np.mean([arr[gy::2, gx::2].mean() for gy, gx in greens]))
    return r, g, b


def metered_brightness(means: tuple[float, float, float], bit_depth: int,
                       wb: tuple[float, float] = NEUTRAL_WB) -> float:
    """Auto-exposure's metering value: WB-corrected luminance, scaled to 0-255.

    This used to be the mean of the raw mosaic, which is not a brightness at
    all. Two of every four photosites in an RGGB quad are green, so a flat mean
    is half green by construction — before the sensor's response is even
    considered, and green is the most responsive channel on both cameras here.
    Measured on the IMX477 the mosaic mean sits far above the red and blue
    planes, so the loop was servoing on a number that tracks the green channel
    rather than anything a viewer would call brightness.

    Metering the corrected luminance instead targets what the finished image
    actually looks like. Note that even at neutral 1.0/1.0 this is not the old
    number: it is the correctly *weighted* one, so a camera whose white balance
    has never been set still meters more sensibly than it did.
    """
    r, g, b = means
    wb_r, wb_b = wb
    scale = 255.0 / (65535.0 if bit_depth > 8 else 255.0)
    luma = LUMA_R * r * wb_r + LUMA_G * g + LUMA_B * b * wb_b
    return luma * scale


def mean_brightness(frame: Frame, wb: tuple[float, float] = NEUTRAL_WB) -> float:
    """Metering value for one frame. The capture loop uses the two-step form
    above, because it needs the plane means for the sidecar anyway."""
    return metered_brightness(plane_means(frame), frame.bit_depth, wb)


def gray_world(means: tuple[float, float, float]) -> tuple[float, float]:
    """Multipliers that would make this frame's channel means equal.

    The oldest trick there is, and a seed rather than an answer: it assumes the
    scene averages to grey, which a sky at dusk or a room lit by one warm lamp
    does not. That is precisely why the UI offers it as a suggestion to adjust
    rather than applying it — no automatic estimate can be right for every
    camera and lens, so the manual override is the feature and this is the
    starting point for it.
    """
    r, g, b = means
    return (g / r if r > 0 else 1.0, g / b if b > 0 else 1.0)


def apply_wb(img: np.ndarray, r: float, b: float) -> np.ndarray:
    """Scale red and blue in a debayered BGR image; green is the 1.0 reference.

    Applied while the data is still 16-bit, so the sensor's headroom absorbs
    the multiplication rather than quantising it into 8-bit steps. Clipped at
    full scale: boosting a channel can only push already-bright pixels past the
    top, and letting that wrap would turn a blown highlight into a coloured
    hole in the middle of the frame.
    """
    if r == 1.0 and b == 1.0:
        return img
    ceiling = 65535 if img.dtype == np.uint16 else 255
    out = img.astype(np.float32)
    out[..., 2] *= r          # OpenCV channel order is BGR
    out[..., 0] *= b
    return np.clip(out, 0, ceiling).astype(img.dtype)


# The white-balance preview buffer. The settings screen has to show what a
# *pending* pair of multipliers would look like, and the saved JPEG cannot
# answer that: it already has the applied multipliers baked in, and any
# highlight the previous setting clipped is gone for good. So the capture loop
# leaves the most recent frame's raw mosaic here, decimated, and the api
# renders it on demand exactly the way the pipeline would.
#
# It lives in /run, which is a tmpfs: this is written every frame and must not
# cost the SD card a single write.
WB_PREVIEW_NAME = "wb_preview.npz"
WB_PREVIEW_QUADS = 320          # long edge, counted in 2x2 quads


def decimate_bayer(arr: np.ndarray, quads: int = WB_PREVIEW_QUADS) -> np.ndarray:
    """Shrink a mosaic while keeping it a mosaic.

    Whole 2x2 quads are kept or dropped, never split, so the result still
    debayers as the same pattern. Resizing a bayer array with any ordinary
    interpolation would blend neighbouring colours into each other and produce
    a picture whose colour is an artefact of the resize — which for a white
    balance preview is the one thing that must not happen.
    """
    step = max(1, (max(arr.shape) // 2) // max(1, quads))
    if step == 1:
        return arr
    s = 2 * step
    h, w = (arr.shape[0] // s) * 2, (arr.shape[1] // s) * 2
    out = np.empty((h, w), dtype=arr.dtype)
    out[0::2, 0::2] = arr[0:h // 2 * s:s, 0:w // 2 * s:s]
    out[0::2, 1::2] = arr[0:h // 2 * s:s, 1:w // 2 * s + 1:s]
    out[1::2, 0::2] = arr[1:h // 2 * s + 1:s, 0:w // 2 * s:s]
    out[1::2, 1::2] = arr[1:h // 2 * s + 1:s, 1:w // 2 * s + 1:s]
    return out


def write_wb_preview(frame: Frame, camera_id: str, run_dir: Path) -> Path:
    """Stash the current frame's raw mosaic for the settings preview."""
    path = run_dir / WB_PREVIEW_NAME
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"camera_id": camera_id, "bayer": frame.bayer.value,
            "bit_depth": frame.bit_depth, "timestamp": frame.timestamp}
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, mosaic=decimate_bayer(_as_array(frame)),
             meta=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8))
    tmp.replace(path)            # never serve a half-written buffer
    return path


def read_wb_preview(run_dir: Path) -> tuple[np.ndarray, dict] | None:
    """The stashed mosaic and its metadata, or None if nothing has been captured."""
    path = run_dir / WB_PREVIEW_NAME
    if not path.exists():
        return None
    with np.load(path) as data:
        return data["mosaic"], json.loads(bytes(data["meta"]).decode())


def preview_frame(mosaic: np.ndarray, meta: dict) -> Frame:
    """Wrap a stashed mosaic back into a Frame, so every consumer of the
    preview buffer goes through the same debayer and metering code a real
    capture does instead of reimplementing it."""
    return Frame(data=mosaic.tobytes(), width=mosaic.shape[1],
                 height=mosaic.shape[0], bit_depth=int(meta["bit_depth"]),
                 bayer=BayerPattern(meta["bayer"]), exposure_us=0, gain=0,
                 timestamp=float(meta.get("timestamp", 0.0)))


def render_wb_preview(mosaic: np.ndarray, meta: dict, wb: tuple[float, float],
                      quality: int = 85) -> bytes:
    """Debayer the stashed mosaic with the given multipliers, as a JPEG."""
    ok, buf = cv2.imencode(".jpg", to_bgr(preview_frame(mosaic, meta), wb),
                           [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed for the white balance preview")
    return buf.tobytes()


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


def to_bgr(frame: Frame, wb: tuple[float, float] = NEUTRAL_WB) -> np.ndarray:
    """Raw bayer -> 8-bit BGR. Shared by the JPEG writer and the focus preview
    so the live view shows exactly what a saved frame would look like — which
    now includes the white balance, so nobody has to judge focus through a
    green filter."""
    arr = _as_array(frame)
    if frame.bayer == BayerPattern.MONO:
        img = arr
    else:
        img = apply_wb(cv2.demosaicing(arr, _CV_BAYER[frame.bayer]), *wb)
    if frame.bit_depth > 8:
        img = (img.astype(np.float32) / 257.0).astype(np.uint8)
    return img


def write_preview(frame: Frame, path: Path, quality: int = 85,
                  wb: tuple[float, float] = NEUTRAL_WB) -> Path:
    """Full-resolution JPEG to an arbitrary path, no sidecar.

    Used for the focus live view, which writes to /run (a tmpfs) — focus
    frames must never reach the image store, and keeping them off the SD card
    also keeps them off its write budget.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.jpg")
    cv2.imwrite(str(tmp), to_bgr(frame, wb), [cv2.IMWRITE_JPEG_QUALITY, quality])
    tmp.replace(path)        # atomic: the API never serves a half-written frame
    return path


def save_jpeg(frame: Frame, camera_id: str, quality: int = 92,
              overlay: bool = False, stars: int | None = None,
              wb: tuple[float, float] = NEUTRAL_WB) -> Path:
    # The thumbnail is cut from this same array further down, so it inherits
    # the white balance for free — a filmstrip that did not match the frames
    # it indexes would be its own bug.
    img = to_bgr(frame, wb)
    if overlay:
        from .analyze import burn_overlay
        img = burn_overlay(np.ascontiguousarray(img), frame.timestamp,
                           frame.exposure_us, frame.gain, frame.sensor_temp_c)

    folder = day_folder(frame.timestamp, camera_id)
    stamp = datetime.fromtimestamp(frame.timestamp).strftime("%Y%m%d_%H%M%S")
    path = folder / f"img_{stamp}.jpg"
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    # Thumbnail now, while the debayered array is already in memory. Generating
    # these on demand meant the first scrub of a 1200-frame night made a phone
    # wait on 1200 full-res decodes; here it costs a resize we have paid for.
    write_thumb(img, thumb_path(path))
    _write_sidecar(path, frame)
    return path


def thumb_path(jpeg_path: Path) -> Path:
    """Thumbnail location for a frame: thumb_<stem>.jpg beside it."""
    return jpeg_path.parent / f"{THUMB_PREFIX}{jpeg_path.stem}.jpg"


def write_thumb(img_bgr: np.ndarray, path: Path) -> Path:
    """Downscale an already-decoded BGR frame to THUMB_PX on its long edge."""
    h, w = img_bgr.shape[:2]
    scale = THUMB_PX / max(h, w)
    if scale < 1.0:
        img_bgr = cv2.resize(
            img_bgr, (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), img_bgr, [cv2.IMWRITE_JPEG_QUALITY, THUMB_QUALITY])
    return path


def write_dng(frame: Frame, path: Path,
              wb: tuple[float, float] = NEUTRAL_WB) -> Path:
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
    if frame.bayer != BayerPattern.MONO:
        # The pixel data stays exactly as the sensor gave it — that is the
        # whole point of shipping DNG — but the multipliers ride along so
        # Siril and Lightroom open the frame balanced instead of green. DNG
        # asks for the neutral *in camera space*, which is the reciprocal of
        # the multipliers with green normalised to 1.
        r, b = (v if v > 0 else 1.0 for v in wb)
        tags.set(Tag.AsShotNeutral, [[10000, int(round(10000 * r))],
                                     [10000, 10000],
                                     [10000, int(round(10000 * b))]])

    stem = path.with_suffix("")          # pidng appends .dng itself
    path.parent.mkdir(parents=True, exist_ok=True)
    dng = RAW2DNG()
    dng.options(tags, path=str(stem.parent), compress=False)
    dng.convert(_as_array(frame), filename=stem.name)
    return stem.with_suffix(".dng")


def save_dng(frame: Frame, camera_id: str,
             wb: tuple[float, float] = NEUTRAL_WB) -> Path:
    """Raw bayer -> DNG in the image store, beside the night's JPEGs."""
    folder = day_folder(frame.timestamp, camera_id)
    stamp = datetime.fromtimestamp(frame.timestamp).strftime("%Y%m%d_%H%M%S")
    return write_dng(frame, folder / f"img_{stamp}.dng", wb)


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
    # Per-plane means of the raw mosaic, before any white balance. The
    # metering loop computes these anyway, and keeping them makes a colour
    # cast readable straight out of a night's sidecars instead of needing
    # every frame reopened.
    if getattr(frame, "_raw_means", None) is not None:
        meta["raw_means"] = dict(zip("rgb", frame._raw_means))
    image_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
