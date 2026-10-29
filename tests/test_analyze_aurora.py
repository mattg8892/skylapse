"""Overlay burns text; aurora thresholds map to latitude and alerts fire once
per episode, never in daylight."""
from unittest import mock

import cv2
import numpy as np

from skylapse import config
from skylapse.daemon import aurora
from skylapse.daemon.pipeline.analyze import burn_overlay


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

