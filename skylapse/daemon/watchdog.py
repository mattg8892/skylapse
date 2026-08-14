"""Stall detection: notice when capture has quietly stopped.

The failure this exists for is the silent one. systemd is happy, the process
is alive, the dashboard shows a frame — and it is the same frame it showed an
hour ago, because the camera wedged or the USB link dropped mid-night. Nothing
else in the system notices, because nothing else is looking for an *absence*.

Deliberately pure and latching:

- **Pure**, because the interesting cases (stalled at 3am, idle by design,
  paused for safety) are miserable to reproduce on hardware and trivial to
  assert on directly.
- **Latching**, because a stall lasts hours. One alert per episode, one
  recovery notice when frames resume. A notifier that fires every loop
  iteration through the night teaches you to mute it.
"""
from __future__ import annotations

STALL_FACTOR = 3.0      # multiples of the expected cadence before alerting
MIN_STALL_S = 60.0      # floor: a fast cadence must not alert on one hiccup

# States where producing no frames is correct. Alerting in any of these would
# be crying wolf about the software working as designed.
#
# `no_camera` is here for a different reason: the reopen loop already sends
# camera_offline with a better-informed message, so alerting again would just
# double up on the same event.
QUIET_STATES = frozenset({"idle_day", "paused_safety", "focusing", "no_camera"})


def expected_cadence_s(gap_s: float, exposure_us: float) -> float:
    """How long one frame *should* take: the exposure plus the configured gap."""
    return max(1.0, float(gap_s) + float(exposure_us) / 1_000_000.0)


def stall_threshold_s(gap_s: float, exposure_us: float,
                      factor: float = STALL_FACTOR,
                      floor: float = MIN_STALL_S) -> float:
    """Silence long enough to mean something is wrong.

    Scaled to the cadence because a 25s-exposure night and a 100ms daytime
    frame have wildly different notions of "too long", with a floor so a rig
    running back-to-back short frames does not alert on a single slow save.
    """
    return max(floor, factor * expected_cadence_s(gap_s, exposure_us))


def describe(age_s: float) -> str:
    minutes = age_s / 60.0
    if minutes < 1:
        return f"{int(age_s)}s"
    if minutes < 60:
        return f"{int(minutes)}m"
    return f"{minutes / 60:.1f}h"


class StallWatch:
    """Latching stall detector for the capture loop."""

    def __init__(self) -> None:
        self.alerted = False

    def frame_written(self) -> bool:
        """Record a captured frame.

        Returns True exactly once per episode — when a frame arrives after an
        alert — so the caller can send the recovery notice and nothing else.
        """
        if self.alerted:
            self.alerted = False
            return True
        return False

    def check(self, *, state: str, now: float, last_frame_at: float,
              gap_s: float, exposure_us: float) -> float | None:
        """Return the stall age in seconds if this is the moment to alert.

        None means "say nothing": either capture is legitimately quiet, or the
        gap is still within tolerance, or we have already alerted about this
        episode.
        """
        if state in QUIET_STATES:
            return None
        if self.alerted:                       # one alert per episode
            return None
        if not last_frame_at:                  # nothing captured yet this run
            return None
        age = now - last_frame_at
        if age < stall_threshold_s(gap_s, exposure_us):
            return None
        self.alerted = True
        return age
