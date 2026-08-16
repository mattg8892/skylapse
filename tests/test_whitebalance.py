"""White balance: the multipliers, the grey-world seed, and AE metering.

Two sensors here produce green frames for the same reason and it is not a bug
in either of them: an RGGB quad has two green photosites, green has the highest
quantum efficiency, and until now nothing downstream corrected for either. The
ZWO session removed the vendor's white balance from the raw buffer, which was
right for raw fidelity and left the JPEG path with no colour stage at all.

The metering tests are the ones that matter most. Auto-exposure servos on a
single number, and that number was the flat mean of the mosaic — half green by
construction before the sensor's response is even considered.
"""
from __future__ import annotations

import numpy as np
import pytest

from skylapse.daemon.drivers.base import BayerPattern, Frame
from skylapse.daemon.pipeline import process


def mosaic(r: int, g: int, b: int, size: int = 64,
           pattern: BayerPattern = BayerPattern.RGGB) -> np.ndarray:
    """A flat frame with exactly these values in each colour plane."""
    arr = np.zeros((size, size), dtype=np.uint16)
    (ry, rx), greens, (by, bx) = process._PLANE_OFFSETS[pattern]
    arr[ry::2, rx::2] = r
    arr[by::2, bx::2] = b
    for gy, gx in greens:
        arr[gy::2, gx::2] = g
    return arr


def frame(arr: np.ndarray, pattern: BayerPattern = BayerPattern.RGGB,
          bit_depth: int = 16) -> Frame:
    return Frame(data=arr.tobytes(), width=arr.shape[1], height=arr.shape[0],
                 bayer=pattern, bit_depth=bit_depth, exposure_us=1000, gain=0,
                 timestamp=1_786_000_000.0)


# -- reading the planes ------------------------------------------------------

@pytest.mark.parametrize("pattern", [
    BayerPattern.RGGB, BayerPattern.BGGR,
    BayerPattern.GRBG, BayerPattern.GBRG,
])
def test_plane_means_find_the_right_photosites(pattern):
    """Every pattern, because getting the offsets wrong swaps red and blue and
    the result still looks like a plausible colour cast."""
    arr = mosaic(1000, 2000, 3000, pattern=pattern)
    r, g, b = process.plane_means(frame(arr, pattern))
    assert (round(r), round(g), round(b)) == (1000, 2000, 3000)


def test_the_green_sites_are_averaged_not_summed():
    """Two of four photosites are green. Averaging them keeps green one vote of
    three; letting them accumulate is precisely the metering bug."""
    arr = mosaic(1000, 2000, 3000)
    _, g, _ = process.plane_means(frame(arr))
    assert round(g) == 2000, "green counted twice"
    assert round(float(arr.mean())) == 2000, "check the fixture, not the code"


def test_a_mono_frame_reports_one_value_for_all_three():
    arr = np.full((32, 32), 1234, dtype=np.uint16)
    assert process.plane_means(frame(arr, BayerPattern.MONO)) == (1234, 1234, 1234)


# -- applying the multipliers ------------------------------------------------

def test_channel_means_shift_exactly_as_commanded():
    img = np.full((16, 16, 3), 1000, dtype=np.uint16)      # BGR
    out = process.apply_wb(img, r=1.8, b=1.3)
    assert round(float(out[..., 2].mean())) == 1800        # red
    assert round(float(out[..., 1].mean())) == 1000        # green untouched
    assert round(float(out[..., 0].mean())) == 1300        # blue


def test_green_is_the_reference_and_is_never_scaled():
    img = np.random.default_rng(0).integers(0, 60000, (32, 32, 3), dtype=np.uint16)
    out = process.apply_wb(img, r=2.5, b=0.4)
    assert np.array_equal(out[..., 1], img[..., 1])


def test_neutral_multipliers_are_a_no_op():
    """Not merely equal — the same object. Every existing rig runs at 1.0/1.0,
    and a full-frame float round trip per frame for nothing is real work on a
    Pi."""
    img = np.full((8, 8, 3), 500, dtype=np.uint16)
    assert process.apply_wb(img, 1.0, 1.0) is img


