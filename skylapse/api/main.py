"""Skylapse API. Serves REST + the built React frontend.

Runs as its own service; talks to the daemon and netwatch only through
config.yaml and /run/skylapse status files — no direct coupling.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, config, notify, remote, updater, usbexport
from ..daemon.pipeline import process
from ..daemon.scheduler import profile_for
from ..daemon.watchdog import stall_threshold_s

app = FastAPI(title="Skylapse", version="0.1.0")

MAX_CLOCK_DRIFT_S = 5


def _read_status(name: str) -> dict:
    path = config.RUN_DIR / f"{name}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


# -- status ----------------------------------------------------------------

def night_bytes(night_dir: Path) -> int:
    """On-disk size of one night, thumbnails and all — this feeds the retention
    estimate, so it must count everything the night actually costs."""
    return sum(f.stat().st_size for f in night_dir.iterdir() if f.is_file())


# Used only when a rig has never written a DNG, so there is nothing to measure.
# Roughly a 12MP 16-bit sensor; the UI hedges the wording when this is in play.
RAW_BYTES_FALLBACK = 25_000_000

# Walking a 1200-frame night on every 5s status poll (per connected browser)
# is real work on a Pi, and the answer moves slowly. Cache it.
_RETENTION_TTL_S = 60
_retention_cache: dict = {"at": 0.0, "value": None}


def _retention(cfg: config.Config) -> dict:
    """How many more nights fit at the current consumption rate.

    Measured from the last *complete* night, since the night in progress is
    only partly written and would flatter the estimate. With no complete night
    yet we fall back to the night in progress and say so, rather than showing
    nothing — the point of the number is to make an expensive RAW policy
    visible immediately, which is exactly when no complete night exists.
    """
    now = time.time()
    if _retention_cache["value"] is not None and \
            now - _retention_cache["at"] < _RETENTION_TTL_S:
        return _retention_cache["value"]

    root = config.IMAGE_ROOT
    per_night = 0
    basis = None
    frames = 0
    non_raw = 0
    raw_sample = 0
    if root.exists():
        for cam in root.iterdir():
            if not cam.is_dir():
                continue
            nights = sorted((d for d in cam.iterdir() if d.is_dir()),
                            key=lambda d: d.name)
            measured = None
            if len(nights) >= 2:
                measured, basis = nights[-2], "complete"
            elif nights:
                measured, basis = nights[-1], basis or "in_progress"
            if measured is None:
                continue
            per_night += night_bytes(measured)
            frames += sum(1 for _ in measured.glob(FRAME_GLOB))
            raws = sorted(measured.glob("img_*.dng"))
            raw_bytes = sum(f.stat().st_size for f in raws)
            non_raw += night_bytes(measured) - raw_bytes
            if raws and not raw_sample:
                raw_sample = raws[-1].stat().st_size

    usable = 0.0
    if root.exists():
        usable = max(0.0, shutil.disk_usage(root).free / 1e9 - cfg.cleanup_free_gb)
    per_night_gb = per_night / 1e9

    # What every-frame RAW would actually cost *this* rig: the night's non-RAW
    # footprint plus one DNG per frame. Measured from a real DNG where one
    # exists, since sensor size and bit depth vary by an order of magnitude
    # across supported cameras and a generic figure would be wrong for most.
    raw_per_frame = raw_sample or RAW_BYTES_FALLBACK
    every_frame_gb = (non_raw + frames * raw_per_frame) / 1e9 if frames else None

    value = {
        "per_night_gb": round(per_night_gb, 2) if basis else None,
        # Headroom above the cleanup floor, not raw free space: below the floor
        # the oldest nights start being deleted, so that is the real ceiling.
        "nights_remaining": int(usable / per_night_gb) if per_night_gb > 0 else None,
        "basis": basis,
        "frames_per_night": frames or None,
        "raw_bytes_per_frame": raw_per_frame,
        "raw_measured": bool(raw_sample),
        "every_frame_raw_gb": round(every_frame_gb, 1) if every_frame_gb else None,
    }
    _retention_cache.update(at=now, value=value)
    return value


def _storage(cfg: config.Config) -> dict:
    """Free space, nights held, the cleanup floor, and how long the card lasts
    at the current rate — everything the dashboard's storage card needs, so it
    never has to guess how close it is to auto-deleting the oldest night."""
    root = config.IMAGE_ROOT
    if not root.exists():
        return {"free_gb": None, "total_gb": None, "nights": 0,
                "cleanup_free_gb": cfg.cleanup_free_gb,
                "per_night_gb": None, "nights_remaining": None, "basis": None}
    usage = shutil.disk_usage(root)
    nights = sum(1 for cam in root.iterdir() if cam.is_dir()
                 for night in cam.iterdir() if night.is_dir())
    return {
        "free_gb": round(usage.free / 1e9, 1),
        "total_gb": round(usage.total / 1e9, 1),
        "nights": nights,
        "cleanup_free_gb": cfg.cleanup_free_gb,
        **_retention(cfg),
    }


def _current(daemon: dict, cfg: config.Config) -> dict:
    """Which camera and night the UI should act on.

    Derived from the latest frame path rather than added to the daemon's
    status write: the daemon only records camera_id on camera open, and every
    per-frame write since then overwrites it. Reading it back out of the path
    keeps this an API concern and leaves the capture loop alone.
    """
    camera_id = night = ""
    latest = daemon.get("latest")
    if latest:
        path = Path(latest)
        night, camera_id = path.parent.name, path.parent.parent.name
    if not camera_id:
        camera_id = cfg.active_camera or next(iter(cfg.cameras), "")
    if camera_id and not night:
        cam_root = config.IMAGE_ROOT / camera_id
        if cam_root.is_dir():
            nights = sorted(d.name for d in cam_root.iterdir() if d.is_dir())
            night = nights[-1] if nights else ""
    has_timelapse = bool(camera_id and night and (
        config.IMAGE_ROOT / camera_id / night / f"timelapse_{night}.mp4").exists())
    # keeper_depth drives the dashboard button's label, so it must reflect the
    # configured value rather than the UI guessing a default.
    # Plain lookup, not cfg.camera(): that one is fetch-or-create and would
    # invent a registry entry on every status poll for an unknown id.
    entry = cfg.cameras.get(camera_id)
    depth = entry.raw.keeper_buffer_frames if entry else 3
    # The camera's name comes from the registry, not the daemon status: the
    # daemon records it once on open and every per-frame write since replaces
    # it, so the dashboard would show "None" for a rig that is capturing fine.
    name = (entry.label or entry.model) if entry else ""
    return {"camera_id": camera_id, "night": night, "timelapse": has_timelapse,
            "keeper_depth": depth, "camera_name": name}


# Counting a night's frames is a directory scan; frames arrive minutes apart at
# most, so a short cache costs nothing and saves repeating it per browser poll.
_FRAMES_TTL_S = 10
_frames_cache: dict = {"at": 0.0, "night": "", "count": 0}


def _frames_tonight(camera_id: str, night: str) -> int:
    now = time.time()
    if _frames_cache["night"] == night and now - _frames_cache["at"] < _FRAMES_TTL_S:
        return _frames_cache["count"]
    folder = config.IMAGE_ROOT / camera_id / night
    count = sum(1 for _ in folder.glob(FRAME_GLOB)) if folder.is_dir() else 0
    _frames_cache.update(at=now, night=night, count=count)
    return count


def _capture(cfg: config.Config, daemon: dict, current: dict) -> dict:
    """What the dashboard needs to judge liveness for itself.

    The pill is driven by frame freshness rather than the daemon's own state
    string, because "capturing" is what a wedged daemon reports right up until
    someone notices. Handing the client the same threshold the watchdog uses
    means the pill and the notification can never disagree about what counts as
    stalled.
    """
    cam = cfg.cameras.get(current.get("camera_id", ""))
    gap_s = exposure_us = None
    if cam is not None:
        profile = profile_for(cfg, cam)
        gap_s = profile.gap_s
        exposure_us = daemon.get("exposure_us") or profile.exposure_us
    return {
        "frame_at": daemon.get("frame_at"),
        "exposure_us": exposure_us,
        "gap_s": gap_s,
        "stall_threshold_s": (
            stall_threshold_s(gap_s, exposure_us)
            if gap_s is not None and exposure_us is not None else None),
        "frames_tonight": _frames_tonight(current.get("camera_id", ""),
                                          current.get("night", "")),
    }


@app.get("/api/status")
def status() -> dict:
    cfg = config.load()
    daemon = _read_status("daemon")
    current = _current(daemon, cfg)
    return {
        "daemon": daemon,
        "network": _read_status("netwatch"),
        "server_time": time.time(),
        "version": __version__,
        "setup_complete": cfg.setup_complete,
        "storage": _storage(cfg),
        "current": current,
        "capture": _capture(cfg, daemon, current),
        # Written by the daemon after it acts on a keeper request, so the UI can
        # report what was actually saved — POST /api/keeper returns immediately.
        "keeper": _read_status("keeper_result"),
    }


@app.get("/api/latest")
def latest_image():
    daemon = _read_status("daemon")
    path = daemon.get("latest")
    if not path or not Path(path).exists():
        raise HTTPException(404, "No frames captured yet")
    return FileResponse(path, media_type="image/jpeg")


# -- settings --------------------------------------------------------------

@app.get("/api/config")
def get_config() -> dict:
    cfg = config.load().model_dump()
    cfg["network"].pop("hotspot_password", None)   # never echo secrets
    return cfg


@app.get("/api/config/download")
def download_config():
    """The live config as YAML, for keeping somewhere the SD card isn't.

    The hotspot password is redacted: this file gets emailed to yourself and
    dropped in cloud storage, which is not where an AP credential belongs. The
    copy written to a USB stick during export is deliberately not redacted —
    that one is for restoring a rig, and it stays on your own drive.
    """
    data = config.load().model_dump()
    if data.get("network", {}).get("hotspot_password"):
        data["network"]["hotspot_password"] = "<redacted>"
    name = f"skylapse-config-{time.strftime('%Y-%m-%d')}.yaml"
    return Response(
        content=yaml.safe_dump(data, sort_keys=False),
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.put("/api/config")
def put_config(body: dict) -> dict:
    current = config.load()
    updated = current.model_copy(update=body, deep=True)
    config.save(config.Config.model_validate(updated.model_dump()))
    return {"ok": True}


# -- time sync (standalone mode) --------------------------------------------

class TimeSync(BaseModel):
    epoch_ms: int
    timezone: str            # IANA name from the browser


@app.post("/api/time/sync")
def time_sync(body: TimeSync) -> dict:
    """Browser-supplied clock. Applied only when we have no better source and
    drift exceeds threshold. Never steps backward mid-session (see DESIGN.md);
    the netwatch/timesync helper enforces source hierarchy — this endpoint
    just records the offer.
    """
    drift = abs(time.time() - body.epoch_ms / 1000.0)
    offer = {"epoch_ms": body.epoch_ms, "timezone": body.timezone,
             "drift_s": round(drift, 1), "received": time.time()}
    config.write_run_file("time_offer.json", json.dumps(offer))
    return {"applied_eligible": drift > MAX_CLOCK_DRIFT_S, **offer}


# -- white balance ----------------------------------------------------------
#
# Both endpoints read the preview buffer the capture loop leaves in /run: the
# most recent frame's raw mosaic, decimated but still a mosaic. The saved JPEG
# cannot answer either question — it already has the applied multipliers baked
# in, and whatever the previous setting clipped is gone for good.

WB_MIN, WB_MAX = 0.5, 3.0


def _clamp_wb(value: float) -> float:
    return max(WB_MIN, min(WB_MAX, float(value)))


@app.get("/api/wb/suggest")
def wb_suggest(camera_id: str = "") -> dict:
    """Grey-world multipliers from the current frame. Suggested, never applied.

    A seed rather than an answer: grey-world assumes the scene averages to
    grey, which a sky at dusk or a room lit by one warm lamp does not. The
    manual override is the feature; this is the starting point for it.
    """
    preview = process.read_wb_preview(config.RUN_DIR)
    if preview is None:
        raise HTTPException(404, "no frame captured yet")
    mosaic, meta = preview
    if camera_id and meta.get("camera_id") != camera_id:
        raise HTTPException(409, "the latest frame is from a different camera")

    means = process.plane_means(process.preview_frame(mosaic, meta))
    r, b = process.gray_world(means)
    return {
        "r": round(_clamp_wb(r), 3), "b": round(_clamp_wb(b), 3),
        # Unclamped alongside, so a suggestion that ran into the slider's end
        # is visible as such rather than looking like a considered answer.
        "raw_r": round(r, 3), "raw_b": round(b, 3),
        "clamped": r != _clamp_wb(r) or b != _clamp_wb(b),
        "means": dict(zip("rgb", (round(m, 1) for m in means))),
        "camera_id": meta.get("camera_id", ""),
        "frame_at": meta.get("timestamp"),
    }


@app.get("/api/wb/preview")
def wb_preview(r: float = 1.0, b: float = 1.0) -> Response:
    """The current frame rendered with the given multipliers, as a JPEG.

    Rendered from the raw mosaic through the same code the pipeline uses, so
    what the slider shows is what the next frame will be — including the
    highlights a stronger multiplier clips, which no amount of scaling the
    saved JPEG could reproduce.
    """
    preview = process.read_wb_preview(config.RUN_DIR)
    if preview is None:
        raise HTTPException(404, "no frame captured yet")
    mosaic, meta = preview
    jpeg = process.render_wb_preview(mosaic, meta, (_clamp_wb(r), _clamp_wb(b)))
    # No caching: the slider requests one of these per change, and a stale
    # render is worse than no render when the whole job is judging colour.
    return Response(jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# -- network actions --------------------------------------------------------

class WifiJoin(BaseModel):
    ssid: str
    password: str


@app.post("/api/network/join")
def network_join(body: WifiJoin) -> dict:
    """Hand credentials to netwatch via its command file. The UI has already
    warned the user this may disconnect their phone from the hotspot.
    """
    config.write_run_file("netwatch_cmd.json", json.dumps(
        {"cmd": "join", "ssid": body.ssid, "password": body.password}))
    return {"ok": True, "note": "Attempting connection; hotspot may drop for up to 90s"}


@app.post("/api/network/standalone")
def network_standalone(always: bool = False) -> dict:
    config.write_run_file("netwatch_cmd.json", json.dumps(
        {"cmd": "standalone", "always": always}))
    if always:
        cfg = config.load()
        cfg.network.mode = "standalone"
        config.save(cfg)
    return {"ok": True}


# Access-point mode is a manual override, distinct from the automatic fallback:
# the camera stays an access point until it is switched back, rather than
# waiting out a connect timeout. That is the whole point — someone standing at
# the camera with a phone should not have to wait for Wi-Fi to fail first.
AP_MODES = {"hotspot", "hotspot_timed"}


class NetworkMode(BaseModel):
    mode: str                    # auto | hotspot | hotspot_timed
    minutes: int = 120           # only read for hotspot_timed


@app.get("/api/network")
def network_state() -> dict:
    """Everything the UI needs to render the network card and badge."""
    status = _read_status("netwatch") or {}
    cfg = config.load()
    until = float(status.get("hotspot_until") or cfg.network.hotspot_until or 0)
    ap = status.get("state") in ("hotspot", "standalone")
    return {
        "state": status.get("state", "unknown"),
        # The UI speaks of access-point mode; "standalone" is the state
        # machine's name for the same thing and is not worth exposing.
        "mode": ("hotspot_timed" if cfg.network.mode == "standalone" and until
                 else "hotspot" if cfg.network.mode == "standalone" else "auto"),
        "access_point": ap,
        "hotspot_ssid": cfg.network.hotspot_ssid,
        "hotspot_secured": bool(cfg.network.hotspot_password),
        "clients": status.get("hotspot_clients", 0),
        "hotspot_until": until,
        "remaining_s": max(0, int(until - time.time())) if until else None,
        "ssid": _wifi_ssid(),
        "updated": status.get("updated"),
    }


def _wifi_ssid() -> str:
    """The network we are joined to, or "" when we are the access point.

    nmcli lists our own broadcast among the active networks, so serving the
    hotspot reads back as being joined to it — which had the Settings card
    reporting the camera was on a Wi-Fi network called Skylapse-Setup while it
    was in fact serving that name itself.
    """
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                             capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    hotspot = config.load().network.hotspot_ssid
    for line in out.splitlines():
        if line.startswith("yes:"):
            ssid = line.split(":", 1)[1]
            return "" if ssid == hotspot else ssid
    return ""


@app.post("/api/network/mode")
def network_set_mode(body: NetworkMode) -> dict:
    """Switch access-point mode on or off by hand.

    Persisted to config rather than sent as a command, so it survives a reboot
    and so netwatch applies it on its next poll without a restart.
    """
    if body.mode not in AP_MODES | {"auto"}:
        raise HTTPException(400, f"unknown mode {body.mode!r}")
    cfg = config.load()
    if body.mode == "auto":
        cfg.network.mode, cfg.network.hotspot_until = "auto", 0.0
        # A camera can also be an access point by *session* choice — the
        # fallback screen's "use in access point mode" deliberately leaves the
        # config alone. Clearing the persisted mode does nothing about that, so
        # "switch back to Wi-Fi" would silently do nothing at all in exactly
        # the case someone is most likely to press it. The retry command is
        # what revokes a session choice.
        config.write_run_file("netwatch_cmd.json", json.dumps({"cmd": "retry"}))
    else:
        cfg.network.mode = "standalone"
        cfg.network.hotspot_until = (
            time.time() + max(1, body.minutes) * 60
            if body.mode == "hotspot_timed" else 0.0)
    config.save(cfg)
    return {"ok": True, "mode": body.mode,
            "hotspot_until": cfg.network.hotspot_until,
            "note": ("Switching to the access point drops Wi-Fi within a few seconds"
                     if body.mode in AP_MODES else
                     "Rejoining Wi-Fi; the access point drops within a few seconds")}


@app.post("/api/network/retry")
def network_retry() -> dict:
    config.write_run_file("netwatch_cmd.json", json.dumps({"cmd": "retry"}))
    return {"ok": True}


@app.get("/api/network/scan")
def network_scan() -> list[dict]:
    """Visible networks via nmcli; netwatch guards decide if scanning is safe."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    nets, seen = [], set()
    for line in out.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] and parts[0] not in seen:
            seen.add(parts[0])
            nets.append({"ssid": parts[0], "signal": int(parts[1] or 0),
                         "secured": len(parts) > 2 and parts[2] != ""})
    return sorted(nets, key=lambda n: -n["signal"])


