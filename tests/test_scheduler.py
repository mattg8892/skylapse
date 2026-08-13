"""Manual exposure override: the tracker use case. AE must never touch it."""
from skylapse.config import CaptureProfile
from skylapse.daemon.scheduler import next_exposure


def manual_30s():
    return CaptureProfile(auto_exposure=False, exposure_us=30_000_000, gain=150,
                          gap_s=10)


def test_manual_mode_returns_exact_settings():
    exp, gain = next_exposure(manual_30s(), last_mean_brightness=200.0,
                              current_exposure_us=1_000_000, current_gain=50)
    assert (exp, gain) == (30_000_000, 150)


def test_manual_mode_ignores_extreme_brightness():
    # Blown-out or black frames must not budge a locked exposure.
    for brightness in (0.0, 1.0, 254.9, None):
        exp, gain = next_exposure(manual_30s(), brightness, 5_000_000, 300)
        assert (exp, gain) == (30_000_000, 150)


def test_auto_mode_still_adapts():
    p = CaptureProfile(auto_exposure=True, target_brightness=90,
                       max_exposure_us=25_000_000)
    exp, _ = next_exposure(p, last_mean_brightness=30.0,
                           current_exposure_us=1_000_000, current_gain=100)
    assert exp > 1_000_000            # underexposed -> AE pushes up


def test_auto_respects_exposure_ceiling_and_spills_to_gain():
    p = CaptureProfile(auto_exposure=True, target_brightness=90,
                       max_exposure_us=10_000_000, max_gain=300)
    exp, gain = next_exposure(p, last_mean_brightness=10.0,
                              current_exposure_us=9_000_000, current_gain=100)
    assert exp == 10_000_000          # clamped at ceiling
    assert gain > 100                 # remainder pushed into gain


# -- manual safety stop ------------------------------------------------------

from skylapse.daemon.scheduler import SAFETY_BRIGHT_FRAMES, safety_should_stop


def test_safety_never_trips_in_auto_mode():
    p = CaptureProfile(auto_exposure=True)
    assert safety_should_stop(p, "day", 99) is None


def test_safety_checkbox_off_never_trips():
    p = CaptureProfile(auto_exposure=False, manual_safety_stop=False)
    assert safety_should_stop(p, "day", 99) is None


def test_safety_trips_on_daylight():
    p = CaptureProfile(auto_exposure=False, manual_safety_stop=True)
    assert safety_should_stop(p, "day", 0) == "daylight"


def test_safety_trips_after_consecutive_bright_frames():
    p = CaptureProfile(auto_exposure=False, manual_safety_stop=True)
    assert safety_should_stop(p, "night", SAFETY_BRIGHT_FRAMES - 1) is None
    assert safety_should_stop(p, "night", SAFETY_BRIGHT_FRAMES) == "bright_frames"


def test_safety_quiet_at_night_with_normal_frames():
    p = CaptureProfile(auto_exposure=False, manual_safety_stop=True)
    assert safety_should_stop(p, "night", 0) is None
    assert safety_should_stop(p, "twilight", 0) is None
