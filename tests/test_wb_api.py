"""The white balance endpoints: suggest a seed, render a pending setting.

Both read the preview buffer the capture loop leaves in /run, because the
saved JPEG cannot answer either question — it already has the applied
multipliers baked into it, and whatever the previous setting clipped is gone.
"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api
from skylapse.daemon.pipeline import process

from .test_whitebalance import frame, mosaic


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    return TestClient(api.app)


def _capture(cast=(10000, 18000, 13000), camera_id="picam-imx477", size=64):
    """Stand in for the capture loop leaving its preview buffer behind."""
    process.write_wb_preview(frame(mosaic(*cast, size=size)), camera_id,
                             config.RUN_DIR)


def test_suggest_returns_the_inverse_of_the_cast(rig):
    _capture((10000, 18000, 13000))
    body = rig.get("/api/wb/suggest").json()
    assert body["r"] == pytest.approx(1.8, abs=0.01)
    assert body["b"] == pytest.approx(18000 / 13000, abs=0.01)
    assert body["means"]["g"] > body["means"]["r"]


def test_suggest_does_not_apply_anything(rig):
    """The whole contract: it is a suggestion the user accepts or tweaks. No
    automatic estimate is right for every camera and lens."""
    _capture((10000, 18000, 13000))
    rig.get("/api/wb/suggest")
    entry = config.load().cameras.get("picam-imx477")
    assert entry is None or (entry.wb_r, entry.wb_b) == (1.0, 1.0)


def test_suggest_says_when_it_hit_the_slider_limit(rig):
    """A suggestion clamped to the end of the range is not a considered answer,
    and showing it as one would send someone hunting for a fault in the camera."""
    _capture((1000, 18000, 13000))          # a cast far beyond the range
    body = rig.get("/api/wb/suggest").json()
    assert body["clamped"] is True
    assert body["r"] == api.WB_MAX and body["raw_r"] > api.WB_MAX


def test_suggest_before_any_frame_is_a_404_not_a_guess(rig):
    assert rig.get("/api/wb/suggest").status_code == 404


def test_suggest_refuses_a_frame_from_another_camera(rig):
    """Multipliers are per camera. Seeding the ZWO's from an IMX477 frame would
    be worse than not offering the button at all."""
    _capture(camera_id="picam-imx477")
    assert rig.get("/api/wb/suggest?camera_id=zwo-asi676mc").status_code == 409
    assert rig.get("/api/wb/suggest?camera_id=picam-imx477").status_code == 200


def test_preview_renders_the_pending_multipliers(rig):
    import cv2

    _capture((10000, 18000, 13000))
    green = rig.get("/api/wb/preview?r=1.0&b=1.0")
    assert green.headers["content-type"] == "image/jpeg"
    corrected = rig.get("/api/wb/preview?r=1.8&b=1.385")

    def means(resp):
        img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
        return [float(img[..., i].mean()) for i in range(3)]      # B, G, R

    gb, gg, gr = means(green)
    cb, cg, cr = means(corrected)
    assert gr < gg * 0.8, "the uncorrected render should be visibly green"
    assert abs(cr - cg) / cg < 0.05, f"red not corrected: {cr:.0f} vs {cg:.0f}"
    assert abs(cb - cg) / cg < 0.05, f"blue not corrected: {cb:.0f} vs {cg:.0f}"


def test_preview_before_any_frame_is_a_404(rig):
    assert rig.get("/api/wb/preview?r=1.5&b=1.2").status_code == 404


def test_preview_clamps_rather_than_rendering_nonsense(rig):
    _capture()
    assert rig.get("/api/wb/preview?r=99&b=-5").status_code == 200


def test_the_preview_buffer_is_still_a_mosaic(rig):
    """Decimation keeps whole 2x2 quads. Resizing a bayer array with ordinary
    interpolation would blend neighbouring colours into each other, and the
    preview's colour would then be an artefact of the resize — the one thing a
    white balance preview must not be.
    """
    _capture((10000, 18000, 13000), size=512)
    stored, meta = process.read_wb_preview(config.RUN_DIR)
    r, g, b = process.plane_means(process.preview_frame(stored, meta))
    assert (round(r), round(g), round(b)) == (10000, 18000, 13000)


def test_the_preview_buffer_is_smaller_than_the_frame(rig):
    """It is rewritten every frame into a tmpfs. A full 12MP mosaic there would
    be 24MB of RAM per write for a picture nobody is looking at most nights."""
    big = mosaic(10000, 18000, 13000, size=2048)
    process.write_wb_preview(frame(big), "picam-imx477", config.RUN_DIR)
    stored, _ = process.read_wb_preview(config.RUN_DIR)
    assert stored.size < big.size / 4
