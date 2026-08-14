"""Notifications. One abstraction, two channels (ntfy now, web push later).

Master switch + per-event toggles live in config; everything defaults OFF.
ntfy is a plain HTTP POST — no dependency, no account, self-hostable server.
"""
from __future__ import annotations

import logging
import urllib.request
from email.header import Header

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


def _encode_title(title: str) -> str:
    """ASCII-safe Title header.

    HTTP headers are latin-1 in urllib, so a title containing an em dash, a
    degree sign, or any accented character raises at send time — the
    notification is simply lost, which for a camera-offline alert is the worst
    possible thing to lose. ntfy decodes RFC 2047 encoded-words, so non-ASCII
    titles go over the wire base64-encoded and arrive intact. Verified against
    ntfy.sh: the title round-trips exactly.

    Plain ASCII titles are left alone so the common case stays readable on the
    wire and in any proxy log.
    """
    try:
        title.encode("ascii")
        return title
    except UnicodeEncodeError:
        return Header(title, "utf-8").encode()


def _post_ntfy(server: str, topic: str, title: str, body: str) -> bool:
    url = f"{server.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Title": _encode_title(title), "Tags": "milky_way"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.warning("ntfy delivery failed: %s", exc)
        return False