# -- nights browser -----------------------------------------------------------

# Thumbnails are written at capture time by the daemon (process.save_jpeg).
# The constants live there; these names are re-exported for the tests and the
# backfill path below.
THUMB_PX = process.THUMB_PX
THUMB_PREFIX = process.THUMB_PREFIX

# A night is one folder holding frames, sidecars, thumbnails and the mp4.
# Everything that indexes frames filters on img_*.jpg specifically, so
# thumb_*.jpg can live beside them without polluting the index or star counts.
FRAME_GLOB = "img_*.jpg"


def _night_dir(camera_id: str, night: str) -> Path:
    """Resolve a night folder, refusing anything that escapes the image root.

    camera_id and night arrive from the URL, so '..' segments would otherwise
    let a request walk out of the store and serve arbitrary files.
    """
    root = config.IMAGE_ROOT.resolve()
    path = (root / camera_id / night).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(400, "Invalid path")
    if not path.is_dir():
        raise HTTPException(404, "No such night")
    return path


@app.get("/api/nights/{camera_id}")
def nights(camera_id: str) -> list[dict]:
    """Every night held for a camera, newest first."""
    root = config.IMAGE_ROOT.resolve()
    cam_root = (root / camera_id).resolve()
    if not cam_root.is_relative_to(root) or not cam_root.is_dir():
        raise HTTPException(404, "No such camera")

    out = []
    for night in sorted((d for d in cam_root.iterdir() if d.is_dir()),
                        key=lambda d: d.name, reverse=True):
        frames = sorted(night.glob(FRAME_GLOB))
        if not frames:
            continue
        out.append({
            "night": night.name,
            "frames": len(frames),
            "first": frames[0].stat().st_mtime,
            "last": frames[-1].stat().st_mtime,
            "has_timelapse": (night / f"timelapse_{night.name}.mp4").exists(),
            "bytes": night_bytes(night),
        })
    return out


