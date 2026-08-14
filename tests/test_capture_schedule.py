"""Capture schedule: night_only skips daylight, always never does.

The twilight cases are the ones worth pinning. Twilight uses the night profile
and is when the interesting sky starts, so treating it as day would cut off
exactly what an allsky camera is set up to catch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skylapse.config import CameraEntry, Config
from skylapse.daemon.scheduler import next_dusk, should_capture


@pytest.mark.parametrize("schedule,current,expected", [
    ("night_only", "day", False),        # the whole point of the setting
    ("always", "day", True),
    ("night_only", "twilight", True),    # twilight is night's business
    ("always", "twilight", True),
    ("night_only", "night", True),
    ("always", "night", True),
])
def test_should_capture(schedule, current, expected):
    cam = CameraEntry(capture_schedule=schedule)
    assert should_capture(cam, current) is expected


def test_default_schedule_captures_around_the_clock():
    """A camera that has never been configured must not silently skip the day."""
    assert CameraEntry().capture_schedule == "always"
    assert should_capture(CameraEntry(), "day") is True


def test_unknown_schedule_falls_back_to_capturing():
    """A typo in config must not quietly stop a rig capturing."""
    assert should_capture(CameraEntry(capture_schedule="nonsense"), "day") is True


def test_next_dusk_is_in_the_future():
    cfg = Config()
    cfg.location.latitude, cfg.location.longitude = 42.73, -87.78
    now = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
    dusk = next_dusk(cfg, now)
    assert dusk is not None and dusk > now


def test_next_dusk_rolls_to_tomorrow_after_sunset():
    cfg = Config()
    cfg.location.latitude, cfg.location.longitude = 42.73, -87.78
    just_after_sunset = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)
    dusk = next_dusk(cfg, just_after_sunset)
    assert dusk is not None and dusk > just_after_sunset


def test_next_dusk_handles_polar_day():
    """Above the Arctic circle in midsummer the sun never sets; say so rather
    than inventing a time."""
    cfg = Config()
    cfg.location.latitude, cfg.location.longitude = 78.2, 15.6   # Svalbard
    midsummer = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    assert next_dusk(cfg, midsummer) is None
