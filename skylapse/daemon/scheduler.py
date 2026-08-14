"""Day/night scheduling from sun altitude, plus the auto-exposure loop.

Both are pure functions over inputs so they're trivially unit-testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from astral import LocationInfo
from astral.sun import elevation

from ..config import CameraEntry, CaptureProfile, Config

# Sun altitude thresholds (degrees). Between them = twilight; we bias twilight
# toward the night profile since that's when interesting sky starts.
DAY_ABOVE = 0.0
NIGHT_BELOW = -9.0


def period(cfg: Config, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    loc = LocationInfo(latitude=cfg.location.latitude,
                       longitude=cfg.location.longitude)
    alt = elevation(loc.observer, when)
    if alt >= DAY_ABOVE:
        return "day"
    if alt <= NIGHT_BELOW:
        return "night"
    return "twilight"


def profile_for(cfg: Config, cam: CameraEntry,
                when: datetime | None = None) -> CaptureProfile:
    p = period(cfg, when)
    return cam.day if p == "day" else cam.night   # twilight uses night profile


def should_capture(cam: CameraEntry, current_period: str) -> bool:
    """Whether the capture schedule allows a frame right now.

    `night_only` skips daylight for allsky rigs whose owners do not want a
    day's worth of blue-sky frames filling the card. Twilight still captures:
    it is when the interesting sky starts, and it already uses the night
    profile, so treating it as day would cut off exactly the part people set an
    allsky camera up for.
    """
    if cam.capture_schedule != "night_only":
        return True
    return current_period != "day"


def next_dusk(cfg: Config, when: datetime | None = None) -> datetime | None:
    """When the sun next drops below the horizon.

    This is what "idle until dusk" means, so it matches the same threshold
    `period()` uses to stop calling it day. None inside the polar circles,
    where the sun may not set at all — the UI says "when the sun sets" instead
    of inventing a time.
    """
    from astral.sun import sunset

    when = when or datetime.now(timezone.utc)
    loc = LocationInfo(latitude=cfg.location.latitude,
                       longitude=cfg.location.longitude)
    for offset in (0, 1):
        try:
            candidate = sunset(loc.observer,
                               (when + timedelta(days=offset)).date(),
                               tzinfo=timezone.utc)
        except ValueError:
            continue           # sun does not cross the horizon on that date
        if candidate > when:
            return candidate
    return None


# Manual-mode safety stop: trip on daylight or repeated near-saturation.
SAFETY_BRIGHT_LEVEL = 235.0      # mean 0-255; ~92% of full scale
SAFETY_BRIGHT_FRAMES = 3         # consecutive frames required to trip


def safety_should_stop(profile: CaptureProfile, current_period: str,
                       consecutive_bright: int) -> str | None:
    """Returns a trip reason ('daylight' | 'bright_frames') or None.
    Never trips in auto-exposure mode or with the checkbox off."""
    if profile.auto_exposure or not profile.manual_safety_stop:
        return None
    if current_period == "day":
        return "daylight"
    if consecutive_bright >= SAFETY_BRIGHT_FRAMES:
        return "bright_frames"
    return None


def next_exposure(profile: CaptureProfile, last_mean_brightness: float | None,
                  current_exposure_us: int, current_gain: int) -> tuple[int, int]:
    """One auto-exposure step: nudge exposure (then gain) toward the target
    mean brightness. Multiplicative steps, clamped, damped to avoid oscillation.
    """
    if not profile.auto_exposure or last_mean_brightness is None:
        if not profile.auto_exposure:
            return profile.exposure_us, profile.gain
        return current_exposure_us, current_gain

    target = float(profile.target_brightness)
    measured = max(1.0, last_mean_brightness)
    ratio = target / measured
    # Damp: apply only 60% of the correction per frame, capped at 4x per step.
    ratio = min(4.0, max(0.25, 1.0 + (ratio - 1.0) * 0.6))

    exposure = int(current_exposure_us * ratio)
    gain = current_gain

    if exposure > profile.max_exposure_us:
        # Out of exposure headroom: push the remainder into gain.
        spill = exposure / profile.max_exposure_us
        exposure = profile.max_exposure_us
        gain = min(profile.max_gain, max(1, int(current_gain * spill)))
    elif exposure < profile.max_exposure_us * 0.5 and gain > profile.gain:
        # Plenty of exposure headroom: walk gain back down toward baseline first.
        gain = max(profile.gain, int(current_gain * 0.8))

    exposure = max(100, exposure)
    return exposure, gain