def test_boosting_clips_at_full_scale_rather_than_wrapping():
    """uint16 arithmetic wraps silently. A blown highlight scaled past the top
    would come back as a dark coloured hole in the middle of the frame — much
    worse than the clipped white it should be."""
    img = np.full((8, 8, 3), 60000, dtype=np.uint16)
    out = process.apply_wb(img, r=2.0, b=2.0)
    assert out[..., 2].max() == 65535
    assert out[..., 0].max() == 65535


def test_eight_bit_frames_clip_at_their_own_ceiling():
    img = np.full((8, 8, 3), 200, dtype=np.uint8)
    out = process.apply_wb(img, r=2.0, b=1.0)
    assert out.dtype == np.uint8 and out[..., 2].max() == 255


def test_the_debayered_image_carries_the_correction():
    """End to end through to_bgr: a green-cast mosaic comes out neutral once
    the inverse multipliers are applied."""
    arr = mosaic(10000, 18000, 13000)
    wb = process.gray_world(process.plane_means(frame(arr)))
    img = process.to_bgr(frame(arr), wb)
    b, g, r = (float(img[..., i].mean()) for i in range(3))
    assert abs(r - g) / g < 0.02, f"red still off: {r:.0f} vs {g:.0f}"
    assert abs(b - g) / g < 0.02, f"blue still off: {b:.0f} vs {g:.0f}"


# -- the grey-world seed -----------------------------------------------------

def test_gray_world_recovers_the_inverse_of_a_known_cast():
    """Impose a cast, ask for the seed, get the multipliers that undo it."""
    r_mult, b_mult = process.gray_world((10000.0, 18000.0, 13000.0))
    assert r_mult == pytest.approx(1.8, abs=0.001)
    assert b_mult == pytest.approx(18000 / 13000, abs=0.001)


def test_gray_world_leaves_an_already_neutral_frame_alone():
    assert process.gray_world((5000.0, 5000.0, 5000.0)) == (1.0, 1.0)


def test_gray_world_survives_a_black_frame():
    """A closed shutter divides by zero otherwise, and the suggestion endpoint
    is reachable at any hour."""
    assert process.gray_world((0.0, 0.0, 0.0)) == (1.0, 1.0)


def test_applying_the_seed_equalises_the_planes():
    arr = mosaic(9000, 20000, 11000)
    means = process.plane_means(frame(arr))
    r_mult, b_mult = process.gray_world(means)
    r, g, b = means
    assert r * r_mult == pytest.approx(g, rel=1e-6)
    assert b * b_mult == pytest.approx(g, rel=1e-6)


# -- per-camera isolation ----------------------------------------------------

def test_each_camera_keeps_its_own_multipliers():
    from skylapse import config

    cfg = config.Config()
    zwo = cfg.camera("zwo-asi676mc")
    pi = cfg.camera("picam-imx477")
    pi.wb_r, pi.wb_b = 1.85, 1.32

    assert (zwo.wb_r, zwo.wb_b) == (1.0, 1.0), \
        "tuning one camera moved another's colour"
    assert (pi.wb_r, pi.wb_b) == (1.85, 1.32)


def test_a_new_camera_starts_neutral():
    """Defaults are today's behaviour exactly. A first frame from an unknown
    camera must not arrive with someone else's colour correction on it."""
    from skylapse import config

    entry = config.Config().camera("brand-new-camera")
    assert (entry.wb_r, entry.wb_b) == (1.0, 1.0)


def test_multipliers_survive_a_save_and_load(tmp_path, monkeypatch):
    from skylapse import config

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    cfg.camera("picam-imx477").wb_r = 1.85
    cfg.camera("picam-imx477").wb_b = 1.32
    config.save(cfg)

    entry = config.load().cameras["picam-imx477"]
    assert (entry.wb_r, entry.wb_b) == (1.85, 1.32)
