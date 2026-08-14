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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, notify, remote

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
    if root.exists():
        for cam in root.iterdir():
            if not cam.is_dir():
                continue
            nights = sorted((d for d in cam.iterdir() if d.is_dir()),
                            key=lambda d: d.name)
            if len(nights) >= 2:
                per_night += night_bytes(nights[-2])
                basis = "complete"
            elif nights:
                per_night += night_bytes(nights[-1])
                basis = basis or "in_progress"

    usable = 0.0
    if root.exists():
        usable = max(0.0, shutil.disk_usage(root).free / 1e9 - cfg.cleanup_free_gb)
    per_night_gb = per_night / 1e9
    value = {
        "per_night_gb": round(per_night_gb, 2) if basis else None,
        # Headroom above the cleanup floor, not raw free space: below the floor
        # the oldest nights start being deleted, so that is the real ceiling.
        "nights_remaining": int(usable / per_night_gb) if per_night_gb > 0 else None,
        "basis": basis,
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
    return {"camera_id": camera_id, "night": night, "timelapse": has_timelapse}


@app.get("/api/status")
def status() -> dict:
    cfg = config.load()
    daemon = _read_status("daemon")
    return {
        "daemon": daemon,
        "network": _read_status("netwatch"),
        "server_time": time.time(),
        "setup_complete": cfg.setup_complete,
        "storage": _storage(cfg),
        "current": _current(daemon, cfg),
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
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "time_offer.json").write_text(json.dumps(offer))
    return {"applied_eligible": drift > MAX_CLOCK_DRIFT_S, **offer}


# -- network actions --------------------------------------------------------

class WifiJoin(BaseModel):
    ssid: str
    password: str


@app.post("/api/network/join")
def network_join(body: WifiJoin) -> dict:
    """Hand credentials to netwatch via its command file. The UI has already
    warned the user this may disconnect their phone from the hotspot.
    """
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "netwatch_cmd.json").write_text(json.dumps(
        {"cmd": "join", "ssid": body.ssid, "password": body.password}))
    return {"ok": True, "note": "Attempting connection; hotspot may drop for up to 90s"}


@app.post("/api/network/standalone")
def network_standalone(always: bool = False) -> dict:
    (config.RUN_DIR / "netwatch_cmd.json").write_text(json.dumps(
        {"cmd": "standalone", "always": always}))
    if always:
        cfg = config.load()
        cfg.network.mode = "standalone"
        config.save(cfg)
    return {"ok": True}


@app.post("/api/network/retry")
def network_retry() -> dict:
    (config.RUN_DIR / "netwatch_cmd.json").write_text(json.dumps({"cmd": "retry"}))
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

THUMB_PX = 256
THUMB_PREFIX = "thumb_"

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
    """One JPEG. thumb=true serves a ~256px version, generated on first request
    and cached beside the frame — shipping 1200 full-res frames to a phone for a
    filmstrip is not viable, and pre-generating them would stall the capture loop.
    """
    folder = _night_dir(camera_id, night)
    jpeg = (folder / name).resolve()
    if not jpeg.is_relative_to(folder) or not jpeg.is_file() \
            or not jpeg.name.startswith("img_") or jpeg.suffix != ".jpg":
        raise HTTPException(404, "No such frame")
    if not thumb:
        return FileResponse(jpeg, media_type="image/jpeg")

    thumbnail = folder / f"{THUMB_PREFIX}{jpeg.stem}.jpg"
    if not thumbnail.exists():
        import cv2
        img = cv2.imread(str(jpeg))
        if img is None:
            raise HTTPException(500, "Could not read frame")
        h, w = img.shape[:2]
        scale = THUMB_PX / max(h, w)
        small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(thumbnail), small, [cv2.IMWRITE_JPEG_QUALITY, 80])
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
