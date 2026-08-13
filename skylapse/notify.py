"""Notifications. One abstraction, two channels (ntfy now, web push later).

Master switch + per-event toggles live in config; everything defaults OFF.
ntfy is a plain HTTP POST — no dependency, no account, self-hostable server.
"""
from __future__ import annotations

import logging
import urllib.request

from . import config

log = logging.getLogger("skylapse.notify")

EVENTS = ("aurora", "storage_low", "camera_offline", "timelapse_ready",
          "safety_stop", "test")


def notify(event: str, title: str, body: str, cfg: config.Config | None = None) -> bool:
    """Send if-and-only-if: master switch on, event toggle on, topic configured.
    Returns True only when a delivery attempt was actually made and accepted.
    """
    cfg = cfg or config.load()
    n = cfg.notifications
    if not n.enabled:                                  # master switch wins
        return False
    if event != "test" and not n.events.get(event, False):
        return False
    if not n.ntfy_topic:
        return False
    return _post_ntfy(n.ntfy_server, n.ntfy_topic, title, body)


def _post_ntfy(server: str, topic: str, title: str, body: str) -> bool:
    url = f"{server.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url, data=body.encode(), method="POST",
        headers={"Title": title, "Tags": "milky_way"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.warning("ntfy delivery failed: %s", exc)
        return False
