"""Aurora alerts from NOAA SWPC (free, keyless US government data).

Threshold auto-derives from the camera's configured latitude — the same rig
moved from Racine (42.7N, needs ~Kp7) to Amberg (45.5N, ~Kp5) recalibrates
itself. Fires at most once per Kp episode, and only when it's dark on-site.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .. import config, notify

log = logging.getLogger("skylapse.aurora")

KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
POLL_MINUTES = 30

# (min_abs_latitude, kp_needed) — coarse geomagnetic visibility bands.
_KP_BANDS = [(60, 3), (55, 4), (50, 5), (45, 6), (40, 7), (0, 8)]


def kp_threshold(latitude: float) -> int:
    lat = abs(latitude)
    for band_lat, kp in _KP_BANDS:
        if lat >= band_lat:
            return kp
    return 8


def fetch_current_kp() -> float | None:
    """Latest observed Kp. Table format: header row, then [time, kp, ...]."""
    try:
        with urllib.request.urlopen(KP_URL, timeout=15) as resp:
            rows = json.loads(resp.read())
        return float(rows[-1][1])
    except Exception as exc:
        log.debug("SWPC fetch failed: %s", exc)
        return None


def check(cfg: config.Config, current_period: str,
          already_alerted: bool) -> tuple[bool, float | None]:
    """Returns (alerted_now, kp). Alerts once per episode: caller keeps the
    already_alerted flag latched until Kp falls back below threshold."""
    kp = fetch_current_kp()
    if kp is None:
        return already_alerted, None
    threshold = kp_threshold(cfg.location.latitude)
    if kp < threshold:
        return False, kp                     # episode over; re-arm
    if already_alerted or current_period == "day":
        return already_alerted, kp
    notify.notify("aurora", "Aurora possible tonight",
                  f"Kp is {kp:.0f} (your threshold: {threshold}). "
                  "Skylapse is capturing.", cfg)
    return True, kp
