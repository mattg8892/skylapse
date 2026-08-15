"""Stall watchdog.

Every case here is one that is miserable to stage on hardware — a camera that
wedges at 3am, a rig legitimately idle through the day — and trivial to assert
on directly. The false-positive cases matter as much as the true ones: an
alert that fires when the software is working as designed is an alert people
learn to ignore.
"""
from __future__ import annotations

import pytest

from skylapse.daemon import watchdog
from skylapse.daemon.watchdog import StallWatch, stall_threshold_s

NIGHT = {"gap_s": 5, "exposure_us": 20_000_000}      # 25s cadence -> 75s threshold


def test_threshold_scales_with_cadence():
    fast = stall_threshold_s(gap_s=1, exposure_us=100_000)      # ~1.1s cadence
    slow = stall_threshold_s(gap_s=10, exposure_us=30_000_000)  # 40s cadence
    assert slow > fast
    assert slow == pytest.approx(120.0)


def test_threshold_has_a_floor():
    """A rig on fast frames must not alert on one slow save."""
    assert stall_threshold_s(gap_s=0, exposure_us=1_000) == watchdog.MIN_STALL_S


def test_stall_during_the_night_fires():
    w = StallWatch()
    age = w.check(state="capturing", now=1000.0, last_frame_at=1000.0 - 300, **NIGHT)
    assert age == pytest.approx(300.0)


def test_a_normal_gap_does_not_fire():
    w = StallWatch()
    assert w.check(state="capturing", now=1000.0,
                   last_frame_at=1000.0 - 30, **NIGHT) is None


def test_only_one_alert_per_episode():
    """A stall lasts hours; alerting every loop teaches people to mute it."""
    w = StallWatch()
    assert w.check(state="capturing", now=1000.0,
                   last_frame_at=700.0, **NIGHT) is not None
    for extra in (10, 60, 600):
        assert w.check(state="capturing", now=1000.0 + extra,
                       last_frame_at=700.0, **NIGHT) is None


@pytest.mark.parametrize("state", sorted(watchdog.QUIET_STATES))
def test_quiet_states_never_fire(state):
    """idle_day, paused_safety and focusing are the software working. no_camera
    is already covered by the reopen loop's own notification."""
    w = StallWatch()
    assert w.check(state=state, now=1e6, last_frame_at=0.0, **NIGHT) is None
    assert w.alerted is False, f"{state} latched an alert it never sent"


def test_idle_day_never_fires_even_after_hours():
    w = StallWatch()
    assert w.check(state="idle_day", now=1000.0 + 12 * 3600,
                   last_frame_at=1000.0, **NIGHT) is None


def test_paused_safety_never_fires():
    w = StallWatch()
    assert w.check(state="paused_safety", now=1000.0 + 6 * 3600,
                   last_frame_at=1000.0, **NIGHT) is None


def test_no_frames_yet_does_not_fire():
    """A daemon that has not captured anything yet is starting up, not stalled."""
    w = StallWatch()
    assert w.check(state="capturing", now=1e6, last_frame_at=0.0, **NIGHT) is None


def test_recovery_rearms_and_reports_once():
    w = StallWatch()
    assert w.check(state="capturing", now=1000.0, last_frame_at=700.0, **NIGHT)
    assert w.frame_written() is True, "no recovery notice after a stall"
    assert w.frame_written() is False, "recovery notice sent more than once"
    # Re-armed: a second stall alerts again.
    assert w.check(state="capturing", now=2000.0,
                   last_frame_at=1700.0, **NIGHT) is not None


def test_an_alert_from_the_reopen_loop_still_gets_an_all_clear():
    """Regression from a real unplug: the reopen loop raised camera_offline,
    the camera came back, and no recovery notice was ever sent — because only
    the watchdog's own alerts armed the latch. Every alert needs an all-clear.
    """
    w = StallWatch()
    w.mark_alerted()                       # as the reopen loop now does
    assert w.frame_written() is True, "no all-clear after a reopen-loop alert"
    assert w.frame_written() is False, "all-clear sent more than once"


def test_mark_alerted_suppresses_a_duplicate_watchdog_alert():
    """If the reopen loop already said something, the watchdog must not pile on."""
    w = StallWatch()
    w.mark_alerted()
    assert w.check(state="capturing", now=1e6, last_frame_at=1.0, **NIGHT) is None


def test_frames_without_a_stall_say_nothing():
    """Normal operation must not emit a recovery notice on every frame."""
    w = StallWatch()
    assert all(w.frame_written() is False for _ in range(5))


@pytest.mark.parametrize("age,expected", [
    (45, "45s"), (480, "8m"), (7200, "2.0h"),
])
def test_describe_is_readable(age, expected):
    assert watchdog.describe(age) == expected


def test_the_daemon_measures_stalls_on_a_monotonic_clock(monkeypatch):
    """Regression: a Pi has no battery-backed RTC, so it boots with a stale
    clock that NTP steps forward — 15.8 hours on this rig after an overnight
    power-off. Measured against wall time that step read as 15.8 hours of
    silence, and every power-cycle would send a false stall alert.
    """
    import time
    from skylapse.daemon.main import CaptureDaemon

    daemon = CaptureDaemon()
    seen = {}

    class Spy:
        def check(self, **kw):
            seen.update(kw)
            return None

    daemon.stall = Spy()
    daemon.last_frame_monotonic = time.monotonic()

    class Profile:
        gap_s, exposure_us = 5, 1_000_000

    daemon._check_for_stall(Profile())

    # Monotonic and wall clock are wildly different magnitudes; asserting on
    # that distinguishes them without depending on either exact value.
    assert abs(seen["now"] - time.monotonic()) < 5
    assert abs(seen["now"] - time.time()) > 1000, "stall is measured on wall time"
