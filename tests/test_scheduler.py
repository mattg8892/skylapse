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


# -- AE headroom -------------------------------------------------------------

class _Pinned:
    """Just the attributes _check_ae_headroom touches, so the check can be
    exercised without standing up a camera."""
    def __init__(self, exposure_us, gain, brightness=40.0):
        self.exposure_us = exposure_us
        self.gain = gain
        self.last_brightness = brightness
        self.ae_pinned = 0
        self.ae_pinned_said = False


def _profile(max_exposure_us=25_000_000, max_gain=22):
    from skylapse.config import CaptureProfile
    return CaptureProfile(auto_exposure=True, target_brightness=90,
                          max_exposure_us=max_exposure_us, max_gain=max_gain)


def _run(loop, profile, frames):
    from skylapse.daemon.main import CaptureDaemon
    for _ in range(frames):
        CaptureDaemon._check_ae_headroom(loop, profile)
    return loop


def test_ae_at_both_ceilings_is_flagged_after_three_frames():
    """The night of 2026-08-17 ran pinned at gain 22 on a module whose ceiling
    is 22, for hours, and said nothing at all."""
    from skylapse.daemon.main import AE_PINNED_FRAMES
    loop = _Pinned(25_000_000, 22)
    _run(loop, _profile(), 2)
    assert loop.ae_pinned < AE_PINNED_FRAMES
    _run(loop, _profile(), 1)
    assert loop.ae_pinned >= AE_PINNED_FRAMES


def test_one_dark_frame_is_not_an_episode():
    """A cloud crossing is not the same as running out of exposure."""
    from skylapse.daemon.main import AE_PINNED_FRAMES
    loop = _Pinned(25_000_000, 22)
    _run(loop, _profile(), 1)
    loop.exposure_us = 12_000_000                    # AE found room again
    _run(loop, _profile(), 1)
    assert loop.ae_pinned == 0 and loop.ae_pinned_said is False


def test_headroom_in_either_control_is_not_pinned():
    """Both ceilings, or it is not out of road."""
    _run(exposure := _Pinned(25_000_000, 10), _profile(), 5)
    _run(gain := _Pinned(9_000_000, 22), _profile(), 5)
    assert exposure.ae_pinned == 0 and gain.ae_pinned == 0


def test_it_says_so_once_per_episode(caplog):
    """Once. Not once per frame for the rest of the night."""
    loop = _Pinned(25_000_000, 22)
    with caplog.at_level("INFO", logger="skylapse.daemon"):
        _run(loop, _profile(), 40)
    said = [r for r in caplog.records if "AE at limits" in r.getMessage()]
    assert len(said) == 1, f"logged {len(said)} times"


def test_manual_exposure_never_reports_pinned():
    """Manual mode is a choice, not a limit that has been hit."""
    from skylapse.config import CaptureProfile
    manual = CaptureProfile(auto_exposure=False, max_exposure_us=25_000_000,
                            max_gain=22)
    loop = _Pinned(25_000_000, 22)
    _run(loop, manual, 5)
    assert loop.ae_pinned == 0


# -- picking a target for someone who should not have to ---------------------

def test_a_pinned_rig_is_given_a_target_it_can_reach():
    """The night that prompted this: aiming at 90, reaching 41, pinned at both
    ceilings for hours. Aiming AT 41 would leave it pinned exactly where it
    already is, so it aims under."""
    from skylapse.daemon.scheduler import suggest_target
    target, why = suggest_target(41.0, at_limits=True, current_target=90)
    assert target < 41
    assert "41" in why and str(target) in why


def test_a_rig_that_is_coping_is_left_alone():
    """A working number is not improved by being nudged."""
    from skylapse.daemon.scheduler import suggest_target
    target, why = suggest_target(88.0, at_limits=False, current_target=90)
    assert target == 90 and "nothing to change" in why


def test_it_never_suggests_a_target_of_near_black():
    """A cloudy, moonless frame measures almost nothing. Chasing that would set
    a target auto-exposure can hit with the lens cap on."""
    from skylapse.daemon.scheduler import MIN_USEFUL_TARGET, suggest_target
    target, _ = suggest_target(2.0, at_limits=True, current_target=90)
    assert target == MIN_USEFUL_TARGET


def test_it_declines_without_a_measurement():
    from skylapse.daemon.scheduler import suggest_target
    target, why = suggest_target(None, at_limits=True, current_target=90)
    assert target == 90 and "no frame" in why


def test_it_does_not_raise_a_target_that_is_only_just_out_of_reach():
    """Pinned, but nearly there — lowering it would cost real brightness for a
    problem that is about to solve itself as the sky darkens."""
    from skylapse.daemon.scheduler import suggest_target
    target, why = suggest_target(89.0, at_limits=True, current_target=90)
    assert target == 90 and "leave alone" in why
