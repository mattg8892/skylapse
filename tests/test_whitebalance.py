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


# -- AE metering -------------------------------------------------------------

def test_correcting_the_cast_raises_the_metered_value():
    """The direction here is the opposite of the obvious guess, and it is the
    whole reason the fix works.

    You might expect a green-cast frame to meter *lower* once green stops being
    double-counted. It does not: green is 2 of 4 photosites, so it is 50% of
    the mosaic mean, while Rec. 601 luma gives it 59%. Switching to luma at
    neutral multipliers therefore moves the number by about +2% and nothing
    useful happens.

    What moves it is the white balance. Correcting the cast lifts red and blue
    *up to* green, so the corrected frame is genuinely brighter than the
    uncorrected mosaic mean suggested — measured on the ZWO bench means from
    DESIGN.md, +17%. Auto-exposure answers that by pulling exposure down, which
    is the visible fix for frames that were both green and too bright.
    """
    arr = mosaic(12258, 18030, 13300)          # the ZWO bench scene
    f = frame(arr)
    mosaic_mean = float(arr.mean()) * 255.0 / 65535.0
    corrected = process.mean_brightness(f, process.gray_world(process.plane_means(f)))
    assert corrected > mosaic_mean * 1.10, (
        f"correcting the cast should raise metering well clear of the mosaic "
        f"mean: {corrected:.1f} vs {mosaic_mean:.1f}")


def test_metering_is_a_luminance_not_a_photosite_count():
    """Each channel moves the metered value by its luma weight, not by how many
    photosites happen to carry it."""
    base = process.metered_brightness((10000.0, 10000.0, 10000.0), 16)
    scale = 255.0 / 65535.0
    for channel, weight in ((0, process.LUMA_R), (1, process.LUMA_G),
                            (2, process.LUMA_B)):
        means = [10000.0, 10000.0, 10000.0]
        means[channel] += 1000.0
        moved = process.metered_brightness(tuple(means), 16) - base
        assert moved == pytest.approx(1000.0 * weight * scale, rel=1e-6)


def test_a_neutral_frame_meters_the_same_as_its_mosaic_mean():
    """The luma weights sum to 1, so on a frame with no cast the correction
    changes nothing. A metering fix that moved neutral frames too would be
    silently re-exposing every mono and every already-balanced camera."""
    arr = mosaic(12000, 12000, 12000)
    mosaic_mean = float(arr.mean()) * 255.0 / 65535.0
    assert process.mean_brightness(frame(arr)) == pytest.approx(mosaic_mean, rel=1e-6)


def test_metering_follows_the_multipliers():
    """Once a camera's white balance is set, the metered value is the corrected
    image's brightness — otherwise applying WB would quietly re-expose the rig."""
    arr = mosaic(10000, 18000, 13000)
    f = frame(arr)
    corrected = process.mean_brightness(f, process.gray_world(process.plane_means(f)))
    assert corrected > process.mean_brightness(f), \
        "boosting red and blue must raise the metered brightness"


def test_metering_is_unchanged_for_a_camera_still_at_neutral():
    """Per-camera isolation, at the metering level: a rig that has not set its
    own multipliers must meter exactly as it did."""
    arr = mosaic(10000, 18000, 13000)
    f = frame(arr)
    assert process.mean_brightness(f, (1.0, 1.0)) == process.mean_brightness(f)


def test_eight_bit_frames_scale_correctly():
    arr = (mosaic(100, 100, 100) // 1).astype(np.uint8)
    assert process.mean_brightness(frame(arr, bit_depth=8)) == pytest.approx(100, rel=1e-6)


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


def test_a_camera_at_neutral_gets_the_pipeline_it_had_before():
    """No colour stage at all at 1.0/1.0 — not "close enough".

    Verified once against the pre-white-balance commit directly: same frame,
    byte-identical debayer and byte-identical encoded JPEG. This is the durable
    form of that check. It matters because the ZWO stays at 1.0/1.0 until
    somebody moves its own sliders, and a rig that has been running for weeks
    must not have its output shift under it by a release.
    """
    import cv2

    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65535, (256, 256), dtype=np.uint16)
    f = frame(arr)

    expected = cv2.demosaicing(arr, process._CV_BAYER[BayerPattern.RGGB])
    expected = (expected.astype(np.float32) / 257.0).astype(np.uint8)
    assert np.array_equal(process.to_bgr(f), expected)


def test_a_neutral_camera_is_not_charged_for_the_colour_stage():
    """apply_wb returns the input object untouched at 1.0/1.0, so the frames of
    every unconfigured rig do not pay for a full-frame float round trip."""
    arr = np.zeros((64, 64, 3), dtype=np.uint16)
    assert process.apply_wb(arr, 1.0, 1.0) is arr


# -- automatic white balance -------------------------------------------------

def test_auto_wb_defaults_on_for_a_new_camera():
    """Asked for after a whole night came back green because the multipliers
    had never been set. That is not a mistake worth making twice."""
    from skylapse import config
    assert config.Config().camera("picam-imx477").wb_auto is True


def test_the_blend_cannot_be_swung_by_one_frame():
    """A car's headlights crossing the frame must not recolour the night, so
    each measurement moves it only a fraction of the way."""
    from skylapse.daemon.main import AUTO_WB_BLEND
    current, wild = 1.0, 3.0
    after = (1 - AUTO_WB_BLEND) * current + AUTO_WB_BLEND * wild
    assert after < 1.5, f"one frame moved it to {after}"
    settled = current
    for _ in range(30):
        settled = (1 - AUTO_WB_BLEND) * settled + AUTO_WB_BLEND * wild
    assert settled > 2.9, "but a sustained change must still arrive"
