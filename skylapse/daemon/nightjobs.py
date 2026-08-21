"""End-of-night jobs: dawn timelapse render + storage cleanup.

Timelapse: at the night->day transition the daemon calls render_night() on
the folder that just finished. Output lands next to the frames as
timelapse_YYYY-MM-DD.mp4 (h264/yuv420p, playable everywhere, YouTube-ready)
and fires the timelapse_ready notification.

Cleanup: when free space drops below the configured floor, delete the oldest
nights' FRAMES first — mp4 timelapses are the distilled keepsake of a night
and are the last thing removed.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import config, notify

log = logging.getLogger("skylapse.nightjobs")

FPS_MIN, FPS_MAX = 12, 60
CRF = {"standard": "23", "high": "20", "max": "17"}

# h264 level is what decides whether a phone will play the file, and level is
# set by macroblocks per SECOND — not by resolution alone. The resolution budget
# below fixes the spatial half; this fixes the temporal half, which it does not.
#
# Measured on the rig: the 2205-frame night of 2026-08-17 came out inside the 4K
# budget at 3326x2492 — 32,448 macroblocks, comfortably under level 5.1's 36,864
# per-frame ceiling — and still landed at level 5.2, because 2205 frames over a
# 30-second target asks for 74 fps, clamps to 60, and 32,448 x 60 is 1.9M MB/s
# against level 5.1's 983,040. A long night is exactly the case that trips this,
# which is to say the case that matters.
#
# So the frame rate is capped to whatever keeps the output inside 5.1. The clip
# comes out longer than asked for, and that is the right trade: a 73-second
# timelapse that plays beats a 37-second one that does not.
LEVEL_51_MB_PER_S = 983_040


def max_playable_fps(width: int, height: int) -> int:
    """Highest frame rate that keeps this frame size inside h264 level 5.1."""
    if not width or not height:
        return FPS_MAX
    macroblocks = ((width + 15) // 16) * ((height + 15) // 16)
    return max(FPS_MIN, min(FPS_MAX, LEVEL_51_MB_PER_S // max(1, macroblocks)))


# Output size, as a pixel budget rather than a width. Level is what decides
# whether a phone can play the file, and level is set by macroblock count — so
# a width alone tells you nothing on a sensor that is not 16:9. Measured on the
# rig: 3840x2878 is h264 level 6.0, beyond essentially every hardware decoder,
# while 3328x2494 — the same 4K *budget* — comes out at level 5.1 and plays.
RESOLUTIONS = {
    "4k": 3840 * 2160,        # 8.3 MP. Default: the largest that reliably plays.
    "1080p": 1920 * 1080,
    "full": 0,                # native. Warned about in the UI; see DESIGN.md.
}


def output_size(width: int, height: int, resolution: str) -> tuple[int, int]:
    """Scale (width, height) into the chosen budget, preserving aspect.

    Dimensions come back even, because h264 with yuv420p cannot encode odd
    ones. Never upscales: a small sensor is not improved by being stretched to
    fill a budget.
    """
    budget = RESOLUTIONS.get(resolution, RESOLUTIONS["4k"])
    if not budget or width * height <= budget:
        return (width // 2) * 2, (height // 2) * 2
    scale = (budget / (width * height)) ** 0.5
    return (max(2, int(width * scale) // 2 * 2),
            max(2, int(height * scale) // 2 * 2))


def scale_filter(width: int, height: int, resolution: str) -> str:
    """The -vf argument for this output size.

    Prefers concrete numbers, measured from the first frame. When the frame
    cannot be measured the same budget is handed to ffmpeg as an expression
    instead of giving up on it — otherwise a retry at a smaller size would
    produce byte-for-byte the same command as the attempt that just failed.
    """
    budget = RESOLUTIONS.get(resolution, RESOLUTIONS["4k"])
    if width and height:
        w, h = output_size(width, height, resolution)
        return f"scale={w}:{h}"
    if not budget:                                 # native, even edges only
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    # min(1, ...) keeps it a downscale; -2 holds the aspect and rounds even.
    return f"scale=trunc(iw*min(1\\,sqrt({budget}/(iw*ih)))/2)*2:-2"



def classify_frames(frames: list, cfg=None) -> dict:
    """Split a folder's frames into the night and the day around it.

    A night folder runs noon to noon, so it holds the evening before dusk, the
    night, and the morning after dawn — and those are shot on completely
    different settings. Cutting them into one film puts a hard jump at each end
    where the exposure changes by three orders of magnitude, which is jarring
    to watch and was the first thing anyone said about it.

    Judged by the sun's altitude at the moment each frame was taken, not by any
    metadata, so it works on nights already sitting on the card. Twilight counts
    as night: it is exposed on the night profile and blends with it, and it is
    the part of the evening actually worth watching.
    """
    from ..config import load
    from .scheduler import period

    cfg = cfg or load()
    out = {"night": [], "day": []}
    for frame in frames:
        when = datetime.fromtimestamp(frame.stat().st_mtime, timezone.utc)
        out["day" if period(cfg, when) == "day" else "night"].append(frame)
    return out


def select_frames(frames: list, clip_seconds: int, fps: int) -> list:
    """Evenly spaced frames, so the clip comes out the length that was asked for.

    Duration is frames / fps, so with the frame rate pinned there is exactly one
    free variable left: how many frames go in. A 2205-frame night wants 74 fps to
    fit 30 seconds; the ceiling is 60, and h264 level pulls it to 30 — so it came
    out 73 seconds while the setting said 30. That setting had never been honoured
    on a long night, which in summer is every night.

    Sampling loses nothing that is kept anywhere else: every frame is still on the
    card and still in the nights browser. What it changes is the pace of the clip,
    which is the thing the setting is asking about.

    A night with fewer frames than the target is left alone — there is nothing to
    sample, and padding it out would mean duplicating frames to reach a length
    nobody would notice.
    """
    wanted = max(1, clip_seconds * fps)
    if len(frames) <= wanted:
        return frames

    # Evenly spaced in TIME, not in frame number. Frames do not arrive at a
    # constant rate — exposure changes through the night, and a gap where the
    # sensor was settling leaves a hole — so picking every Nth frame makes the
    # sky crawl through the dense stretches and leap across the sparse ones.
    # That unevenness is what "jumpy" means. Sampling on a clock instead gives
    # constant apparent motion, and a gap becomes a brief hold rather than a
    # jump, which is both smoother and more honest about what was captured.
    try:
        times = [f.stat().st_mtime for f in frames]
    except (OSError, AttributeError):
        # No timestamps available (a caller passing plain values, or a file that
        # vanished mid-render): fall back to every Nth, which is worse but works.
        step = len(frames) / wanted
        return [frames[min(len(frames) - 1, int(i * step))] for i in range(wanted)]

    span = times[-1] - times[0]
    if span <= 0:
        step = len(frames) / wanted
        return [frames[min(len(frames) - 1, int(i * step))] for i in range(wanted)]

    chosen, cursor = [], 0
    for i in range(wanted):
        target = times[0] + span * i / (wanted - 1 if wanted > 1 else 1)
        while cursor + 1 < len(times) and abs(times[cursor + 1] - target) <=                 abs(times[cursor] - target):
            cursor += 1
        chosen.append(frames[cursor])
    return chosen


def usable_frames(folder: Path) -> list[Path]:
    """The night's frames, minus any that ffmpeg could not open.

    One unreadable frame used to cost the whole rest of the night: the concat
    demuxer stops at the first input it cannot open, and ffmpeg still exits 0.
    Measured on the rig, a single zero-byte JPEG turned a 328-frame night into
    a seventeen-frame, 1.4-second clip that reported success and sent a
    "timelapse ready" notification.

    Zero-length is the case actually seen, and it is cheap to test for without
    decoding 300 files. Anything else that fails to open is caught by the
    validation pass after the render.
    """
    frames, skipped = [], 0
    for f in sorted(folder.glob("img_*.jpg")):
        if f.stat().st_size > 0:
            frames.append(f)
        else:
            skipped += 1
    if skipped:
        log.warning("Skipping %d unreadable frame(s) in %s", skipped, folder.name)
    return frames


def _probe(path: Path) -> dict | None:
    """Frame count and dimensions of a rendered file.

    {} means ffprobe ran and could make nothing of the file — which is a real
    fault. None means ffprobe is not installed, which says nothing about the
    file and must not be treated as a failure.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames,width,height",
             "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
            capture_output=True, text=True, timeout=600).stdout
    except FileNotFoundError:
        # No ffprobe on this system. "Cannot check" is not "is broken": ffmpeg
        # exited 0 and there is a file, and throwing that away would mean never
        # producing a timelapse at all here. Returning None says unverified,
        # which the caller accepts with a warning; {} still means it ran and
        # told us nothing, which does mean broken.
        return None
    except subprocess.TimeoutExpired:
        return {}
    try:
        return dict(line.split("=", 1)
                    for line in out.strip().splitlines() if "=" in line)
    except (AttributeError, TypeError, ValueError):
        return {}


