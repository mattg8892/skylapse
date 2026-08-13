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
from .focus import TIMEOUT_S as FOCUS_TIMEOUT, FocusSession, sharpness
from . import aurora
from .dewheater import DewHeater
from .pipeline.analyze import star_count
from .. import notify
from . import nightjobs
from .scheduler import (SAFETY_BRIGHT_LEVEL, next_exposure, period,
                        profile_for, safety_should_stop)

log = logging.getLogger("skylapse.daemon")

REOPEN_BACKOFF = (5, 15, 60)     # camera disconnect recovery, seconds


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
                self.driver = detect_camera()
                info = self.driver.open()
                self.camera_id = info.camera_id
                # Register in the config-file camera registry (fetch-or-create),
                # filling identity fields on first sight of this hardware.
                cfg = config.load()
                entry = cfg.camera(self.camera_id)
                if not entry.model:
                    entry.driver, entry.model = info.driver, info.name
                    entry.label = entry.label or info.name
                    config.save(cfg)
                self.cfg = cfg
                self._write_status({"camera": info.name,
                                    "camera_id": self.camera_id,
                                    "state": "capturing"})
                return
            except CameraError as exc:
                delay = REOPEN_BACKOFF[min(attempt, len(REOPEN_BACKOFF) - 1)]
                log.warning("Camera open failed (%s); retry in %ss", exc, delay)
                self._write_status({"camera": None, "state": "no_camera"})
                if attempt == 1:          # first retry failed too: it's real
                    notify.notify("camera_offline", "Camera offline",
                                  "Skylapse can't reach the camera.")
                time.sleep(delay)
                attempt += 1

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        while self.running:
            self.cfg = config.load()          # cheap; picks up UI changes
            cam = self.cfg.camera(self.camera_id)
            profile = profile_for(self.cfg, cam)
            # Dawn: night just became day -> render last night's timelapse,
            # then run cleanup while nothing interesting is in the sky.
            now_period = period(self.cfg)
            if self.last_period is not None and now_period != self.last_period:
                log.info("Period change: %s -> %s", self.last_period, now_period)
            if self.last_period in ("night", "twilight") and now_period == "day":
                log.info("Dawn: running night jobs (timelapse + cleanup)")
                cam_root = config.IMAGE_ROOT / self.camera_id
                latest_night = max((d for d in cam_root.iterdir() if d.is_dir()),
                                   default=None)
                if latest_night and cam.timelapse.auto_render:
                    log.info("Rendering timelapse for %s", latest_night.name)
                    nightjobs.render_night(latest_night, cam.timelapse)
                nightjobs.cleanup(cam_root, self.cfg.cleanup_free_gb)
            self.last_period = now_period

            # Focus assist: rapid throwaway frames + live sharpness score.
            # Auto-exits after FOCUS_TIMEOUT so a forgotten session can't
            # eat the night. Nothing is saved to disk in this mode.
            self._poll_focus_command()
            if self.focus is not None:
                if time.time() - self.focus_started > FOCUS_TIMEOUT:
                    self.focus = None
                    log.info("Focus mode timed out; resuming capture")
                else:
                    self._focus_frame()
                    continue

            # Manual-mode safety: pause before capturing into daylight or
            # after repeated near-saturated frames (tracked-sensor protection).
            reason = safety_should_stop(profile, now_period, self.consecutive_bright)
            if reason:
                self._safety_pause(reason)
                continue

            self._poll_keeper_command()

            # Aurora poll every POLL_MINUTES; once-per-episode alerting.
            if time.time() - self.aurora_last_poll > aurora.POLL_MINUTES * 60:
                self.aurora_last_poll = time.time()
                self.aurora_alerted, self.last_kp = aurora.check(
                    self.cfg, now_period, self.aurora_alerted)

            nightjobs.check_storage_warning(config.IMAGE_ROOT,
                                            self.cfg.cleanup_free_gb)
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

            self.last_brightness = process.mean_brightness(frame)
            self.consecutive_bright = (self.consecutive_bright + 1
                                       if self.last_brightness >= SAFETY_BRIGHT_LEVEL
                                       else 0)
            frame._stars = star_count(corrected if corrected is not arr else arr) \
                if period(self.cfg) != "day" else None
            jpeg = process.save_jpeg(frame, self.camera_id, self.cfg.jpeg_quality,
                                     overlay=cam.overlay, stars=frame._stars)
            self.keeper_buffer.append(frame)
            dng_saved = self._raw_due(frame)
            if dng_saved:
                process.save_dng(frame, self.camera_id)

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

            self._write_status({
                "state": "capturing",
                "period": period(self.cfg),
                "latest": str(jpeg),
                "exposure_us": self.exposure_us,
                "gain": self.gain,
                "brightness": round(self.last_brightness, 1),
                "stars": getattr(frame, "_stars", None),
                "kp": self.last_kp,
                "dew": self.dewheater.tick() if self.dewheater else None,
            })

            # Gap-based timing: wait gap_s after the frame (capture + save)
            # finishes. Deterministic in both auto and manual modes.
            if profile.gap_s > 0:
                time.sleep(profile.gap_s)

    # -- helpers -----------------------------------------------------------

    def _safety_pause(self, reason: str) -> None:
        """Stop capturing until user resume or the next dusk. Notifies once."""
        log.warning("Safety stop (%s): pausing capture", reason)
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
                self.focus_started = time.time()
                log.info("Focus mode ON (auto-exit in %ds)", FOCUS_TIMEOUT)
        if stop.exists():
            stop.unlink(missing_ok=True)
            if self.focus is not None:
                self.focus = None
                log.info("Focus mode OFF")

    def _focus_frame(self) -> None:
        """One rapid frame -> score -> status. Never saved."""
        import numpy as np
        try:
            self.driver.set_controls(1_000_000, 250)      # 1s, hot gain
            frame = self.driver.capture()
        except CameraError:
            log.exception("Focus capture failed; reopening camera")
            self.driver.close()
            self._open_camera()
            return
        dtype = np.uint16 if frame.bit_depth > 8 else np.uint8
        arr = np.frombuffer(frame.data, dtype=dtype).reshape(
            frame.height, frame.width)
        info = self.focus.update(sharpness(arr))
        self._write_status({"state": "focusing", **info})

    def _poll_keeper_command(self) -> None:
        """UI 'save RAW' button: dump the rolling raw buffer to DNGs."""
        cmd = config.RUN_DIR / "keeper_cmd"
        if not cmd.exists():
            return
        cmd.unlink(missing_ok=True)
        for buffered in list(self.keeper_buffer):
            try:
                process.save_dng(buffered, self.camera_id)
            except Exception:
                log.exception("Keeper DNG save failed")
        log.info("Keeper: saved %d buffered frames as DNG", len(self.keeper_buffer))

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

    def _write_status(self, extra: dict) -> None:
        config.RUN_DIR.mkdir(parents=True, exist_ok=True)
        status = {"updated": time.time(), **extra}
        (config.RUN_DIR / "daemon.json").write_text(json.dumps(status))


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    CaptureDaemon().start()


if __name__ == "__main__":
    main()