@app.get("/api/nights/{camera_id}/{night}/frames")
def night_frames(camera_id: str, night: str, offset: int = 0,
                 limit: int = 500) -> dict:
    """Windowed frame index. A night can run past 1200 frames, so this is
    paginated rather than returned whole — the filmstrip fetches what it needs."""
    folder = _night_dir(camera_id, night)
    frames = sorted(folder.glob(FRAME_GLOB))
    total = len(frames)
    limit = max(1, min(limit, 2000))
    offset = max(0, min(offset, total))
    window = frames[offset:offset + limit]

    items = []
    for jpeg in window:
        meta = {}
        sidecar = jpeg.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text())
            except json.JSONDecodeError:
                meta = {}
        items.append({
            "name": jpeg.name,
            "timestamp": meta.get("timestamp", jpeg.stat().st_mtime),
            "stars": meta.get("stars"),
            "has_dng": jpeg.with_suffix(".dng").exists(),
            "exposure_us": meta.get("exposure_us"),
            "gain": meta.get("gain"),
        })
    return {"night": night, "total": total, "offset": offset,
            "limit": limit, "frames": items}


@app.get("/api/nights/{camera_id}/{night}/frame/{name}")
def night_frame(camera_id: str, night: str, name: str, thumb: bool = False):
    """One JPEG, or its filmstrip thumbnail.

    Thumbnails are normally written at capture time, so this just serves them.
    The generate-on-miss path remains as backfill for nights captured before
    that existed — and it is a genuine miss path, not the common one: decoding
    a full-res frame per thumbnail is exactly what made the first scrub slow.
    """
    folder = _night_dir(camera_id, night)
    jpeg = (folder / name).resolve()
    if not jpeg.is_relative_to(folder) or not jpeg.is_file() \
            or not jpeg.name.startswith("img_") or jpeg.suffix != ".jpg":
        raise HTTPException(404, "No such frame")
    if not thumb:
        return FileResponse(jpeg, media_type="image/jpeg")

    thumbnail = process.thumb_path(jpeg)
    if not thumbnail.exists():
        import cv2
        img = cv2.imread(str(jpeg))
        if img is None:
            raise HTTPException(500, "Could not read frame")
        process.write_thumb(img, thumbnail)
    return FileResponse(thumbnail, media_type="image/jpeg")


