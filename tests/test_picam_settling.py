"""Waiting for the sensor to apply new settings, and when to stop waiting.

libcamera applies a control change several frames later and keeps serving
frames exposed at the old settings meanwhile, so the driver discards frames
until the metadata reflects what was asked for. That is necessary. What it must
not do is wait for an exactness the hardware cannot deliver — at a 25 second
exposure each discarded frame is 25 seconds of a night.
"""
from __future__ import annotations

from skylapse.daemon.drivers import picam


class _Driver(picam.PiCamDriver):
    """Just the settle check, without a camera behind it."""

    def __init__(self, exposure_us, gain):
        self._exposure_us = exposure_us
        self._gain_value = gain


def test_a_quantised_gain_still_counts_as_settled():
    """The bug that cost about 13% of a night. Analogue gain is quantised in
    hardware: ask for 17.0 and the sensor reports what it could actually make.
    The tolerance was a flat 0.05 — a third of a percent at that gain — so it
    never matched, the loop discarded frames until it gave up, and every gain
    change through the night cost the full timeout.
    """
    driver = _Driver(25_000_000, 17.0)
    for reported in (16.7, 16.9, 17.0, 17.2, 17.3):
        assert driver._settled({"ExposureTime": 25_000_000,
                                "AnalogueGain": reported}), \
            f"sensor reported {reported} for a requested 17.0 and it was refused"


def test_a_gain_that_really_is_wrong_is_still_refused():
    """A tolerance wide enough to accept anything would defeat the point: a
    frame exposed at the previous settings must not be filed under the new
    ones."""
    driver = _Driver(25_000_000, 17.0)
    assert not driver._settled({"ExposureTime": 25_000_000, "AnalogueGain": 8.0})
    assert not driver._settled({"ExposureTime": 12_000_000, "AnalogueGain": 17.0})


def test_the_tolerance_scales_with_the_value():
    """At unity gain a tenth is enormous; at gain 22 it is nothing. A fixed
    number is wrong at one end or the other, and it was wrong at the end this
    camera spends its nights in."""
    tight = _Driver(1_000_000, 1.0)
    loose = _Driver(1_000_000, 20.0)
    assert not tight._settled({"ExposureTime": 1_000_000, "AnalogueGain": 1.5})
    assert loose._settled({"ExposureTime": 1_000_000, "AnalogueGain": 20.3})


def test_missing_metadata_is_not_treated_as_unsettled():
    """Nothing to compare against means take the frame, not wait forever."""
    driver = _Driver(25_000_000, 17.0)
    assert driver._settled({})


def test_small_changes_do_not_start_a_settle_at_all():
    """The other half: a nudge from gain 14 to 15 produces a frame
    indistinguishable from the one being waited for."""
    assert picam.SETTLE_WORTH_WAITING > 0
    change = abs(15 - 14) / 14
    assert change < picam.SETTLE_WORTH_WAITING, "a one-step gain nudge would wait"