def validate_render(path: Path, expected_frames: int) -> str:
    """Empty string if the file is good, else why it is not.

    A render is not finished when ffmpeg exits 0. That is exactly what happened
    here: exit 0, a playable file, and 95% of the night missing. Nothing may
    report a timelapse ready without having counted the frames in it.
    """
    if not path.exists() or path.stat().st_size == 0:
        return "no output file"
    probe = _probe(path)
    if probe is None:
        log.warning("ffprobe is not installed, so %s cannot be checked; "
                    "accepting it unverified", path.name)
        return ""
    if not probe:
        return "output does not probe as video"
    try:
        got = int(probe.get("nb_read_frames", 0))
    except ValueError:
        return "output frame count unreadable"
    if got < expected_frames:
        return f"only {got} of {expected_frames} frames encoded"
    return ""


def partial_path(out: Path) -> Path:
    """Where a render is written before it is finished.

    An mp4's index is written last, so a render in progress is a large file
    that no player can open. Serving that is how a night's timelapse showed up
    as a blank player reading 0:00 while ffmpeg was still working on it — the
    file existed, so everything downstream believed it was ready.

    Rendering beside the real name and moving it into place at the end makes
    the finished path mean finished, atomically, the way config.save does.
    """
    return out.with_suffix(out.suffix + ".part")


