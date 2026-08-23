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

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import (__version__, auth, config, notify, remote, setup, updater,
                usbexport, zwosdk)
from ..daemon.pipeline import process
from ..daemon.scheduler import profile_for
from ..daemon.watchdog import stall_threshold_s

app = FastAPI(title="Skylapse", version="0.1.0")

MAX_CLOCK_DRIFT_S = 5


# -- setup wizard -----------------------------------------------------------

@app.get("/api/setup/draft")
def setup_draft() -> dict:
    """The wizard's answers so far, seeded from config on first read."""
    return {"complete": config.load().setup_complete,
            "steps": list(setup.STEPS), "draft": setup.load_draft()}


@app.patch("/api/setup/draft")
def setup_patch(patch: dict) -> dict:
    """One screen's answers. Merged section-wise so going Back and forward
    again cannot blank the screens after it."""
    return {"draft": setup.save_draft(setup.merge(setup.load_draft(), patch))}


@app.post("/api/setup/complete")
def setup_finish(body: dict | None = None) -> dict:
    """Commit the draft and mark setup done.

    Order matters and is DESIGN.md guard 4: the config and the flag are written
    in one atomic save, *before* the final screen is rendered. A power cut at
    the wrong instant then leaves a configured camera rather than one that
    starts the wizard again at every boot.

    The password is applied here rather than on the security screen so that a
    wizard abandoned halfway never leaves a camera locked with a password
    nobody finished choosing.
    """
    draft = setup.merge(setup.load_draft(), body or {})
    cfg = setup.apply_draft(draft, config.load())

    password = (draft.get("security") or {}).get("password") or ""
    if password:
        if len(password) < auth.MIN_PASSWORD_CHARS:
            raise HTTPException(400, "password is too short")
        try:
            cfg.auth.password_hash = auth.hash_password(password)
        except auth.PasswordTooLong as exc:
            raise HTTPException(400, str(exc)) from exc
        cfg.auth.session_secret = auth.new_secret()
        cfg.auth.public_live_view = bool(
            (draft.get("security") or {}).get("public_live_view"))

    cfg.setup_complete = True
    config.save(cfg)
    setup.clear_draft()

    response = JSONResponse({"ok": True, "summary": _setup_summary(cfg)})
    if password:
        # Whoever just set the password is holding the phone; logging them out
        # of the camera they have this second finished setting up would be a
        # remarkable way to end a wizard.
        _set_session(response, cfg)
    return response


@app.post("/api/setup/reset")
def setup_reset() -> dict:
    """Run setup again. Clears the flag; the draft reseeds from current config,
    so nothing is blanked before anything is answered."""
    cfg = config.load()
    cfg.setup_complete = False
    config.save(cfg)
    setup.clear_draft()
    return {"ok": True}


