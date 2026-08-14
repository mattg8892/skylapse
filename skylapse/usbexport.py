"""Copy nights off the camera onto a USB drive.

The whole point of this module is to be boring and safe. Two rules govern
everything here:

1. **The boot medium must never be offered as an export target.** A Pi's SD
   card reports as removable, so `rm == 1` alone is not enough to identify a
   USB stick — filtering on it would happily present the running system's own
   card as a place to write. Candidates must be on a USB transport *and* must
   not live on whichever disk currently carries `/`.
2. **Nothing is ever deleted from the camera.** Export copies. There is no
   move, no cleanup, no "free up space afterwards" path in this file.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger("skylapse.usbexport")

MOUNT_ROOT = Path("/media/skylapse-export")
EXPORT_DIRNAME = "Skylapse"
STATUS_NAME = "export.json"
_PROGRESS_INTERVAL_S = 0.5      # how often a long copy refreshes its status


# -- drive discovery --------------------------------------------------------

def _run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _root_disk() -> str:
    """Name of the disk carrying / — e.g. 'mmcblk0' or 'sda'. Never a target."""
    try:
        src = _run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()
        if not src:
            return ""
        name = _run(["lsblk", "-no", "PKNAME", src]).stdout.strip().splitlines()
        return name[0].strip() if name else Path(src).name
    except Exception:
        log.exception("Could not determine the root disk; refusing all drives")
        # Fail closed: an unknown root disk means we cannot prove a candidate
        # is not the boot medium, and every candidate is rejected below.
        return ""


def list_drives() -> list[dict]:
    """Mountable USB partitions, boot medium excluded."""
    try:
        out = _run(["lsblk", "-J", "-b", "-o",
                    "NAME,PATH,SIZE,TYPE,TRAN,RM,MOUNTPOINT,LABEL,FSTYPE,PKNAME"])
    except Exception:
        log.exception("lsblk failed")
        return []
    if out.returncode != 0:
        log.warning("lsblk exited %s: %s", out.returncode, out.stderr.strip())
        return []

    try:
        tree = json.loads(out.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        log.warning("lsblk returned unparseable JSON")
        return []

    root_disk = _root_disk()
    drives: list[dict] = []

    for disk in tree:
        if disk.get("type") != "disk":
            continue
        # Rule 1: USB transport only. An SD card reports rm=1 but tran=None,
        # so this is what actually keeps the boot card out of the list.
        if (disk.get("tran") or "").lower() != "usb":
            continue
        # Belt and braces: even a USB-attached disk is refused if it is the one
        # we booted from (USB-boot Pis are common).
        if not root_disk or disk.get("name") == root_disk:
            continue

        for part in disk.get("children") or [disk]:
            if part.get("type") not in ("part", "disk"):
                continue
            if part.get("mountpoint") in ("/", "/boot", "/boot/firmware"):
                continue
            if not part.get("fstype"):
                continue          # unformatted or extended container
            drives.append({
                "device": part.get("path"),
                "label": part.get("label") or disk.get("label") or part.get("name"),
                "size_bytes": int(part.get("size") or 0),
                "fstype": part.get("fstype"),
                "mountpoint": part.get("mountpoint"),
                "disk": disk.get("name"),
            })
    return drives


def _drive(device: str) -> dict:
    for d in list_drives():
        if d["device"] == device:
            return d
    raise ValueError(f"{device} is not an eligible USB drive")


# -- mount / eject ----------------------------------------------------------

def mount(device: str) -> dict:
    """Mount via udisksctl (no root needed under the usual polkit rules).

    Returns {"mountpoint": ..., "needs_sudo": bool}. We never silently escalate
    — if udisksctl is unavailable or refuses, the caller surfaces that and the
    operator decides.
    """
    drive = _drive(device)
    if drive["mountpoint"]:
        return {"mountpoint": drive["mountpoint"], "needs_sudo": False}

    if shutil.which("udisksctl"):
        res = _run(["udisksctl", "mount", "-b", device, "--no-user-interaction"], 60)
        if res.returncode == 0:
            # "Mounted /dev/sda1 at /media/user/LABEL."
            tail = res.stdout.strip().rstrip(".").split(" at ")
            if len(tail) == 2:
                return {"mountpoint": tail[1], "needs_sudo": False}
            refreshed = _drive(device)
            if refreshed["mountpoint"]:
                return {"mountpoint": refreshed["mountpoint"], "needs_sudo": False}
        log.warning("udisksctl mount failed: %s", res.stderr.strip())

    return {"mountpoint": None, "needs_sudo": True,
            "hint": f"sudo mkdir -p {MOUNT_ROOT} && sudo mount {device} {MOUNT_ROOT}"}


def eject(device: str) -> dict:
    """Flush, unmount, and power down the drive so it is safe to pull."""
    subprocess.run(["sync"], timeout=120)
    if not shutil.which("udisksctl"):
        return {"ok": False, "needs_sudo": True,
                "hint": f"sudo umount {device}"}
    res = _run(["udisksctl", "unmount", "-b", device, "--no-user-interaction"], 120)
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or "unmount failed"}
    # Powering off is best-effort: unmounted is already safe to remove.
    _run(["udisksctl", "power-off", "-b", device, "--no-user-interaction"], 60)
    return {"ok": True}


# -- planning ---------------------------------------------------------------

def _night_files(night_dir: Path, content: dict) -> list[Path]:
    """Which files a content selection pulls out of one night."""
    files: list[Path] = []
    if content.get("timelapse", True):
        files += sorted(night_dir.glob("timelapse_*.mp4"))
    if content.get("jpegs", False):
        files += sorted(night_dir.glob("img_*.jpg"))
        files += sorted(night_dir.glob("img_*.json"))   # sidecars travel with them
    if content.get("raws", False):
        files += sorted(night_dir.glob("img_*.dng"))
    return files


def plan(camera_id: str, nights: list[str], content: dict) -> dict:
    """What an export would copy, and how big it is."""
    root = config.IMAGE_ROOT
    items: list[tuple[Path, str]] = []
    total = 0
    for night in nights:
        night_dir = root / camera_id / night
        if not night_dir.is_dir():
            continue
        for src in _night_files(night_dir, content):
            rel = f"{EXPORT_DIRNAME}/{camera_id}/{night}/{src.name}"
            items.append((src, rel))
            total += src.stat().st_size
    return {"items": items, "bytes_total": total, "files_total": len(items)}


# -- the job ----------------------------------------------------------------

_lock = threading.Lock()
_thread: threading.Thread | None = None


def status() -> dict:
    path = config.RUN_DIR / STATUS_NAME
    if not path.exists():
        return {"state": "idle"}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"state": "idle"}


def _write_status(**fields) -> None:
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / STATUS_NAME).write_text(json.dumps(fields))


def start(device: str, camera_id: str, nights: list[str], content: dict) -> dict:
    """Validate, then run the copy on a background thread.

    Refuses up front when the drive cannot hold the selection — finding that
    out 40 GB in, with a half-written night on the stick, is the outcome this
    check exists to prevent.
    """
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return {"ok": False, "error": "An export is already running"}

        mountpoint = _drive(device)["mountpoint"]
        if not mountpoint:
            return {"ok": False, "error": "Drive is not mounted"}

        job = plan(camera_id, nights, content)
        if not job["files_total"]:
            return {"ok": False, "error": "Nothing to export for that selection"}

        free = shutil.disk_usage(mountpoint).free
        if job["bytes_total"] > free:
            return {"ok": False, "error": "Not enough space on the drive",
                    "required_bytes": job["bytes_total"], "free_bytes": free}

        _write_status(state="running", bytes_done=0,
                      bytes_total=job["bytes_total"], files_done=0,
                      files_total=job["files_total"], current_file="",
                      started=time.time(), device=device, mountpoint=mountpoint)
        _thread = threading.Thread(target=_run_job, daemon=True,
                                   args=(Path(mountpoint), job, device))
        _thread.start()
        return {"ok": True, "bytes_total": job["bytes_total"],
                "files_total": job["files_total"], "free_bytes": free}


def _copy(src: Path, dest: Path, done: int, total: int, files_done: int,
          files_total: int, device: str, mountpoint: str) -> int:
    """Chunked copy so progress moves during a 25 MB DNG, not just between files."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = 0.0
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        while chunk := fin.read(1024 * 1024):
            fout.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > _PROGRESS_INTERVAL_S:
                last = now
                _write_status(state="running", bytes_done=done, bytes_total=total,
                              files_done=files_done, files_total=files_total,
                              current_file=src.name, device=device,
                              mountpoint=mountpoint)
    shutil.copystat(src, dest)
    return done


def _run_job(mountpoint: Path, job: dict, device: str) -> None:
    done = 0
    total = job["bytes_total"]
    files_total = job["files_total"]
    try:
        for i, (src, rel) in enumerate(job["items"]):
            done = _copy(src, mountpoint / rel, done, total, i, files_total,
                         device, str(mountpoint))
        subprocess.run(["sync"], timeout=300)
        _write_status(state="done", bytes_done=done, bytes_total=total,
                      files_done=files_total, files_total=files_total,
                      current_file="", finished=time.time(), device=device,
                      mountpoint=str(mountpoint))
        log.info("Export finished: %d files, %.1f GB -> %s",
                 files_total, total / 1e9, mountpoint)
    except Exception as exc:
        log.exception("Export failed")
        _write_status(state="error", error=str(exc), bytes_done=done,
                      bytes_total=total, files_total=files_total,
                      finished=time.time(), device=device,
                      mountpoint=str(mountpoint))