def _run_ffmpeg(folder: Path, frames: list[Path], out: Path, fps: int,
                crf: str, scale: str) -> str:
    """One render attempt. Returns an error string, or "" on success."""
    listfile = folder / ".frames.txt"
    listfile.write_text("".join(f"file '{f.name}'\n" for f in frames))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-r", str(fps), "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c:v", "libx264", "-preset", "medium",
             "-crf", crf, "-pix_fmt", "yuv420p", "-vf", scale,
             # Stated, not inferred. ffmpeg picks the container from the output
             # extension, and renders go to a .part file so a half-written one
             # is never mistaken for a finished film — an extension ffmpeg has
             # never heard of. Without this it exits immediately with "unable to
             # find a suitable output format", which is how a release went out
             # that could not render anything at all.
             "-f", "mp4", str(out)],
            cwd=folder, capture_output=True, timeout=1800, check=True)
    except FileNotFoundError:
        return "ffmpeg not installed"
    except subprocess.CalledProcessError as exc:
        return f"ffmpeg exited {exc.returncode}: " \
               f"{(exc.stderr or b'').decode(errors='replace').strip()[-300:]}"
    except subprocess.TimeoutExpired:
        return "ffmpeg timed out"
    finally:
        listfile.unlink(missing_ok=True)
    return ""


def output_name(folder: Path, variant: str) -> Path:
    """Where each film lands. The night keeps the original name — it is the one
    anybody came for — and the day gets a suffix."""
    stem = f"timelapse_{folder.name}"
    return folder / (f"{stem}.mp4" if variant == "night" else f"{stem}_{variant}.mp4")


def render_all(folder: Path, settings=None, force: bool = False) -> dict:
    """Render the night, and the day around it, as separate films.

    A night folder runs noon to noon, so it contains the evening before dusk and
    the morning after dawn as well — shot at a fiftieth of a second at unity
    gain, against twenty-five seconds at gain fifteen for the night itself.
    Splicing those together puts a hard cut at each end where the picture
    changes completely, which is the first thing anyone notices watching it.

    So they are separate films now. Neither is a compromise, and the night one
    no longer opens on daylight.
    """
    results = {}
    groups = classify_frames(usable_frames(folder))
    for variant in ("night", "day"):
        if len(groups[variant]) < FPS_MIN:
            continue
        if variant == "day" and settings is not None                 and not getattr(settings, "day_clip", True):
            continue
        rendered = render_night(folder, settings, force=force,
                                frames=groups[variant], variant=variant)
        if rendered:
            results[variant] = rendered
    return results