@app.get("/api/setup/location/estimate")
def setup_location_estimate() -> dict:
    """City-level location from the public IP. The middle rung of the cascade.

    Asked for only after the browser's own geolocation has been declined or has
    failed, and answered from the camera rather than the phone because in the
    hotspot case the phone has no route to the internet either — but the camera
    might, over ethernet.

    Approximate by construction and labelled as such: it locates the ISP's exit,
    which can be a different city. Good enough to put sunset within minutes,
    which is all the scheduler needs.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(
                "http://ip-api.com/json/?fields=status,lat,lon,timezone,city,regionName",
                timeout=6) as response:
            data = json.loads(response.read().decode())
    except Exception:
        raise HTTPException(503, "no internet connection to estimate from")
    if data.get("status") != "success":
        raise HTTPException(503, "location lookup failed")
    return {"latitude": data.get("lat"), "longitude": data.get("lon"),
            "timezone": data.get("timezone"),
            "place": ", ".join(p for p in (data.get("city"),
                                           data.get("regionName")) if p),
            "approximate": True, "source": "ip"}


@app.get("/api/setup/location/check")
def setup_location_check(latitude: float, longitude: float) -> dict:
    """What a location implies, so the screen can prove it matters.

    Sunset and the aurora threshold are the two things a wrong location breaks
    quietly — the camera would simply idle at the wrong hours and never alert.
    Showing them turns "type your coordinates" into something checkable.
    """
    from ..daemon.aurora import kp_threshold
    from ..daemon.scheduler import next_dusk

    cfg = config.load().model_copy(deep=True)
    cfg.location.latitude, cfg.location.longitude = latitude, longitude
    sunset = next_dusk(cfg)
    # 0,0 is in the Gulf of Guinea and is what an unset config looks like, so
    # it is the one value most likely to be wrong by accident rather than
    # because someone is on a boat.
    null_island = abs(latitude) < 0.5 and abs(longitude) < 0.5
    return {
        "sunset": sunset.timestamp() if sunset else None,
        "kp_threshold": kp_threshold(latitude),
        "null_island": null_island,
        "out_of_range": not (-90 <= latitude <= 90 and -180 <= longitude <= 180),
    }


@app.post("/api/setup/camera/test")
def setup_camera_test() -> dict:
    """Detect cameras and take one real frame from the chosen one.

    A test shot is the only part of setup that proves the hardware works rather
    than merely enumerates. It goes through the focus preview path, which
    writes to /run and never to the image store — a setup frame must not turn
    up in someone's first night.
    """
    config.write_run_file("focus_cmd.json", json.dumps(
        {"cmd": "test_shot", "at": time.time()}))
    return {"ok": True,
            "note": "capture requested; poll /api/setup/camera for the result"}


@app.get("/api/setup/camera")
def setup_camera() -> dict:
    """What the daemon can see, and whether a test shot is waiting."""
    status = _read_status("daemon") or {}
    cfg = config.load()
    cameras = [{"camera_id": cam_id,
                "label": entry.label or entry.model or cam_id,
                "driver": entry.driver}
               for cam_id, entry in cfg.cameras.items()]
    shot = config.RUN_DIR / "setup_shot.jpg"
    return {
        "cameras": cameras,
        "active": cfg.active_camera or status.get("camera_id", ""),
        "detected": status.get("camera_id", ""),
        "state": status.get("state", "unknown"),
        "shot_at": shot.stat().st_mtime if shot.exists() else None,
    }


# Sensors whose overlays ship with Raspberry Pi OS, in the order someone is
# likely to own one. Mirrors the closed list in scripts/skylapse-admin, which
# is the one that actually enforces it — this is only what the UI offers.
CAMERA_SENSORS = [
    ("imx477", "HQ Camera / IMX477 (incl. Arducam)"),
    ("imx708", "Camera Module 3 / IMX708"),
    ("imx219", "Camera Module 2 / IMX219"),
    ("ov5647", "Camera Module 1 / OV5647"),
    ("imx519", "Arducam 16MP / IMX519"),
    ("imx296", "Global Shutter / IMX296"),
]


@app.get("/api/setup/camera/sensors")
def setup_camera_sensors() -> dict:
    return {"sensors": [{"value": v, "label": l} for v, l in CAMERA_SENSORS]}


class CameraOverlay(BaseModel):
    sensor: str


@app.post("/api/setup/camera/overlay")
def setup_camera_overlay(body: CameraOverlay) -> dict:
    """Declare a sensor in the boot config, then reboot.

    Auto-detect reads an EEPROM that third-party boards do not carry, so a
    perfectly good HQ-clone camera is simply invisible on a stock image — and
    the only fix was editing /boot/firmware/config.txt over SSH, which is the
    exact thing an appliance image exists to avoid.

    Both ports are declared, because which connector a camera is in is not
    something anyone should have to know. The empty port fails its probe
    harmlessly; that is what a working rig's log looks like.
    """
    if body.sensor not in {v for v, _ in CAMERA_SENSORS}:
        raise HTTPException(400, f"unknown sensor {body.sensor!r}")
    helper = str(Path(__file__).resolve().parents[2] / "scripts" / "skylapse-admin")
    result = subprocess.run(["sudo", "-n", helper, "camera-overlay", body.sensor],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise HTTPException(500, f"could not write the boot config: "
                                 f"{(result.stderr or result.stdout).strip()[:200]}")
    subprocess.run(["sudo", "-n", helper, "reboot"],
                   capture_output=True, text=True, timeout=30)
    return {"ok": True, "sensor": body.sensor,
            "note": "rebooting; the camera is checked again on the way back up"}


# -- ZWO support (optional, on demand) --------------------------------------

@app.get("/api/setup/zwo")
def setup_zwo_status() -> dict:
    """Whether ZWO support can be installed here, and whether it already is."""
    return zwosdk.status()


class ZwoInstall(BaseModel):
    # An explicit tick, not a default. The camera is about to download a
    # proprietary binary on the user's behalf under a licence that is theirs to
    # accept, so the request has to carry the acceptance rather than the server
    # assuming it.
    accept_terms: bool = False


@app.post("/api/setup/zwo/install")
def setup_zwo_install(body: ZwoInstall) -> dict:
    """Download and install ZWO's SDK, then restart the daemon.

    The one thing standing between a ZWO rig and an SSH-free setup: their
    licence forbids redistributing the library, so it cannot be in the image,
    so until now it had to be fetched by hand over a terminal. Doing it here
    redistributes nothing — the user asks, accepts ZWO's terms, and the file
    comes from the INDI project's mirror, pinned and checksummed in
    scripts/skylapse-admin.
    """
    if not body.accept_terms:
        raise HTTPException(400, "ZWO's licence has to be accepted first")
    result = zwosdk.install()
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "could not install the SDK"))
    return result


@app.get("/api/setup/camera/shot")
def setup_camera_shot() -> Response:
    shot = config.RUN_DIR / "setup_shot.jpg"
    if not shot.exists():
        raise HTTPException(404, "no test shot yet")
    return FileResponse(shot, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


def _setup_summary(cfg: config.Config) -> dict:
    """What the final screen states back. Read from the saved config rather
    than the draft, so it reflects what was actually written."""
    from ..daemon.aurora import kp_threshold

    camera_id = cfg.active_camera or next(iter(cfg.cameras), "")
    entry = cfg.cameras.get(camera_id)
    return {
        "camera": (entry.label or entry.model or camera_id) if entry else "",
        "latitude": cfg.location.latitude,
        "longitude": cfg.location.longitude,
        "timezone": cfg.location.timezone,
        "schedule": entry.capture_schedule if entry else "always",
        "raw_mode": entry.raw.mode if entry else "off",
        "protected": bool(cfg.auth.password_hash),
        "network_mode": cfg.network.mode,
        "hotspot_ssid": cfg.network.hotspot_ssid,
        "kp_threshold": kp_threshold(cfg.location.latitude),
    }


# -- access control ---------------------------------------------------------
#
# Off unless a password has been set, which is the default. When it is set it
# protects the API; the SPA is always served, because the login screen is part
# of the SPA and a device that refused to serve its own login page would be
# unrecoverable from a phone.

# Reachable without a session even when a password is set. Login obviously, and
# the status probe the login screen itself needs to know whether to render.
_OPEN_PATHS = {"/api/auth/status", "/api/auth/login"}

# Additionally readable when "public live view" is on: the latest frame and
# enough status to caption it. Controls and settings stay locked either way.
_PUBLIC_PATHS = {"/api/status", "/api/latest"}


def _authorised(request: Request, cfg: config.Config) -> bool:
    if not cfg.auth.password_hash:
        return True
    path = request.url.path
    if path in _OPEN_PATHS or not path.startswith("/api/"):
        return True
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    if auth.token_valid(token, cfg.auth.session_secret):
        return True
    # The public sub-toggle is read-only by construction: a GET, on a named
    # path. Anything that changes the camera needs a session regardless.
    return (cfg.auth.public_live_view and request.method == "GET"
            and path in _PUBLIC_PATHS)


@app.middleware("http")
async def require_password(request: Request, call_next):
    try:
        cfg = config.load()
    except Exception:
        # A config we cannot read is not a reason to lock everyone out of the
        # camera; it is a reason to let them in and see the error.
        return await call_next(request)
    if not _authorised(request, cfg):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    password: str


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    """What the SPA needs to decide between the login screen and the app."""
    cfg = config.load()
    return {
        "password_set": bool(cfg.auth.password_hash),
        "public_live_view": cfg.auth.public_live_view,
        "authenticated": _authorised(request, cfg),
    }


@app.post("/api/auth/login")
def auth_login(body: Login, response: Response) -> dict:
    cfg = config.load()
    if not cfg.auth.password_hash:
        return {"ok": True, "note": "no password is set on this camera"}
    if not auth.verify_password(body.password, cfg.auth.password_hash):
        raise HTTPException(401, "wrong password")
    _set_session(response, cfg)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


class PasswordChange(BaseModel):
    # Three states, deliberately distinct: absent leaves the password alone
    # (for changing only the sub-toggle), empty removes it, anything else sets
    # it. Defaulting absent to "remove" would let a request that simply forgot
    # the field silently unprotect the camera.
    password: str | None = None
    current: str = ""               # required once one is set
    public_live_view: bool | None = None


@app.post("/api/auth/password")
def auth_set_password(body: PasswordChange, response: Response) -> dict:
    """Set, change or remove the password.

    Changing it requires the current one even though the caller already holds a
    session: a phone left unlocked on a bench is the realistic threat here, and
    it is the one moment where asking again costs nothing.
    """
    cfg = config.load()
    if cfg.auth.password_hash and not auth.verify_password(
            body.current, cfg.auth.password_hash):
        raise HTTPException(403, "current password required")

    if body.password is None:
        pass                        # only the sub-toggle is being changed
    elif body.password:
        if len(body.password) < auth.MIN_PASSWORD_CHARS:
            raise HTTPException(400, "password is too short")
        try:
            cfg.auth.password_hash = auth.hash_password(body.password)
        except auth.PasswordTooLong as exc:
            raise HTTPException(400, str(exc)) from exc
        # A fresh secret on every change, so removing and re-setting a password
        # invalidates the sessions the old one had handed out.
        cfg.auth.session_secret = auth.new_secret()
    else:
        cfg.auth.password_hash = ""
        cfg.auth.session_secret = ""
        cfg.auth.public_live_view = False

    if body.public_live_view is not None:
        cfg.auth.public_live_view = body.public_live_view
    config.save(cfg)

    if cfg.auth.password_hash and body.password:
        # A new secret was issued, so the caller's own cookie is now stale.
        # Re-issuing here is what stops someone being logged out of the camera
        # they have this second finished securing.
        _set_session(response, cfg)
    elif not cfg.auth.password_hash:
        response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True, "password_set": bool(cfg.auth.password_hash),
            "public_live_view": cfg.auth.public_live_view}


def _set_session(response: Response, cfg: config.Config) -> None:
    """Long-lived so it is once per device, not once per visit.

    Not `secure`: this is plain HTTP on a LAN by design (DESIGN.md rules out
    self-signed certs), and a secure-only cookie would simply never be sent.
    """
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_token(cfg.auth.session_secret),
        max_age=auth.SESSION_SECONDS, httponly=True, samesite="lax")


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
    """Patch config. Top-level keys are replaced; the camera registry merges.

    model_copy(update=) replaces a key wholesale, which for `cameras` means a
    patch naming one camera deletes every other one. The settings screen works
    around it by resending the entire registry with every edit, but that is a
    workaround for a sharp edge rather than a design — and anything else that
    touches this endpoint, a script or the wizard to come, gets no such
    protection. Setting one multiplier on one camera cost this rig its second
    camera's registry entry, which is how the edge was found.

    Merging is per camera, not deep within a camera: the settings screen sends
    a whole camera entry and a partial merge there would make it impossible to
    ever clear a field.
    """
    current = config.load()
    body = dict(body)
    if isinstance(body.get("cameras"), dict):
        merged = dict(current.cameras)
        for cam_id, patch in body["cameras"].items():
            existing = merged.get(cam_id)
            merged[cam_id] = config.CameraEntry.model_validate(
                {**(existing.model_dump() if existing else {}), **patch})
        body["cameras"] = merged
    updated = current.model_copy(update=body, deep=True)
    config.save(config.Config.model_validate(updated.model_dump()))
    return {"ok": True}


# -- dew heater ---------------------------------------------------------------

@app.get("/api/dewheater")
def dewheater_status() -> dict:
    """Everything the dew heater card needs to know what to offer.

    Deliberately answers on a camera with no sensor, no I2C and the feature off:
    each of those is a different next step, and a card that cannot tell them
    apart can only say "not working".
    """
    from ..daemon import dewheater

    cfg = config.load()
    bus_ready = Path("/dev/i2c-1").exists()
    sensor = None
    if bus_ready:
        probe = dewheater.DewHeater(cfg.dew_heater.gpio_pin,
                                    cfg.dew_heater.on_margin_c,
                                    cfg.dew_heater.off_margin_c)
        sensor = probe.available
    return {
        "enabled": cfg.dew_heater.experimental_enabled,
        "i2c_ready": bus_ready,
        "sensor_found": sensor,
        "gpio_pin": cfg.dew_heater.gpio_pin,
        "on_margin_c": cfg.dew_heater.on_margin_c,
        "off_margin_c": cfg.dew_heater.off_margin_c,
        # What the daemon last measured, if it is running the heater at all.
        "reading": (_read_status("daemon") or {}).get("dew"),
    }


@app.post("/api/system/reboot")
def system_reboot() -> dict:
    """Restart the camera, gracefully.

    The helper has had a `reboot` verb since the camera-overlay flow needed
    one, but nothing else could reach it, so every other "reboot to finish"
    ended with the user pulling the power. That is not the same operation: a
    cold pull skips the filesystem sync, and doing it seconds after a boot
    config write is a good way to corrupt the card. An appliance that asks to
    be restarted has to be able to restart itself.

    The helper schedules the reboot two seconds out, so this returns before the
    machine goes down rather than dying mid-response and looking like a crash.
    """
    helper = str(Path(__file__).resolve().parents[2] / "scripts" / "skylapse-admin")
    result = subprocess.run(["sudo", "-n", helper, "reboot"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise HTTPException(500, (result.stderr or result.stdout).strip()[:200]
                            or "could not reboot")
    return {"ok": True, "note": "rebooting; this page will go quiet for a minute"}


@app.post("/api/dewheater/i2c")
def dewheater_enable_i2c() -> dict:
    """Switch the I2C bus on. Pi OS ships it off, and turning it on is a boot
    config edit — a terminal job on a camera that is meant never to need one."""
    helper = str(Path(__file__).resolve().parents[2] / "scripts" / "skylapse-admin")
    result = subprocess.run(["sudo", "-n", helper, "i2c-enable"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise HTTPException(500, (result.stderr or result.stdout).strip()[:200]
                            or "could not enable I2C")
    ready = Path("/dev/i2c-1").exists()
    return {"ok": True, "i2c_ready": ready, "needs_reboot": not ready,
            "note": "ready" if ready else "enabled; reboot to finish"}


class DewHeaterTest(BaseModel):
    # 60s, not the 5s this shipped with. Six resistor bodies take longer than
    # five seconds to become warm enough to feel, so the original default made
    # a correctly wired heater look dead.
    seconds: float = 60.0


@app.post("/api/dewheater/test")
def dewheater_test(body: DewHeaterTest | None = None) -> dict:
    """Switch the heater on for a few seconds, deliberately.

    Commissioning a heater is the one moment worth being careful about, and the
    alternative is enabling the whole feature and waiting for the weather to
    meet the dewpoint before finding out whether anything is wired correctly.
    Capped hard, and the pin is switched off in a finally block so a dropped
    connection cannot leave it on.
    """
    from ..daemon import dewheater

    # Optional body: a plain POST means the default pulse. It was required,
    # and the button sends none -- so the one control for commissioning a
    # heater answered 422, and the settings page went black rendering it.
    seconds = body.seconds if body else DewHeaterTest().seconds
    result = dewheater.test_pulse(config.load().dew_heater.gpio_pin, seconds)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "could not drive the pin"))
    return {**result, "note": "pin is off again"}


class DewHeaterSettings(BaseModel):
    experimental_enabled: bool | None = None
    on_margin_c: float | None = None
    off_margin_c: float | None = None


@app.put("/api/dewheater")
def dewheater_configure(body: DewHeaterSettings) -> dict:
    """Turn it on, or move the hysteresis band.

    The band is two numbers and they are not independent: heating starts when
    the glass is within on_margin of the dewpoint and stops once it clears
    off_margin, so off must exceed on or the thing chatters on and off forever.
    """
    cfg = config.load()
    dh = cfg.dew_heater
    on = body.on_margin_c if body.on_margin_c is not None else dh.on_margin_c
    off = body.off_margin_c if body.off_margin_c is not None else dh.off_margin_c
    if off <= on:
        raise HTTPException(400, "the off margin has to be above the on margin, "
                                 "or the heater will chatter")
    dh.on_margin_c, dh.off_margin_c = on, off
    if body.experimental_enabled is not None:
        dh.experimental_enabled = body.experimental_enabled
    config.save(cfg)
    return {"ok": True, "enabled": dh.experimental_enabled,
            "on_margin_c": on, "off_margin_c": off,
            "note": "the daemon picks this up on its next camera open"}


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
        "country": cfg.network.country,
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


class WifiCountry(BaseModel):
    country: str


@app.post("/api/network/country")
def network_set_country(body: WifiCountry) -> dict:
    """The Wi-Fi regulatory country.

    Its own endpoint rather than a config PUT because it is the one setting a
    camera may be unreachable without: until it is set the radio sits in the
    world domain, where no channel may transmit, and the access point that
    setup itself runs on cannot start.
    """
    country = (body.country or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise HTTPException(400, "expected a two-letter country code")
    cfg = config.load()
    cfg.network.country = country
    config.save(cfg)
    # Applied on the next hotspot start; pushing it now would mean touching the
    # radio out from under whatever it is currently doing.
    return {"ok": True, "country": country}


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
            # The daylight either side of the night is its own film, because
            # splicing it on puts a three-orders-of-magnitude exposure cut in
            # the middle of the one people actually watch.
            "has_day_timelapse": (night / f"timelapse_{night.name}_day.mp4").exists(),
            # A render in progress is a large unplayable file, so the UI needs
            # to know the difference between "not rendered" and "rendering".
            "rendering": any(night.glob("timelapse_*.mp4.part")),
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


# -- deleting -----------------------------------------------------------------
#
# Nothing could remove a frame before this. The only thing that deleted anything
# was the automatic cleanup at the low-space floor, which takes the OLDEST night
# first — the exact opposite of what a night of setup junk needs. So the answer
# to "I have seven hundred useless frames from getting the thing working" was to
# either keep them forever or take the card out.
#
# Destructive and immediate, with no undo. The UI confirms; this does not, since
# a confirmation an API asks for is one an API cannot enforce.


def _frame_family(folder: Path, name: str) -> list[Path]:
    """A frame is four files: the JPEG, its sidecar, maybe a DNG, and the
    filmstrip thumbnail. Deleting one and orphaning the rest leaves a folder
    that is not empty, will not tidy up, and counts wrong."""
    stem = Path(name).stem
    family = [folder / f"{stem}{suffix}" for suffix in (".jpg", ".json", ".dng")]
    family.append(process.thumb_path(folder / f"{stem}.jpg"))
    return [f for f in family if f.exists()]


@app.delete("/api/nights/{camera_id}/{night}/frame/{name}")
def delete_frame(camera_id: str, night: str, name: str) -> dict:
    """One frame and everything that belongs to it."""
    folder = _night_dir(camera_id, night)
    jpeg = (folder / name).resolve()
    if not jpeg.is_relative_to(folder) or not jpeg.name.startswith("img_")             or jpeg.suffix != ".jpg":
        raise HTTPException(404, "No such frame")
    removed = _frame_family(folder, jpeg.name)
    if not removed:
        raise HTTPException(404, "No such frame")
    for path in removed:
        path.unlink(missing_ok=True)
    return {"ok": True, "deleted": len(removed), "frame": jpeg.name}


@app.delete("/api/nights/{camera_id}/{night}")
def delete_night(camera_id: str, night: str, before: float | None = None) -> dict:
    """A whole night, or everything in it up to a moment.

    `before` is what makes this usable on the night you are standing in: a
    setup evening leaves hundreds of junk frames and then turns into the real
    night at some point, and the folder is still being written to. Trimming by
    time clears the mess without touching the folder the daemon is using or the
    frames that arrive after.
    """
    folder = _night_dir(camera_id, night)
    frames = sorted(folder.glob(FRAME_GLOB))
    if before is not None:
        frames = [f for f in frames if f.stat().st_mtime < before]

    deleted = files = 0
    for jpeg in frames:
        for path in _frame_family(folder, jpeg.name):
            path.unlink(missing_ok=True)
            files += 1
        deleted += 1

    # The timelapse only goes when the whole night does — it is the distilled
    # keepsake, and trimming the front of a night does not invalidate it.
    if before is None:
        for extra in folder.glob("timelapse_*.mp4"):
            extra.unlink(missing_ok=True)
            files += 1
        for leftover in folder.glob("thumb_*.jpg"):
            leftover.unlink(missing_ok=True)
            files += 1
        if not any(folder.iterdir()):
            folder.rmdir()
    return {"ok": True, "frames_deleted": deleted, "files_deleted": files,
            "night": night, "folder_removed": before is None and not folder.exists()}


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
    # Float, because focusing in daylight needs a fraction of a millisecond and
    # a whole one is already several stops over-exposed on a fast lens.
    exposure_ms: float = 500
    gain: int = 250


@app.post("/api/focus/controls")
def focus_controls(body: FocusControls) -> dict:
    """Exposure/gain for the live view. The daemon re-reads this before every
    focus frame, so a slider move applies on the next capture."""
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"exposure_ms": max(0.05, body.exposure_ms), "gain": max(0, body.gain)}
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

@app.get("/api/timelapse/plan/{camera_id}")
def timelapse_plan(camera_id: str) -> dict:
    """What a render would produce, without doing one.

    The clip length is honoured by leaving frames out, and until this existed
    that happened silently — which is precisely how the setting managed to be
    ignored on every long night without anyone noticing. The settings screen
    reads this so it can say, in numbers, what tonight will look like.
    """
    from ..daemon import nightjobs
    root = config.IMAGE_ROOT / camera_id
    nights = sorted((d for d in root.iterdir() if d.is_dir()),
                    key=lambda d: d.name) if root.is_dir() else []
    if not nights:
        raise HTTPException(404, "no nights captured yet")
    cfg = config.load()
    # Plain lookup, not cfg.camera(): that one is fetch-or-create and would
    # invent a registry entry every time the settings screen was opened.
    entry = cfg.cameras.get(camera_id)
    settings = entry.timelapse if entry else None
    return {"night": nights[-1].name, **nightjobs.plan_render(nights[-1], settings)}


@app.get("/api/timelapse/{camera_id}/{night}")
def timelapse_file(camera_id: str, night: str, variant: str = "night"):
    """Serve a night's rendered mp4 so the dashboard can play it inline.

    Declared before the render route only for readability — they never
    collide, since this is GET and the render endpoint is POST under a
    literal /render/ segment.
    """
    from ..daemon import nightjobs
    if variant not in ("night", "day"):
        raise HTTPException(400, "variant must be night or day")
    path = nightjobs.output_name(config.IMAGE_ROOT / camera_id / night, variant)
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
    rendered = nightjobs.render_all(folder, settings, force=force)
    out = rendered.get("night") or rendered.get("day")
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
    subscribe = (f"{cfg.notifications.ntfy_server.rstrip('/')}/"
                 f"{cfg.notifications.ntfy_topic}")
    # A QR beats typing a random hex topic into a phone by hand, and the wizard
    # is exactly where the phone is already out. Rendered the same way remote
    # access renders its own.
    return {"topic": cfg.notifications.ntfy_topic,
            "subscribe_url": subscribe,
            "qr_svg": remote.qr_svg(subscribe)}


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


@app.post("/api/remote/install")
def remote_install() -> dict:
    """Install Tailscale from the vendor's signed apt repository.

    Nothing installed it before — not install.sh, not the SD image — so the
    settings card correctly reported it missing on every flashed camera and then
    offered no way to do anything about it. Slow by nature: it holds the request
    for the length of an apt install.
    """
    result = remote.install()
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "could not install Tailscale"))
    return result


@app.post("/api/remote/enable")
def remote_enable() -> dict:
    result = remote.enable()
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "could not start Tailscale"))
    return result


@app.post("/api/remote/disable")
def remote_disable() -> dict:
    result = remote.disable()
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "could not turn it off"))
    return result


# -- static frontend (mounted last so /api wins) -----------------------------

class _Frontend(StaticFiles):
    """Static files, with index.html deliberately never cached.

    Vite fingerprints every asset — index-a1b2c3.js — so those may be cached
    forever, and should be. index.html is the opposite: it is the one file that
    says which fingerprints are current, and a stale copy points at a bundle
    that no longer exists on disk.

    That is not theoretical. After an update the browser kept an index.html
    naming the previous bundle, the camera returned 404 for it, and the settings
    page went black — with the fix already installed and serving correctly. The
    only way in was a hard reload, which is not something to ask of somebody
    holding a phone in a field.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if str(full_path).endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


_web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if _web_dist.exists():
    app.mount("/", _Frontend(directory=_web_dist, html=True), name="web")
