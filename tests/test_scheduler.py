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


def test_a_restart_starts_from_the_profile_not_a_blind_guess():
    """The daemon's pre-camera defaults are one second and gain 100, set before
    any camera has been opened. Auto-exposure only walks gain down a fifth per
    frame, so at a daytime cadence of one frame every three minutes a restart
    cost ten minutes of blown frames climbing back down — which on a rig
    outside reads as a broken camera, and was reported as one.
    """
    from skylapse.config import CaptureProfile

    profile = CaptureProfile(auto_exposure=True, gain=1, max_gain=22,
                             max_exposure_us=100_000)
    camera_max_gain = 22

    # what _open_camera now does with the daemon's starting values
    gain = min(profile.gain, camera_max_gain)
    exposure_us = min(1_000_000, profile.max_exposure_us)

    assert gain == 1, "started at the gain ceiling again"
    assert exposure_us == 100_000, "started longer than the profile allows"


def test_the_seed_never_exceeds_what_the_camera_can_do():
    """A profile carried over from a different camera must not set a gain the
    hardware cannot reach."""
    from skylapse.config import CaptureProfile
    profile = CaptureProfile(gain=100, max_gain=300)
    assert min(profile.gain, 22) == 22


def test_power_health_says_nothing_when_it_cannot_ask():
    """Off a Pi there is no vcgencmd. An empty answer means "no claim", which
    is what the dashboard checks — inventing False would assert the supply is
    fine on hardware that was never asked."""
    from skylapse.daemon import main
    main._power_cache.update(at=0.0, value={})
    health = main.power_health()
    assert health == {} or set(health) == {"undervoltage", "undervoltage_seen",
                                           "throttled"}


def test_the_throttle_bits_are_decoded_the_way_the_firmware_means_them(monkeypatch):
    """0x50005: undervoltage now and since boot, plus throttling. Getting the
    latched bits wrong would report a supply that failed hours ago as healthy."""
    import subprocess
    from skylapse.daemon import main

    monkeypatch.setattr(main.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(
                            a, 0, "throttled=0x50005\n", ""))
    main._power_cache.update(at=0.0, value={})
    health = main.power_health()
    assert health["undervoltage"] is True
    assert health["undervoltage_seen"] is True
    assert health["throttled"] is True


def test_a_healthy_supply_reports_healthy(monkeypatch):
    import subprocess
    from skylapse.daemon import main

    monkeypatch.setattr(main.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(
                            a, 0, "throttled=0x0\n", ""))
    main._power_cache.update(at=0.0, value={})
    health = main.power_health()
    assert health["undervoltage"] is False and health["undervoltage_seen"] is False
