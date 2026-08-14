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


def test_frames_without_a_stall_say_nothing():
    """Normal operation must not emit a recovery notice on every frame."""
    w = StallWatch()
    assert all(w.frame_written() is False for _ in range(5))


@pytest.mark.parametrize("age,expected", [
    (45, "45s"), (480, "8m"), (7200, "2.0h"),
])
def test_describe_is_readable(age, expected):
    assert watchdog.describe(age) == expected
