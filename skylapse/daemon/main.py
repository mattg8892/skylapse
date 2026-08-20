"""Capture daemon. Runs forever; network state is irrelevant here by design.

Loop: pick profile (sun altitude) -> set controls -> capture -> save JPEG
(+DNG per policy) -> update auto-exposure from measured brightness -> sleep
out the remainder of the interval.
"""
from __future__ import annotations

import collections
import json
import logging
import signal
import time

import numpy as np

from .. import config
from .drivers.base import CameraDriver, CameraError, Frame, detect_camera
from .pipeline import process
from .pipeline.hotpixel import HotPixelMap
from .focus import (DEFAULT_EXPOSURE_MS as FOCUS_DEFAULT_EXPOSURE_MS,
                    DEFAULT_GAIN as FOCUS_DEFAULT_GAIN,
                    TIMEOUT_S as FOCUS_TIMEOUT, FocusSession, sharpness)
from . import aurora
from .dewheater import DewHeater
from .pipeline.analyze import star_count
from .. import notify
from . import nightjobs
from .watchdog import StallWatch, describe as describe_age
from .scheduler import (SAFETY_BRIGHT_LEVEL, next_dusk, next_exposure, period,
                        profile_for, safety_should_stop, should_capture)

log = logging.getLogger("skylapse.daemon")

REOPEN_BACKOFF = (5, 15, 60)     # camera disconnect recovery, seconds
IDLE_POLL_S = 30                 # recheck cadence while a night_only camera
                                 # waits out the day — short enough that dusk
                                 # is picked up promptly, long enough to idle
COMMAND_POLL_S = 0.5             # how often a gap is interrupted to look for
                                 # UI commands; the bound on button latency
# Consecutive frames pinned at BOTH auto-exposure ceilings before it is worth
# saying so. Three, because one is a cloud crossing and three is the sky.
AE_PINNED_FRAMES = 3
# The shortest focus exposure worth asking for. Sensors floor this
# themselves; going lower just wastes a round trip.
FOCUS_MIN_EXPOSURE_US = 50
# Auto-exposure's gain floor. Unity: no amplification, the cleanest frame the
# sensor can give, and where a night should start before exposure runs out.
MIN_GAIN = 1
# Command files the UI drops in RUN_DIR. Seeing any of these ends a gap early.
COMMAND_FILES = ("focus_start", "focus_stop", "keeper_cmd", "resume_cmd",
                 "focus_cmd.json")