@app.get("/api/nights/{camera_id}/{night}/raw/{name}")
def night_raw(camera_id: str, night: str, name: str):
    """One DNG, as a download — browsers would otherwise try to render it."""
    folder = _night_dir(camera_id, night)
    dng = (folder / name).resolve()
    if not dng.is_relative_to(folder) or not dng.is_file() \
            or not dng.name.startswith("img_") or dng.suffix != ".dng":
        raise HTTPException(404, "No such raw file")
    return FileResponse(dng, media_type="image/x-adobe-dng",
                        filename=dng.name, content_disposition_type="attachment")


# -- sky quality --------------------------------------------------------------

@app.get("/api/stars/{camera_id}/{night}")
def star_history(camera_id: str, night: str) -> list[dict]:
    """Per-frame star counts from the sidecars: the sky-quality trend chart.
    A cratering count = clouds rolled in."""
    folder = config.IMAGE_ROOT / camera_id / night
    if not folder.is_dir():
        raise HTTPException(404, "No such night")
    out = []
    for sidecar in sorted(folder.glob("img_*.json")):
        try:
            meta = json.loads(sidecar.read_text())
        except json.JSONDecodeError:
            continue
        if meta.get("stars") is not None:
            out.append({"t": meta["timestamp"], "stars": meta["stars"]})
    return out


