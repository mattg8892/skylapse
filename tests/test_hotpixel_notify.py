"""Hot-pixel map: defects stand still, stars move — prove detect + correct.
Notifications: the master switch and per-event toggles gate everything."""
from unittest import mock

import numpy as np
import pytest

from skylapse import config, notify
from skylapse.daemon.pipeline.hotpixel import MIN_FRAMES, HotPixelMap, exposure_bucket


# -- hot pixels --------------------------------------------------------------

HOT = [(50, 60), (120, 200), (10, 10)]


def synthetic_frame(rng, star_shift):
    """Dim sky + moving 'stars' + stuck-high defects at fixed coordinates."""
    arr = rng.normal(2000, 100, (240, 320)).astype(np.float64)
    for i in range(15):                                  # stars drift each frame
        y = (20 + i * 14 + star_shift) % 240
        x = (30 + i * 20 + star_shift * 2) % 320
        arr[y, x] += 30000
    for y, x in HOT:
        arr[y, x] = 60000                                # defects never move
    return np.clip(arr, 0, 65535).astype(np.uint16)


def test_detects_defects_not_stars(tmp_path):
    rng = np.random.default_rng(7)
    hp = HotPixelMap(tmp_path)
    for i in range(MIN_FRAMES):
        hp.observe(synthetic_frame(rng, star_shift=i * 3), exposure_us=5_000_000)
    mask = hp._maps["1-8s"]
    for y, x in HOT:
        assert mask[y, x], f"missed defect at {(y, x)}"
    # Stars moved every frame, so the min-stack swept them away.
    assert mask.sum() <= len(HOT) + 2                    # near-zero false positives


def test_correction_replaces_defects_with_neighbors(tmp_path):
    rng = np.random.default_rng(7)
    hp = HotPixelMap(tmp_path)
    for i in range(MIN_FRAMES):
        hp.observe(synthetic_frame(rng, star_shift=i * 3), exposure_us=5_000_000)
    frame = synthetic_frame(rng, star_shift=99)
    fixed = hp.correct(frame, exposure_us=5_000_000)
    for y, x in HOT:
        assert frame[y, x] == 60000                      # original untouched
        assert fixed[y, x] < 5000                        # defect now looks like sky


def test_no_map_means_no_change(tmp_path):
    hp = HotPixelMap(tmp_path)
    frame = np.full((10, 10), 1000, dtype=np.uint16)
    assert hp.correct(frame, exposure_us=1_000_000) is frame


def test_map_persists_across_restart(tmp_path):
    rng = np.random.default_rng(7)
    hp = HotPixelMap(tmp_path)
    for i in range(MIN_FRAMES):
        hp.observe(synthetic_frame(rng, star_shift=i * 3), exposure_us=5_000_000)
    hp2 = HotPixelMap(tmp_path)                          # fresh instance, loads disk
    assert "1-8s" in hp2._maps
    assert hp2._maps["1-8s"][50, 60]


def test_exposure_buckets():
    assert exposure_bucket(500_000) == "sub1s"
    assert exposure_bucket(5_000_000) == "1-8s"
    assert exposure_bucket(45_000_000) == "30s+"


# -- notifications -----------------------------------------------------------

def _cfg(enabled, topic="skylapse-test", aurora=True):
    c = config.Config()
    c.notifications.enabled = enabled
    c.notifications.ntfy_topic = topic
    c.notifications.events["aurora"] = aurora
    return c


@mock.patch("skylapse.notify._post_ntfy", return_value=True)
def test_master_switch_off_silences_everything(post, ):
    assert notify.notify("aurora", "t", "b", _cfg(enabled=False)) is False
    post.assert_not_called()


@mock.patch("skylapse.notify._post_ntfy", return_value=True)
def test_event_toggle_off_blocks_that_event(post):
    assert notify.notify("aurora", "t", "b", _cfg(True, aurora=False)) is False
    post.assert_not_called()


@mock.patch("skylapse.notify._post_ntfy", return_value=True)
def test_enabled_event_sends(post):
    assert notify.notify("aurora", "t", "b", _cfg(True)) is True
    post.assert_called_once()


@mock.patch("skylapse.notify._post_ntfy", return_value=True)
def test_no_topic_never_sends(post):
    assert notify.notify("aurora", "t", "b", _cfg(True, topic="")) is False
    post.assert_not_called()