def render_night(folder: Path, settings=None, force: bool = False,
                 frames: list | None = None,
                 variant: str = "night") -> Path | None:
    """ffmpeg the folder's JPEGs into an mp4. Returns the path, or None.

    Idempotent unless force=True (the UI's re-render button after a settings
    change: new clip length/quality, same night) — except when the existing
    file is older than the newest frame, which means it was rendered before the
    night finished. A folder rolls at local noon while the render fires at
    dawn, so on the rig every timelapse was missing its morning frames: the
    08-15 clip was written at 07:31 and 252 more frames arrived afterwards.
    """
    from ..config import TimelapseConfig
    settings = settings or TimelapseConfig()
    frames = usable_frames(folder) if frames is None else frames
    if len(frames) < FPS_MIN:                      # not enough for a clip
        return None
    out = output_name(folder, variant)
    if out.exists() and not force:
        newest = max(f.stat().st_mtime for f in frames)
        if out.stat().st_mtime >= newest:          # nothing has arrived since
            return out
        log.info("%s predates the night's last frame; re-rendering", out.name)
    # The previous film stays exactly where it is until a new one has rendered
    # AND passed validation, at which point it is replaced in one move. Deleting
    # it up front means any failure — a bad flag, a full card, a killed
    # process — costs a night that was already safely on disk. That is not
    # hypothetical: it is what happened here.

    target = max(5, settings.clip_seconds)
    fps = max(FPS_MIN, min(FPS_MAX, round(len(frames) / target)))
    crf = CRF.get(settings.quality, "20")

    # Source dimensions decide the output size. If the first frame will not
    # decode we still render — at native size, letting ffmpeg round the edges —
    # rather than abandoning a whole night over one unreadable file.
    import cv2
    first = cv2.imread(str(frames[0]))
    if first is None:
        log.warning("Cannot measure %s; rendering at native size", frames[0].name)
        width = height = 0
    else:
        height, width = first.shape[:2]

    # Full resolution first if that is what was asked for, then one retry at a
    # size that is known to play. A render that dies at 12 MP is far more
    # likely to survive at 4K than to survive being tried again identically.
    attempts = [settings.resolution]
    if settings.resolution != "1080p":
        attempts.append("1080p" if settings.resolution == "4k" else "4k")

    for attempt, resolution in enumerate(attempts):
        scale = scale_filter(width, height, resolution)
        # Only now is the output size known, and the rate depends on it.
        attempt_fps = fps
        if width and height:
            out_w, out_h = output_size(width, height, resolution)
            attempt_fps = min(fps, max_playable_fps(out_w, out_h))
            if attempt_fps != fps:
                log.info("Capping %d fps to %d so %dx%d stays inside h264 "
                         "level 5.1", fps, attempt_fps, out_w, out_h)
        # With the rate settled, the frame count is what sets the duration.
        shown = select_frames(frames, target, attempt_fps)
        if len(shown) != len(frames):
            log.info("Sampling %d of %d frames for a %ds clip at %d fps",
                     len(shown), len(frames), target, attempt_fps)
        working = partial_path(out)
        error = _run_ffmpeg(folder, shown, working, attempt_fps,
                            crf, scale) \
            or validate_render(working, len(shown))
        if not error:
            # Only now does the finished name exist, and it is finished.
            working.replace(out)
            log.info("Rendered %s (%d of %d frames @ %d fps, %s%s)", out.name,
                     len(shown), len(frames), attempt_fps, resolution,
                     f", retried after failing at {attempts[0]}" if attempt else "")
            notify.notify("timelapse_ready", "Timelapse ready",
                          f"Last night's timelapse is done: {out.name} "
                          f"({len(shown)} of {len(frames)} frames, "
                          f"{len(shown) / attempt_fps:.0f}s)")
            return out
        log.warning("Timelapse render at %s failed: %s", resolution, error)
        partial_path(out).unlink(missing_ok=True)

    # Deliberately silent to the user's phone. A notification saying a
    # timelapse is ready, for a file that is not, is worse than no
    # notification: it is the thing that stopped anyone looking at the journal.
    log.error("Timelapse for %s could not be rendered", folder.name)
    return None


