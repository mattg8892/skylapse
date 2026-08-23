"""Star count tracks sky clarity; overlay burns text; aurora thresholds map
to latitude and alerts fire once per episode, never in daylight."""
from unittest import mock

import cv2
import numpy as np

from skylapse import config
from skylapse.daemon import aurora
from skylapse.daemon.pipeline.analyze import burn_overlay, star_count


def sky(n_stars, cloud_blur=0.0, background=1500, noise=60):
    """A synthetic night sky.

    Stars are drawn as small discs, not single pixels. That is what a star
    looks like through a real lens, and it is what the detector now requires:
    a lone bright pixel is a hot pixel or a cosmic ray, and counting those is
    part of what made a dew-covered frame read as 248,138 stars.
    """
    rng = np.random.default_rng(5)
    img = rng.normal(background, noise, (300, 400)).astype(np.float32)
    for i in range(n_stars):
        # A real sky is mostly faint stars and a few bright ones, which is what
        # makes the detection threshold matter: raise it and the faint ones go
        # first. A field of identically-bright stars would hide that entirely.
        # Added to the sky, not painted over it. Light accumulates, so a faint
        # star on a bright background is still brighter than that background —
        # whereas setting an absolute value made faint "stars" come out DARKER
        # than the sky they sat on, which is not a star, and made the fixture
        # disagree with the detector for the wrong reason.
        peak = 30000 * (0.04 + 0.96 * (i / max(1, n_stars - 1)) ** 2)
        y, x = int(rng.integers(10, 290)), int(rng.integers(10, 390))
        patch = np.zeros_like(img)
        cv2.circle(patch, (x, y), 1, peak, -1)
        img += patch
    if cloud_blur:
        img = cv2.GaussianBlur(img, (0, 0), cloud_blur)
    return np.clip(img, 0, 65535).astype(np.uint16)


def test_star_count_scales_with_stars():
    few, many = star_count(sky(15)), star_count(sky(120))
    assert many > few * 3


def test_clouds_crater_the_count():
    clear = star_count(sky(100))
    cloudy = star_count(sky(100, cloud_blur=6.0))
    assert cloudy < clear * 0.3


# -- what the 2026-08-17 night taught it -------------------------------------
#
# The old detector thresholded the raw mosaic at a fixed offset and counted
# every component. On that night it reported 215,553 stars in an 8:15 PM
# twilight frame with none visible, and 248,138 on a 2:23 AM frame too dewed to
# see through. Each test below pins one of the reasons.

def test_a_lit_sky_has_no_stars():
    """Twilight. You cannot see stars through a bright sky, and reporting six
    figures of them is worse than reporting none."""
    assert star_count(sky(100, background=62000, noise=400)) == 0


def test_noise_does_not_become_stars():
    """A starless frame is starless however grainy. The threshold is measured
    in sigma above the frame's own noise, so a noisier sensor raises its own
    bar instead of counting the difference."""
    assert star_count(sky(0)) == 0
    assert star_count(sky(0, noise=600)) == 0


def test_a_noisier_frame_of_the_same_sky_never_reads_higher():
    """Dew, in one line: it scatters light and lifts the noise floor. The count
    must fall, because the stars are harder to see, not rise because the frame
    has more texture in it."""
    clear = star_count(sky(80))
    # Dew does two things, and both are in here: it lifts the background as it
    # scatters light around, and it takes the noise floor up with it. The
    # threshold is measured in sigma, so it rises too, and the faint stars go.
    dewed = star_count(sky(80, noise=900, background=6000))
    assert dewed < clear, f"dew read {dewed}, clear read {clear}"


def test_single_hot_pixels_are_not_stars():
    """On a spaced grid, so nothing pairs up by accident and the assertion is
    about the rule rather than about the random seed."""
    img = np.full((300, 400), 1500, dtype=np.float32)
    img[10::10, 10::10] = 60000
    assert star_count(np.clip(img, 0, 65535).astype(np.uint16)) == 0


def test_big_blobs_are_not_stars():
    """A droplet, a distant floodlight, the moon. All bright, none a star."""
    img = np.full((300, 400), 1500, dtype=np.float32)
    for cx in range(40, 380, 60):
        cv2.circle(img, (cx, 150), 12, 40000, -1)
    assert star_count(np.clip(img, 0, 65535).astype(np.uint16)) == 0


