"""Skylapse API. Serves REST + the built React frontend.

Runs as its own service; talks to the daemon and netwatch only through
config.yaml and /run/skylapse status files — no direct coupling.
"""
from __future__ import annotations

import json
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

@app.get("/api/status")
def status() -> dict:
    return {
        "daemon": _read_status("daemon"),
        "network": _read_status("netwatch"),
        "server_time": time.time(),
        "setup_complete": config.load().setup_complete,
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
