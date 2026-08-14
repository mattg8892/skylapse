"""Filmstrip thumbnails are written at capture time.

The lazy path still exists as backfill, but it must not be what a phone hits
on the first scrub of a night — decoding 1200 full-res frames on demand is the
problem this moved to solve.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from skylapse.daemon.drivers.base import BayerPattern, Frame
from skylapse.daemon.pipeline import process


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "IMAGE_ROOT", tmp_path)
    return tmp_path


def _frame(width: int = 640, height: int = 480) -> Frame:
    rng = np.random.default_rng(1)
    data = rng.integers(0, 65535, size=(height, width), dtype=np.uint16)
    return Frame(data=data.tobytes(), width=width, height=height,
                 bayer=BayerPattern.RGGB, bit_depth=16, exposure_us=500_000,
                 gain=100, timestamp=1_786_000_000.0, sensor_temp_c=10.0)


def test_capture_writes_a_thumbnail(store):
    jpeg = process.save_jpeg(_frame(), "cam")
    thumb = process.thumb_path(jpeg)
    assert thumb.exists(), "no thumbnail was written at capture time"
    assert thumb.name.startswith(process.THUMB_PREFIX)


def test_thumbnail_is_bounded_and_much_smaller(store):
    jpeg = process.save_jpeg(_frame(), "cam")
    thumb = process.thumb_path(jpeg)
    img = cv2.imread(str(thumb))
    assert max(img.shape[:2]) <= process.THUMB_PX
    assert thumb.stat().st_size < jpeg.stat().st_size / 5


def test_thumbnail_keeps_aspect_ratio(store):
    jpeg = process.save_jpeg(_frame(640, 480), "cam")
    img = cv2.imread(str(process.thumb_path(jpeg)))
    h, w = img.shape[:2]
    assert abs((w / h) - (640 / 480)) < 0.05


def test_thumbnail_lives_beside_its_frame(store):
    """The nights index filters on img_*.jpg, so a thumb in the same folder
    must not be mistaken for a frame."""
    jpeg = process.save_jpeg(_frame(), "cam")
    thumb = process.thumb_path(jpeg)
    assert thumb.parent == jpeg.parent
    assert not thumb.name.startswith("img_")
    assert len(list(jpeg.parent.glob("img_*.jpg"))) == 1


def test_small_frames_are_not_upscaled(store):
    """A frame already under the thumb size should be copied, not enlarged."""
    out = process.write_thumb(np.zeros((100, 120, 3), dtype=np.uint8),
                              store / "thumb_small.jpg")
    img = cv2.imread(str(out))
    assert img.shape[:2] == (100, 120)
