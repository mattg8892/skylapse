"""First-run wizard state.

The wizard collects its answers into a draft on the camera rather than in the
phone, and commits the lot at the end. Two reasons, both about the connection
being unreliable by nature: the phone sleeps, and the future entry path is a
hotspot the camera itself is about to reconfigure. Losing four screens of
answers because a screen locked would be a poor first impression of a device
whose whole promise is that it looks after itself.

Guard 4 in DESIGN.md governs the commit: the config and the setup_complete
flag are written atomically, and the flag is persisted *before* the final
screen renders, so a power cut at exactly the wrong moment leaves a configured
camera rather than one that starts the wizard again on every boot. Re-entry is
idempotent — it shows the current values and never blanks them.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

DRAFT_NAME = "setup_draft.json"

# Screens, in order. The wizard is one question per screen and the server keeps
# the list so that "which step am I on" survives a reload.
STEPS = ("welcome", "network", "camera", "location", "capture",
         "security", "notifications", "done")


def draft_path() -> Path:
    """Beside the config, not in /run.

    /run is a tmpfs, and a wizard interrupted by a power cut is exactly the
    case worth surviving — that is a half-configured camera someone is standing
    next to.
    """
    return config.CONFIG_PATH.parent / DRAFT_NAME


def load_draft() -> dict:
    """The draft, seeded from current config on first read.

    Seeding is what makes re-entry idempotent: walking the wizard again on a
    configured camera shows what it is set to now, and changes nothing that is
    not deliberately changed.
    """
    path = draft_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass          # a corrupt draft is not worth a failed setup
    return seed_from_config(config.load())


def seed_from_config(cfg: config.Config) -> dict:
    return {
        "step": "welcome",
        "started": time.time(),
        "location": {"latitude": cfg.location.latitude,
                     "longitude": cfg.location.longitude,
                     "timezone": cfg.location.timezone,
                     "source": ""},
        "camera": {"camera_id": cfg.active_camera},
        "capture": {"schedule": "always", "raw_mode": "off"},
        "security": {"password": "", "public_live_view": False},
        "notifications": {"enabled": cfg.notifications.enabled},
    }


def save_draft(draft: dict) -> dict:
    path = draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(draft, indent=2))
    tmp.replace(path)
    return draft


def clear_draft() -> None:
    draft_path().unlink(missing_ok=True)


def merge(draft: dict, patch: dict) -> dict:
    """One screen's answers into the draft.

    Section-wise rather than wholesale, so a screen only ever writes its own
    piece — going Back and forward again must not blank the screens after it.
    """
    merged = dict(draft)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def apply_draft(draft: dict, cfg: config.Config) -> config.Config:
    """Fold a finished draft into a config. Pure: returns a new config.

    Only what the wizard actually asked about is touched. Everything else on an
    already-configured camera — profiles, white balance, notification topic —
    survives someone walking the wizard again.
    """
    updated = cfg.model_copy(deep=True)

    location = draft.get("location") or {}
    if location.get("latitude") is not None:
        updated.location.latitude = float(location.get("latitude") or 0.0)
        updated.location.longitude = float(location.get("longitude") or 0.0)
    if location.get("timezone"):
        updated.location.timezone = location["timezone"]

    camera_id = (draft.get("camera") or {}).get("camera_id") or ""
    if camera_id:
        updated.active_camera = camera_id

    capture = draft.get("capture") or {}
    if camera_id and capture:
        entry = updated.camera(camera_id)
        if capture.get("schedule") in ("always", "night_only"):
            entry.capture_schedule = capture["schedule"]
        if capture.get("raw_mode") in ("off", "keepers", "every_frame"):
            # "keepers" is the Save-RAW button only, which is raw.mode "off"
            # with the rolling buffer still armed — the button works either
            # way, so the wizard's middle option is the default policy.
            entry.raw.mode = ("every_frame"
                              if capture["raw_mode"] == "every_frame" else "off")

    notifications = draft.get("notifications") or {}
    if "enabled" in notifications:
        updated.notifications.enabled = bool(notifications["enabled"])

    return updated