def cleanup(camera_root: Path, free_gb_floor: float) -> int:
    """Delete oldest nights until free space clears the floor.
    Two passes per night: frames+sidecars first, the mp4 only if still needed.
    Never touches the current (newest) night. Returns files deleted.
    """
    deleted = 0
    nights = sorted(d for d in camera_root.iterdir() if d.is_dir())[:-1]
    for night in nights:
        if _free_gb(camera_root) >= free_gb_floor:
            break
        # thumb_*.jpg are the API's lazily-generated filmstrip thumbnails. They
        # must go with the frames they describe, or they outlive the night and
        # leave the folder permanently non-empty so it can never be removed.
        for pattern in ("img_*.jpg", "img_*.json", "img_*.dng", "thumb_*.jpg"):
            for f in night.glob(pattern):
                f.unlink()
                deleted += 1
        if _free_gb(camera_root) < free_gb_floor:      # still tight: mp4 goes too
            for f in night.glob("timelapse_*.mp4"):
                f.unlink()
                deleted += 1
        if not any(night.iterdir()):
            night.rmdir()
    if deleted:
        log.info("Cleanup freed space: %d files removed", deleted)
    return deleted


def check_storage_warning(camera_root: Path, free_gb_floor: float) -> None:
    """Warn (once per low-space episode) at 2x the cleanup floor."""
    free = _free_gb(camera_root)
    marker = config.RUN_DIR / "storage_warned"
    if free < free_gb_floor * 2:
        if not marker.exists():
            config.RUN_DIR.mkdir(parents=True, exist_ok=True)
            marker.touch()
            notify.notify("storage_low", "Storage running low",
                          f"{free:.1f} GB free on the camera's SD card.")
    else:
        marker.unlink(missing_ok=True)


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


# -- explaining a render before it happens -----------------------------------

# JPEG start-of-frame markers, the ones that carry the dimensions.
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_JPEG_MAGIC = 0xFFD8
_PAD = 0xFF


def jpeg_size(path: Path) -> tuple[int, int]:
    """Width and height from a JPEG header, without decoding the image.

    The settings screen asks what tonight's render will look like, and decoding
    a 12 MP frame to answer that — on a Pi, while it is capturing — is not a
    reasonable price for a label.
    """
    try:
        with open(path, "rb") as fh:
            if int.from_bytes(fh.read(2), "big") != _JPEG_MAGIC:
                return 0, 0
            while True:
                byte = fh.read(1)
                while byte and byte[0] != _PAD:
                    byte = fh.read(1)
                marker = fh.read(1)
                while marker and marker[0] == _PAD:
                    marker = fh.read(1)
                if not marker:
                    return 0, 0
                if marker[0] in _SOF_MARKERS:
                    fh.read(3)                      # precision + skip
                    height = int.from_bytes(fh.read(2), "big")
                    width = int.from_bytes(fh.read(2), "big")
                    return width, height
                length = int.from_bytes(fh.read(2), "big")
                if length < 2:
                    return 0, 0
                fh.seek(length - 2, 1)
    except (OSError, IndexError):
        return 0, 0


def plan_render(folder: Path, settings=None) -> dict:
    """What rendering this folder would produce, without rendering it.

    Exists because the sampling is otherwise invisible. A clip that honours the
    length you asked for does so by leaving frames out, and being told that
    afterwards — or not at all — is how the old behaviour went unnoticed for as
    long as it did. Built from the same functions the renderer uses, so this
    answer cannot drift from what actually happens.
    """
    from ..config import TimelapseConfig
    settings = settings or TimelapseConfig()
    frames = usable_frames(folder)
    target = max(5, settings.clip_seconds)
    plan = {"frames": len(frames), "clip_seconds": target,
            "resolution": settings.resolution}
    if not frames:
        return {**plan, "fps": 0, "used": 0, "seconds": 0.0, "every_nth": 0,
                "width": 0, "height": 0}

    width, height = jpeg_size(frames[0])
    out_w, out_h = (output_size(width, height, settings.resolution)
                    if width and height else (0, 0))
    fps = max(FPS_MIN, min(FPS_MAX, round(len(frames) / target)))
    if out_w and out_h:
        fps = min(fps, max_playable_fps(out_w, out_h))
    used = len(select_frames(frames, target, fps))
    return {**plan, "fps": fps, "used": used, "seconds": round(used / fps, 1),
            # How the sampling reads to a person: "every 2nd frame".
            "every_nth": round(len(frames) / used, 1) if used else 0,
            "width": out_w, "height": out_h}