def test_the_mosaic_checkerboard_is_not_counted():
    """Adjacent pixels in a Bayer mosaic are different colour channels, so a
    mosaic carries its own checkerboard. Demosaicing first is what stopped that
    texture being the bulk of the count."""
    from skylapse.daemon.drivers.base import BayerPattern
    rng = np.random.default_rng(3)
    img = rng.normal(1500, 60, (300, 400)).astype(np.float32)
    img[0::2, 0::2] *= 1.8            # a strong per-channel imbalance
    img[1::2, 1::2] *= 0.6
    mosaic = np.clip(img, 0, 65535).astype(np.uint16)
    assert star_count(mosaic, BayerPattern.RGGB) == 0


def test_overlay_modifies_the_corner():
    img = np.zeros((200, 400, 3), dtype=np.uint8)
    out = burn_overlay(img.copy(), 1786651031.0, 30_000_000, 150, 12.5)
    assert out[170:, :250].sum() > 0            # text landed bottom-left
    assert (out[:100] == 0).all()               # sky untouched


def test_kp_threshold_tracks_latitude():
    assert aurora.kp_threshold(65.0) == 3       # Fairbanks: easy
    assert aurora.kp_threshold(45.5) == 6       # Amberg
    assert aurora.kp_threshold(42.7) == 7       # Racine
    assert aurora.kp_threshold(-45.5) == 6      # southern hemisphere symmetric
    assert aurora.kp_threshold(25.0) == 8       # Miami: good luck


def _cfg(lat):
    c = config.Config()
    c.location.latitude = lat
    c.notifications.enabled = True
    c.notifications.ntfy_topic = "t"
    return c


@mock.patch("skylapse.daemon.aurora.notify.notify", return_value=True)
@mock.patch("skylapse.daemon.aurora.fetch_current_kp", return_value=7.0)
def test_alert_fires_once_per_episode(kp, note):
    cfg = _cfg(42.7)
    alerted, _ = aurora.check(cfg, "night", already_alerted=False)
    assert alerted and note.call_count == 1
    alerted, _ = aurora.check(cfg, "night", already_alerted=alerted)
    assert alerted and note.call_count == 1     # latched: no repeat
    kp.return_value = 3.0                       # storm subsides
    alerted, _ = aurora.check(cfg, "night", already_alerted=alerted)
    assert not alerted                          # re-armed for next episode


@mock.patch("skylapse.daemon.aurora.notify.notify", return_value=True)
@mock.patch("skylapse.daemon.aurora.fetch_current_kp", return_value=9.0)
def test_no_alert_in_daylight(kp, note):
    alerted, _ = aurora.check(_cfg(42.7), "day", already_alerted=False)
    assert not alerted and note.call_count == 0


@mock.patch("skylapse.daemon.aurora.fetch_current_kp", return_value=None)
def test_fetch_failure_is_silent(kp):
    alerted, kp_val = aurora.check(_cfg(42.7), "night", already_alerted=False)
    assert not alerted and kp_val is None


# -- what the second real night taught it ------------------------------------

def test_a_sky_that_needed_no_exposure_has_no_stars():
    """Twilight, in one line. A sky correctly exposed in a fiftieth of a second
    at unity gain has the sun just below the horizon and nothing in it is a
    star — whatever the pixels look like. Reported 221 on such a frame."""
    assert star_count(sky(300), exposure_us=50_000, gain=1) == 0


def test_a_dark_sky_is_counted_normally():
    """Half a minute at gain fifteen is a genuinely dark sky."""
    assert star_count(sky(120), exposure_us=34_000_000, gain=15) > 0


def test_the_glare_gate_is_relative_not_absolute():
    """The bug this replaced: the gate was an absolute background level, and
    auto-exposure normalises every frame to the same mean — so it stopped
    saying anything about darkness. A well-exposed 2 AM frame scored 9 while a
    dim twilight frame scored 221, purely because the good frame was brighter.

    Here the same star field is rendered at two exposure levels. The count must
    not collapse simply because the frame as a whole is brighter.
    """
    dim = star_count(sky(100, background=1200), exposure_us=30_000_000, gain=15)
    bright = star_count(sky(100, background=24000), exposure_us=30_000_000, gain=15)
    assert bright >= dim * 0.5, f"bright frame lost its stars: {bright} vs {dim}"


def test_exposure_and_gain_are_optional():
    """Callers without metadata — the tests above, and anything analysing a
    saved frame — still get a count rather than a crash or a silent zero."""
    assert star_count(sky(100)) > 0
