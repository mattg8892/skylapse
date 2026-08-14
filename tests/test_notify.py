"""Notification gating and wire encoding.

The encoding tests come from a real failure against ntfy.sh: a title with an
em dash raised inside urllib and the notification was silently dropped. For a
camera-offline alert that is the worst possible one to lose, and nothing in
the system would have told you it went missing.
"""
from __future__ import annotations

import pytest

from skylapse import config, notify


@pytest.fixture()
def sent(monkeypatch):
    """Capture what would go on the wire instead of posting it."""
    calls = []

    class FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        calls.append({"url": req.full_url, "headers": dict(req.headers),
                      "body": req.data})
        return FakeResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return calls


def _cfg(**kw):
    cfg = config.Config()
    n = cfg.notifications
    n.enabled = kw.get("enabled", True)
    n.ntfy_topic = kw.get("topic", "skylapse-test")
    if "events" in kw:
        n.events = kw["events"]
    return cfg


# -- gating -----------------------------------------------------------------

def test_master_switch_off_sends_nothing(sent):
    assert notify.notify("aurora", "t", "b", _cfg(enabled=False)) is False
    assert sent == []


def test_event_toggle_off_sends_nothing(sent):
    cfg = _cfg(events={"aurora": False})
    assert notify.notify("aurora", "t", "b", cfg) is False
    assert sent == []


def test_no_topic_sends_nothing(sent):
    assert notify.notify("aurora", "t", "b", _cfg(topic="")) is False
    assert sent == []


def test_enabled_event_sends(sent):
    cfg = _cfg(events={"camera_offline": True})
    assert notify.notify("camera_offline", "Camera offline", "gone", cfg) is True
    assert len(sent) == 1
    assert sent[0]["url"].endswith("/skylapse-test")


def test_test_event_bypasses_the_per_event_toggle(sent):
    """The Send test button must work before any event is switched on."""
    cfg = _cfg(events={})
    assert notify.notify("test", "Skylapse", "hello", cfg) is True
    assert len(sent) == 1


# -- wire encoding ----------------------------------------------------------

def test_ascii_title_is_sent_as_is(sent):
    notify.notify("test", "Camera offline", "body", _cfg())
    assert sent[0]["headers"]["Title"] == "Camera offline"


@pytest.mark.parametrize("title", [
    "Skylapse — capture stalled",     # em dash
    "Sensor at 12.5°C",               # degree sign
    "Aurora tonight ✨",               # emoji
])
def test_non_ascii_titles_do_not_raise_and_go_out_ascii_safe(title, sent):
    """Regression: urllib encodes headers as latin-1, so these used to raise
    inside the send and the notification was dropped."""
    assert notify.notify("test", title, "body", _cfg()) is True
    header = sent[0]["headers"]["Title"]
    header.encode("ascii")            # must not raise
    assert header.startswith("=?utf-8?")


def test_encoded_title_round_trips():
    from email.header import decode_header, make_header
    original = "Skylapse — 12.5°C ✨"
    decoded = str(make_header(decode_header(notify._encode_title(original))))
    assert decoded == original


def test_body_is_utf8(sent):
    notify.notify("test", "t", "temperature —12.5°C", _cfg())
    assert sent[0]["body"] == "temperature —12.5°C".encode("utf-8")


def test_delivery_failure_is_reported_not_raised(monkeypatch):
    def boom(req, timeout=10):
        raise OSError("network down")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.notify("test", "t", "b", _cfg()) is False