# -- focus assist -------------------------------------------------------------

@app.post("/api/focus/start")
def focus_start() -> dict:
    """Rapid capture + live sharpness score; auto-exits after 15 min.
    Poll /api/status for {score, best, trend} while turning the ring."""
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "focus_start").touch()
    return {"ok": True}


@app.post("/api/focus/stop")
def focus_stop() -> dict:
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "focus_stop").touch()
    return {"ok": True}


class FocusControls(BaseModel):
    exposure_ms: int = 500
    gain: int = 250


@app.post("/api/focus/controls")
def focus_controls(body: FocusControls) -> dict:
    """Exposure/gain for the live view. The daemon re-reads this before every
    focus frame, so a slider move applies on the next capture."""
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"exposure_ms": max(1, body.exposure_ms), "gain": max(0, body.gain)}
    config.write_run_file("focus_ctl.json", json.dumps(payload))
    return {"ok": True, **payload}


ZOOM_LEVELS = (1, 2, 4, 8, 10)


@app.get("/api/focus/live")
def focus_live(zoom: int = 1, cx: float = 0.5, cy: float = 0.5):
    """The latest focus frame, optionally cropped.

    Cropping happens here, on the full-resolution frame, and the crop is
    returned at native scale. That is what makes 8x and 10x worth having: they
    show real sensor detail. Scaling a downsized preview up in the browser
    would just show bigger blur and tell you nothing about focus.
    """
    path = config.RUN_DIR / "focus_full.jpg"
    if not path.is_file():
        raise HTTPException(404, "No focus frame yet")

    # Never cached: the whole file is one frame of a live view, and the URL is
    # stable, so without this the browser would happily show a stale frame.
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate",
               "Pragma": "no-cache"}

    zoom = zoom if zoom in ZOOM_LEVELS else 1
    if zoom == 1:
        return FileResponse(path, media_type="image/jpeg", headers=headers)

    import cv2
    img = cv2.imread(str(path))
    if img is None:
        raise HTTPException(503, "Focus frame not readable yet")
    h, w = img.shape[:2]
    cw, ch = max(1, w // zoom), max(1, h // zoom)
    # Clamp the window so a pan to the edge slides rather than falling off.
    x0 = min(max(0, int(cx * w) - cw // 2), w - cw)
    y0 = min(max(0, int(cy * h) - ch // 2), h - ch)
    ok, buf = cv2.imencode(".jpg", img[y0:y0 + ch, x0:x0 + cw],
                           [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise HTTPException(500, "Could not encode crop")
    return Response(content=buf.tobytes(), media_type="image/jpeg", headers=headers)


# -- capture control ---------------------------------------------------------

@app.post("/api/capture/resume")
def capture_resume() -> dict:
    """Lift a manual-exposure safety pause."""
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "resume_cmd").touch()
    return {"ok": True}


# -- timelapse ---------------------------------------------------------------

@app.get("/api/timelapse/{camera_id}/{night}")
def timelapse_file(camera_id: str, night: str):
    """Serve a night's rendered mp4 so the dashboard can play it inline.

    Declared before the render route only for readability — they never
    collide, since this is GET and the render endpoint is POST under a
    literal /render/ segment.
    """
    path = config.IMAGE_ROOT / camera_id / night / f"timelapse_{night}.mp4"
    if not path.is_file():
        raise HTTPException(404, "No timelapse for that night")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/timelapse/render/{camera_id}/{night}")
def render_timelapse(camera_id: str, night: str, force: bool = False,
                     clip_seconds: int | None = None,
                     quality: str | None = None) -> dict:
    """On-demand render. force=true re-renders; clip_seconds/quality are
    ONE-OFF overrides for this render only — saved settings are untouched,
    so the dashboard can offer 'make this one 60s' without a settings trip."""
    from ..daemon import nightjobs
    folder = config.IMAGE_ROOT / camera_id / night
    if not folder.is_dir():
        raise HTTPException(404, "No such night")
    cfg = config.load()
    settings = cfg.camera(camera_id).timelapse.model_copy()
    if clip_seconds is not None:
        settings.clip_seconds = max(5, min(600, clip_seconds))
    if quality in ("standard", "high", "max"):
        settings.quality = quality
    out = nightjobs.render_night(folder, settings, force=force)
    if out is None:
        raise HTTPException(422, "Not enough frames or ffmpeg unavailable")
    return {"ok": True, "file": out.name}


# -- keeper button ------------------------------------------------------------

@app.post("/api/keeper")
def save_keeper() -> dict:
    """Ask the daemon to dump its rolling raw buffer to DNG files."""
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "keeper_cmd").touch()
    return {"ok": True, "note": "Buffered frames will be saved as DNG"}


# -- notifications -----------------------------------------------------------

@app.post("/api/notify/test")
def notify_test() -> dict:
    """Fires a test notification through the real path (honors master switch)."""
    sent = notify.notify("test", "Skylapse", "Test notification — you're all set.")
    return {"sent": sent}


@app.post("/api/notify/generate-topic")
def notify_generate_topic() -> dict:
    """Create a private random ntfy topic and persist it."""
    import secrets
    cfg = config.load()
    if not cfg.notifications.ntfy_topic:
        cfg.notifications.ntfy_topic = f"skylapse-{secrets.token_hex(4)}"
        config.save(cfg)
    return {"topic": cfg.notifications.ntfy_topic,
            "subscribe_url": f"{cfg.notifications.ntfy_server.rstrip('/')}/"
                             f"{cfg.notifications.ntfy_topic}"}


# -- USB export ---------------------------------------------------------------

class ExportRequest(BaseModel):
    device: str
    camera_id: str
    nights: list[str]
    content: dict = {"timelapse": True, "jpegs": False, "raws": False}


@app.get("/api/export/drives")
def export_drives() -> list[dict]:
    """USB drives eligible as export targets. The boot medium is never listed."""
    return usbexport.list_drives()


@app.post("/api/export/mount")
def export_mount(device: str) -> dict:
    try:
        return usbexport.mount(device)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/export/plan")
def export_plan(body: ExportRequest) -> dict:
    """Size a selection without copying, so the UI can show per-night cost."""
    job = usbexport.plan(body.camera_id, body.nights, body.content)
    return {"bytes_total": job["bytes_total"], "files_total": job["files_total"]}


@app.post("/api/export/start")
def export_start(body: ExportRequest) -> dict:
    try:
        result = usbexport.start(body.device, body.camera_id, body.nights,
                                 body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(409, result)
    return result


@app.get("/api/export/status")
def export_status() -> dict:
    return usbexport.status()


@app.post("/api/export/eject")
def export_eject(device: str) -> dict:
    return usbexport.eject(device)


# -- updates ------------------------------------------------------------------

@app.get("/api/update/check")
def update_check(force: bool = False) -> dict:
    """What is available. Cached for a day unless force=true."""
    return updater.check(force=force)


@app.get("/api/update/status")
def update_status() -> dict:
    return updater.status()


class UpdateRequest(BaseModel):
    target_ref: str
    apply_now: bool = False


@app.post("/api/update/apply")
def update_apply(body: UpdateRequest) -> dict:
    """Kick off an update. The work happens in a detached process, because
    applying restarts this one."""
    result = updater.start(body.target_ref, apply_now=body.apply_now)
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "Could not start update"))
    return result


# -- remote access (Tailscale) ----------------------------------------------

@app.get("/api/remote/status")
def remote_status() -> dict:
    st = remote.status()
    if st.get("auth_url"):
        st["qr_svg"] = remote.qr_svg(st["auth_url"])
    return st


@app.post("/api/remote/enable")
def remote_enable() -> dict:
    return remote.enable()


# -- static frontend (mounted last so /api wins) -----------------------------

_web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if _web_dist.exists():
    app.mount("/", StaticFiles(directory=_web_dist, html=True), name="web")