class CaptureDaemon:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.camera_id: str = ""
        self.driver: CameraDriver | None = None
        self.running = True
        self.last_brightness: float | None = None
        self.exposure_us = 1_000_000
        self.gain = 100
        # Rolling raw buffer for the "save RAW" keeper button (guarded by API).
        self.keeper_buffer: collections.deque[Frame] = collections.deque(maxlen=3)
        self.hotpixels = HotPixelMap(config.IMAGE_ROOT.parent / "calibration")
        self.last_period: str | None = None
        self.idle_day = False               # night_only camera waiting for dusk
        self.latest_path: str | None = None
        # Monotonic, NOT wall clock. A Pi has no battery-backed RTC, so it boots
        # with a stale time and NTP steps it forward — 15.8 hours, in the case
        # that found this. Measuring a stall against wall time turns every
        # power-cycle into a false "capture stalled" alert on someone's phone.
        self.last_frame_monotonic = 0.0
        self.stall = StallWatch()
        self.state = "starting"             # what the watchdog judges against
        self.consecutive_bright = 0
        # AE pinned at both ceilings: counted, so a single frame at the
        # limits does not raise anything, and latched so a long episode
        # says so once rather than every frame.
        self.ae_pinned = 0
        self.ae_pinned_said = False
        self.focus: FocusSession | None = None
        self.focus_started = 0.0
        self.focus_controls: tuple[int, int] | None = None
        self.focus_rebaselined_at = 0.0
        self.aurora_alerted = False
        self.aurora_last_poll = 0.0
        self.last_kp: float | None = None
        dh = self.cfg.dew_heater
        # Experimental gate: flag off -> the subsystem is never even built.
        self.dewheater = DewHeater(dh.gpio_pin, dh.on_margin_c,
                                   dh.off_margin_c) \
            if dh.experimental_enabled else None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self._open_camera()
        self._loop()

    def _stop(self, *_):
        self.running = False
        if self.dewheater:
            self.dewheater.off()          # heater never left on across exit

    def _open_camera(self) -> None:
        attempt = 0
        while self.running:
            try:
                # active_camera lets a rig with both a ZWO and a Pi module pick
                # which one is the imaging camera; empty means the probe order.
                self.driver = detect_camera(self.cfg.active_camera)
                info = self.driver.open()
                self.camera_id = info.camera_id
                # Register in the config-file camera registry (fetch-or-create),
                # filling identity fields on first sight of this hardware.
                cfg = config.load()
                entry = cfg.camera(self.camera_id)
                changed = False
                if not entry.model:
                    entry.driver, entry.model = info.driver, info.name
                    entry.label = entry.label or info.name
                    changed = True
                # Fit the profiles to what this hardware can actually do, on
                # every open rather than only on first sight. Profile defaults
                # are ZWO-shaped (gains in the hundreds); a Pi module stops at
                # 22, and AE spills into gain once exposure is capped, so an
                # unreachable ceiling means it keeps asking for more and never
                # sees brightness move. A limit beyond the sensor's capability
                # is not a preference the user expressed — it is impossible —
                # so clamping it is not overriding a choice. Only ever downward.
                for profile in (entry.day, entry.night):
                    for field, ceiling in (("max_gain", info.max_gain),
                                           ("max_exposure_us", info.max_exposure_us)):
                        if getattr(profile, field) > ceiling:
                            setattr(profile, field, ceiling)
                            changed = True
                    # `gain` is auto-exposure's FLOOR — the value it walks back
                    # down to once exposure has room again — and clamping a
                    # floor to the ceiling welds them together. The default is
                    # 100, ZWO-shaped; an IMX477 tops out at 22, so both became
                    # 22 and gain could never move. Every frame that camera
                    # took was at maximum gain: it read "pinned at both limits"
                    # all night, and in daylight with a fast lens it could not
                    # expose correctly at any shutter speed the sensor has.
                    #
                    # Unity, because gain is what you add when exposure runs
                    # out, not something to start from.
                    if profile.gain >= info.max_gain:
                        profile.gain = MIN_GAIN
                        changed = True
                if changed:
                    config.save(cfg)
                self.cfg = cfg

                # Start where this camera's profile says to, not at the
                # pre-camera defaults of one second and gain 100. Those are
                # necessarily blind guesses — they are set before any camera has
                # been opened — and auto-exposure walks gain down by a fifth per
                # frame, so at a daytime cadence of one frame every three
                # minutes a restart cost ten minutes of blown-out frames while
                # it climbed back down. On a rig outside that reads as a broken
                # camera, and it was reported as one.
                profile = profile_for(cfg, entry)
                self.gain = min(profile.gain, info.max_gain)
                self.exposure_us = min(self.exposure_us, profile.max_exposure_us)
                self._write_status({"camera": info.name,
                                    "camera_id": self.camera_id,
                                    "state": "capturing"})
                return
            except CameraError as exc:
                delay = REOPEN_BACKOFF[min(attempt, len(REOPEN_BACKOFF) - 1)]
                log.warning("Camera open failed (%s); retry in %ss", exc, delay)
                self.state = "no_camera"
                self._write_status({"camera": None, "state": "no_camera"})
                if attempt == 1:          # first retry failed too: it's real
                    notify.notify("camera_offline", "Camera offline",
                                  "Skylapse can't reach the camera.")
                    # Arm the recovery notice. Every alert must have an
                    # all-clear, or you are left refreshing the dashboard to
                    # find out whether plugging it back in worked.
                    self.stall.mark_alerted()
                time.sleep(delay)
                attempt += 1

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        while self.running:
            self.cfg = config.load()          # cheap; picks up UI changes
            cam = self.cfg.camera(self.camera_id)
            profile = profile_for(self.cfg, cam)
            # Watchdog first, so a stall is noticed even on iterations that end
            # early (focus, safety pause, night_only idle). It can only run
            # while the loop is turning — the ZWO driver's bounded exposure wait
            # is what guarantees a wedged camera returns here at all.
            self._check_for_stall(profile)

            now_period = period(self.cfg)
            if self.last_period is not None and now_period != self.last_period:
                log.info("Period change: %s -> %s", self.last_period, now_period)
            # Dawn: night just became day -> render last night's timelapse,
            # then run cleanup while nothing interesting is in the sky.
            if self.last_period in ("night", "twilight") and now_period == "day":
                log.info("Dawn: running night jobs (timelapse + cleanup)")
                cam_root = config.IMAGE_ROOT / self.camera_id
                # The folder frames are being written into right now, which at
                # dawn is still the night that just ended — the rollover is at
                # local noon, hours away. Deriving it from the same function
                # that files the frames is what makes that true by construction.
                #
                # This used to be max() over the directory names, i.e. the
                # newest folder, and on 2026-08-17 that was a folder created
                # minutes earlier: the host clock was on London time, so the
                # night rolled over at 6 AM local, and dawn then rendered the
                # 25 frames that had landed since instead of the 2205 from the
                # night. It validated, it notified, and it was 2 seconds long.
                latest_night = process.day_folder(time.time(), self.camera_id)
                if latest_night.exists() and cam.timelapse.auto_render:
                    log.info("Rendering timelapse for %s", latest_night.name)
                    nightjobs.render_night(latest_night, cam.timelapse)
                nightjobs.cleanup(cam_root, self.cfg.cleanup_free_gb)
            self.last_period = now_period

            # Focus assist: rapid throwaway frames + live sharpness score.
            # Auto-exits after FOCUS_TIMEOUT so a forgotten session can't
            # eat the night. Nothing is saved to disk in this mode.
            self._poll_focus_command()
            if self.focus is not None:
                if time.monotonic() - self.focus_started > FOCUS_TIMEOUT:
                    self.focus = None
                    log.info("Focus mode timed out; resuming capture")
                else:
                    self.state = "focusing"
                    self._focus_frame()
                    continue

            # Manual-mode safety: pause before capturing into daylight or
            # after repeated near-saturated frames (tracked-sensor protection).
            reason = safety_should_stop(profile, now_period, self.consecutive_bright)
            if reason:
                self._safety_pause(reason)
                continue

            # Keeper depth is a per-camera setting, and config hot-reloads, so
            # re-seat the deque when it changes. Without this the config field
            # is decorative — the buffer was pinned at its constructor default.
            depth = max(1, cam.raw.keeper_buffer_frames)
            if self.keeper_buffer.maxlen != depth:
                self.keeper_buffer = collections.deque(self.keeper_buffer,
                                                       maxlen=depth)
            self._poll_keeper_command()
            self._poll_setup_shot()

            # Aurora poll every POLL_MINUTES; once-per-episode alerting.
            if time.monotonic() - self.aurora_last_poll > aurora.POLL_MINUTES * 60:
                self.aurora_last_poll = time.monotonic()
                self.aurora_alerted, self.last_kp = aurora.check(
                    self.cfg, now_period, self.aurora_alerted)

            nightjobs.check_storage_warning(config.IMAGE_ROOT,
                                            self.cfg.cleanup_free_gb)

            # Capture schedule. Deliberately placed after the focus, keeper,
            # aurora and storage handling above: on a night_only camera those
            # subsystems must keep working through the day, and focus assist in
            # particular is a daylight activity — you aim and focus a rig in
            # daylight and leave it to run at night.
            if not should_capture(cam, now_period):
                self.state = "idle_day"
                if not self.idle_day:
                    self.idle_day = True
                    log.info("Capture schedule 'night_only' and it is day: "
                             "pausing capture until dusk")
                dusk = next_dusk(self.cfg)
                self._write_status({
                    "state": "idle_day",
                    "period": now_period,
                    "dusk": dusk.timestamp() if dusk else None,
                    # Keep the last frame so the dashboard shows the sky it saw
                    # rather than an empty placeholder that reads as a fault.
                    "latest": self.latest_path,
                })
                self._sleep_interruptible(IDLE_POLL_S)
                continue
            if self.idle_day:
                self.idle_day = False
                log.info("Dusk reached; resuming capture")

            try:
                prev_exposure, prev_gain = self.exposure_us, self.gain
                self.exposure_us, self.gain = next_exposure(
                    profile, self.last_brightness, self.exposure_us, self.gain)
                # Debug, not info: one of these per frame alongside the capture
                # line would double the journal volume for a decision that is
                # only interesting when AE is misbehaving.
                log.debug("AE(%s): measured=%s target=%d -> exposure %dus "
                          "(was %dus), gain %d (was %d)", now_period,
                          "none" if self.last_brightness is None
                          else "%.1f" % self.last_brightness,
                          profile.target_brightness, self.exposure_us,
                          prev_exposure, self.gain, prev_gain)
                self._check_ae_headroom(profile)
                self.driver.set_controls(self.exposure_us, self.gain)
                frame = self.driver.capture()
            except CameraError:
                log.exception("Capture failed; reopening camera")
                self.driver.close()
                self._open_camera()
                continue

            # Hot pixel pass: learn from the raw frame, then correct it in
            # place before anything downstream sees it. Zero user effort.
            dtype = np.uint16 if frame.bit_depth > 8 else np.uint8
            arr = np.frombuffer(frame.data, dtype=dtype).reshape(
                frame.height, frame.width)
            self.hotpixels.observe(arr, frame.exposure_us)
            corrected = self.hotpixels.correct(arr, frame.exposure_us)
            if corrected is not arr:
                frame.data = corrected.tobytes()

            # Plane means once, used twice: metering needs them, and the
            # sidecar keeps them so a colour cast is readable from a night's
            # metadata without reopening a single frame.
            frame._raw_means = process.plane_means(frame)
            self.last_brightness = process.metered_brightness(
                frame._raw_means, frame.bit_depth, self._wb(cam))
            self.consecutive_bright = (self.consecutive_bright + 1
                                       if self.last_brightness >= SAFETY_BRIGHT_LEVEL
                                       else 0)
            frame._stars = star_count(
                corrected if corrected is not arr else arr,
                frame.bayer) \
                if period(self.cfg) != "day" else None
            jpeg = process.save_jpeg(frame, self.camera_id, self.cfg.jpeg_quality,
                                     overlay=cam.overlay, stars=frame._stars,
                                     wb=self._wb(cam))
            # The settings screen renders pending multipliers from this, since
            # the JPEG just written already has the applied ones baked in.
            process.write_wb_preview(frame, self.camera_id, config.RUN_DIR)
            self.keeper_buffer.append(frame)
            dng_saved = self._raw_due(frame)
            if dng_saved:
                process.save_dng(frame, self.camera_id, self._wb(cam))

            # One line per completed frame. Under systemd this lands in journald,
            # which owns rotation — Skylapse writes no log files of its own. This
            # is the only durable per-frame record of what the capture loop did,
            # so it carries every field needed to reconstruct a night afterwards
            # (the status file holds just the latest frame and is overwritten).
            log.info("Frame t=%s exposure=%dus gain=%d brightness=%.1f stars=%s "
                     "dng=%s file=%s",
                     time.strftime("%H:%M:%S", time.localtime(frame.timestamp)),
                     frame.exposure_us, frame.gain, self.last_brightness,
                     frame._stars if frame._stars is not None else "-",
                     "yes" if dng_saved else "no", jpeg.name)

            self.latest_path = str(jpeg)
            self.last_frame_monotonic = time.monotonic()
            self.state = "capturing"
            if self.stall.frame_written():
                log.info("Frames resumed; sending the all-clear")
                notify.notify("camera_offline", "Skylapse: capturing again",
                              "The camera is back and frames are arriving "
                              "normally.")
            self._write_status({
                "state": "capturing",
                "period": period(self.cfg),
                "latest": self.latest_path,
                # Wall clock of the capture itself, not of this status write.
                # The dashboard counts down from it, so the save and analysis
                # time between the two would show up as drift.
                "frame_at": frame.timestamp,
                "exposure_us": self.exposure_us,
                "gain": self.gain,
                # Surfaced so the dashboard can say the exposure is maxed out,
                # rather than leaving a dark night looking like a broken camera.
                "ae_at_limits": self.ae_pinned >= AE_PINNED_FRAMES,
                "brightness": round(self.last_brightness, 1),
                "stars": getattr(frame, "_stars", None),
                "kp": self.last_kp,
                "dew": self.dewheater.tick() if self.dewheater else None,
            })

            # Gap-based timing: wait gap_s after the frame (capture + save)
            # finishes. Deterministic in both auto and manual modes.
            if profile.gap_s > 0:
                self._sleep_interruptible(profile.gap_s)

    # -- helpers -----------------------------------------------------------

    def _sleep_interruptible(self, seconds: float) -> None:
        """Wait out a gap, but wake early when the UI asks for something.

        Commands are only read at the top of the loop, so without this the
        response time to a button press is a whole capture cadence — a minute
        on the 60s day profile. Focus assist is interactive: someone is stood
        at the camera with a hand on the ring, and a minute of nothing is
        indistinguishable from a broken button.
        """
        # Monotonic: a wall-clock deadline turns an NTP step into a wildly wrong
        # sleep. Observed on this rig — the clock moved back four hours mid-gap
        # and the daemon sat in this loop waiting for a deadline four hours out,
        # capturing nothing and reporting "capturing" the whole time.
        deadline = time.monotonic() + seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if any((config.RUN_DIR / name).exists() for name in COMMAND_FILES):
                return
            time.sleep(min(COMMAND_POLL_S, remaining))

    def _check_for_stall(self, profile) -> None:
        """Alert once if frames have stopped arriving when they shouldn't have."""
        age = self.stall.check(
            state=self.state, now=time.monotonic(),
            last_frame_at=self.last_frame_monotonic,
            gap_s=profile.gap_s, exposure_us=self.exposure_us)
        if age is None:
            return
        pretty = describe_age(age)
        log.warning("No frames for %s while state=%s — capture appears stalled",
                    pretty, self.state)
        notify.notify(
            "camera_offline", "Skylapse: capture stalled",
            f"No frames for {pretty}. The daemon is running, so the camera may "
            f"be disconnected or not responding.")

    def _safety_pause(self, reason: str) -> None:
        """Stop capturing until user resume or the next dusk. Notifies once."""
        log.warning("Safety stop (%s): pausing capture", reason)
        self.state = "paused_safety"
        self._write_status({"state": "paused_safety", "reason": reason})
        notify.notify("safety_stop", "Capture paused (safety)",
                      "Manual-exposure safety tripped: "
                      + ("daylight detected." if reason == "daylight"
                         else "frames near saturation."))
        self.consecutive_bright = 0
        self.focus: FocusSession | None = None
        self.focus_started = 0.0
        self.aurora_alerted = False
        self.aurora_last_poll = 0.0
        self.last_kp: float | None = None
        dh = self.cfg.dew_heater
        # Experimental gate: flag off -> the subsystem is never even built.
        self.dewheater = DewHeater(dh.gpio_pin, dh.on_margin_c,
                                   dh.off_margin_c) \
            if dh.experimental_enabled else None
        resume_cmd = config.RUN_DIR / "resume_cmd"
        was_day = False
        while self.running:
            if resume_cmd.exists():
                resume_cmd.unlink(missing_ok=True)
                break
            p = period(self.cfg)
            if p == "day":
                was_day = True
            elif was_day:                 # day has passed; dusk arrived
                break
            time.sleep(30)
        log.info("Safety pause lifted; resuming capture")
        self.last_period = period(self.cfg)

    def _poll_focus_command(self) -> None:
        start = config.RUN_DIR / "focus_start"
        stop = config.RUN_DIR / "focus_stop"
        if start.exists():
            start.unlink(missing_ok=True)
            if self.focus is None:
                self.focus = FocusSession()
                self.focus_started = time.monotonic()
                # Fresh session: don't compare against the last session's
                # controls and rebaseline on the very first frame.
                self.focus_controls = None
                log.info("Focus mode ON (auto-exit in %ds)", FOCUS_TIMEOUT)
        if stop.exists():
            stop.unlink(missing_ok=True)
            if self.focus is not None:
                self.focus = None
                log.info("Focus mode OFF")

    def _poll_setup_shot(self) -> None:
        """The wizard's test shot: one real frame, straight to /run.

        The only part of setup that proves the hardware works rather than
        merely enumerating it. Written to the tmpfs, never to the image store —
        a setup frame turning up in the middle of someone's first night would
        be a small betrayal of the frame numbering.
        """
        request = config.RUN_DIR / "focus_cmd.json"
        if not request.exists():
            return
        request.unlink(missing_ok=True)
        # Controls are set on the driver, not passed to capture(). Getting this
        # wrong took the whole capture daemon down with a TypeError, which is
        # the one thing a wizard convenience must never be able to do — hence
        # the broad except below rather than only CameraError.
        try:
            self.driver.set_controls(*self._focus_controls())
            frame = self.driver.capture()
        except Exception:
            log.exception("Setup test shot failed")
            return
        cam = self.cfg.camera(self.camera_id)
        process.write_preview(frame, config.RUN_DIR / "setup_shot.jpg",
                              wb=self._wb(cam))
        log.info("Setup test shot captured from %s", self.camera_id)

    def _focus_controls(self) -> tuple[int, int]:
        """Live exposure/gain for focus mode.

        Re-read from the control file before every frame rather than latched at
        session start, so moving a slider on the phone lands on the very next
        capture — which is the whole point of a live view.
        """
        exposure_ms, gain = float(FOCUS_DEFAULT_EXPOSURE_MS), FOCUS_DEFAULT_GAIN
        try:
            data = json.loads((config.RUN_DIR / "focus_ctl.json").read_text())
            exposure_ms = float(data.get("exposure_ms", exposure_ms))
            gain = int(data.get("gain", gain))
        except (OSError, ValueError, TypeError):
            pass                      # no file yet, or garbage: use defaults
        # Float, and floored in microseconds rather than whole milliseconds.
        # Focusing on a distant object in daylight — which is how you set
        # infinity before dark — wants a fraction of a millisecond; one whole
        # one is already several stops over. The driver clamps to whatever the
        # sensor can actually do.
        return max(FOCUS_MIN_EXPOSURE_US, int(exposure_ms * 1000)), max(0, gain)

    def _focus_frame(self) -> None:
        """One rapid frame -> preview + score -> status. Never saved to the store."""
        import numpy as np
        exposure_us, gain = self._focus_controls()
        # Sharpness is only comparable at constant exposure and gain — changing
        # gain moves the score by more than a focus adjustment does. Carrying
        # the old peak across would leave an unreachable target on the chart and
        # a trend arrow describing the control change, not the focus ring.
        if self.focus_controls is not None and (exposure_us, gain) != self.focus_controls:
            log.info("Focus controls changed (%dms gain %d -> %dms gain %d); "
                     "rebaselining sharpness score",
                     self.focus_controls[0] // 1000, self.focus_controls[1],
                     exposure_us // 1000, gain)
            self.focus = FocusSession()
            self.focus_rebaselined_at = time.monotonic()
        self.focus_controls = (exposure_us, gain)
        try:
            self.driver.set_controls(exposure_us, gain)
            frame = self.driver.capture()
        except CameraError:
            log.exception("Focus capture failed; reopening camera")
            self.driver.close()
            self._open_camera()
            return
        dtype = np.uint16 if frame.bit_depth > 8 else np.uint8
        arr = np.frombuffer(frame.data, dtype=dtype).reshape(
            frame.height, frame.width)
        # Full resolution on purpose: the API crops this server-side for zoom,
        # so 8x/10x shows real sensor detail instead of an upscaled thumbnail.
        process.write_preview(frame, config.RUN_DIR / "focus_full.jpg",
                              wb=self._wb(self.cfg.camera(self.camera_id)))
        info = self.focus.update(sharpness(arr))
        self._write_status({"state": "focusing", "exposure_us": frame.exposure_us,
                            "gain": frame.gain, "width": frame.width,
                            "height": frame.height,
                            # Brief flag so the UI can explain the vanished peak
                            # rather than looking like it lost the reading.
                            "rebaselined": time.monotonic() - self.focus_rebaselined_at < 4,
                            **info})

    def _poll_keeper_command(self) -> None:
        """UI 'save RAW' button: dump the rolling raw buffer to DNGs."""
        cmd = config.RUN_DIR / "keeper_cmd"
        if not cmd.exists():
            return
        cmd.unlink(missing_ok=True)
        buffered = list(self.keeper_buffer)
        saved: list[str] = []
        for frame in buffered:
            try:
                saved.append(process.save_dng(
                    frame, self.camera_id,
                    self._wb(self.cfg.camera(self.camera_id))).name)
            except Exception:
                # Per-frame and non-fatal: one bad frame must not cost the rest.
                log.warning("Keeper: DNG save failed for frame at %.0f",
                            frame.timestamp, exc_info=True)
        # Only inside the success path. This previously logged unconditionally,
        # so the journal reported saves for frames that had just raised — which
        # is how a completely broken DNG writer went unnoticed.
        if saved:
            log.info("Keeper: saved %d of %d buffered frames as DNG (%s)",
                     len(saved), len(buffered), ", ".join(saved))
        else:
            log.error("Keeper: saved none of %d buffered frames", len(buffered))
        # Real counts for the UI toast — the POST returns long before this runs.
        config.write_run_file("keeper_result.json", json.dumps(
            {"saved": len(saved), "buffered": len(buffered), "at": time.time()}))

    @staticmethod
    def _wb(cam) -> tuple[float, float]:
        """This camera's colour multipliers, read fresh from its registry entry.

        Per camera rather than global: the multipliers describe a sensor and
        the lens in front of it, so the ASI676MC and the IMX477 have no
        business sharing a number. Read on every frame so a change from the
        settings screen takes effect on the next one, without a restart.
        """
        return (cam.wb_r, cam.wb_b)

    def _raw_due(self, frame: Frame) -> bool:
        cam = self.cfg.camera(self.camera_id)
        mode = cam.raw.mode
        if mode == "every_frame":
            return True
        if mode == "every_nth":
            n = max(1, cam.raw.every_nth)
            cadence = max(1, cam.night.gap_s + frame.exposure_us // 1_000_000)
            return int(frame.timestamp) % (n * cadence) < cadence
        # "window" mode: local-time window check
        if mode == "window":
            now = time.strftime("%H:%M")
            start, end = cam.raw.window_start, cam.raw.window_end
            return (start <= now or now < end) if start > end else (start <= now < end)
        return False

    def _check_ae_headroom(self, profile) -> None:
        """Notice when auto-exposure has run out of room.

        Pinned at max exposure AND max gain means the loop is asking for more
        light and has nothing left to give: every frame after that is as bright
        as this rig can make it, and the target is simply out of reach. That is
        not a fault, but it must be visible — a whole night ran at gain 22 on a
        module whose ceiling is 22 and nothing said so, which is why the sky
        looked darker than it should have and nobody knew where to look.
        """
        pinned = (profile.auto_exposure
                  and self.exposure_us >= profile.max_exposure_us
                  and self.gain >= profile.max_gain)
        if not pinned:
            self.ae_pinned = 0
            self.ae_pinned_said = False
            return
        self.ae_pinned += 1
        if self.ae_pinned >= AE_PINNED_FRAMES and not self.ae_pinned_said:
            self.ae_pinned_said = True
            log.info("AE at limits: %.0fs exposure and gain %d are both maxed "
                     "and brightness %.0f is still under the target of %d. "
                     "The sky is darker than this rig can reach — raise the "
                     "night max exposure, or accept darker frames.",
                     profile.max_exposure_us / 1e6, profile.max_gain,
                     self.last_brightness or 0, profile.target_brightness)

    def _write_status(self, extra: dict) -> None:
        config.RUN_DIR.mkdir(parents=True, exist_ok=True)
        status = {"updated": time.time(), **extra}
        config.write_run_file("daemon.json", json.dumps(status))


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    CaptureDaemon().start()


if __name__ == "__main__":
    main()
