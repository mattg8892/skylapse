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
from pathlib import Path

from .. import config, notify

log = logging.getLogger("skylapse.nightjobs")

FPS_MIN, FPS_MAX = 12, 60
CRF = {"standard": "23", "high": "20", "max": "17"}


def render_night(folder: Path, settings=None, force: bool = False) -> Path | None:
    """ffmpeg the folder's JPEGs into an mp4. Returns the path, or None.
    Idempotent unless force=True (the UI's re-render button after a settings
    change: new clip length/quality, same night)."""
    from ..config import TimelapseConfig
    settings = settings or TimelapseConfig()
    frames = sorted(folder.glob("img_*.jpg"))
    if len(frames) < FPS_MIN:                      # not enough for a clip
        return None
    out = folder / f"timelapse_{folder.name}.mp4"
    if out.exists():
        if not force:                              # idempotent across restarts
            return out
        out.unlink()

    target = max(5, settings.clip_seconds)
    fps = max(FPS_MIN, min(FPS_MAX, round(len(frames) / target)))
    listfile = folder / ".frames.txt"
    listfile.write_text("".join(f"file '{f.name}'\n" for f in frames))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-r", str(fps), "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c:v", "libx264", "-preset", "medium",
             "-crf", CRF.get(settings.quality, "20"), "-pix_fmt", "yuv420p",
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",   # h264 needs even dims
             str(out)],
            cwd=folder, capture_output=True, timeout=1800, check=True)
    except FileNotFoundError:
        log.warning("ffmpeg not installed; skipping timelapse")
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("Timelapse render failed: %s", exc)
        return None
    finally:
        listfile.unlink(missing_ok=True)

    log.info("Rendered %s (%d frames @ %d fps)", out.name, len(frames), fps)
    notify.notify("timelapse_ready", "Timelapse ready",
                  f"Last night's timelapse is done: {out.name} "
                  f"({len(frames)} frames)")
    return out


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
        for pattern in ("img_*.jpg", "img_*.json", "img_*.dng"):
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
