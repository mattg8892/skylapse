"""The /api/status capture block that drives the dashboard's liveness pill.

The pill has to agree with the watchdog. If the dashboard says "Capturing"
while a notification says the camera is stalled, the notification is the one
that stops being believed — so the threshold is computed here, once, from the
same function the daemon uses.
"""
from __future__ import annotations

import pytest

from skylapse import config
from skylapse.api import main as api
from skylapse.daemon.watchdog import stall_threshold_s

CAMERA = "cam"
NIGHT = "2026-08-15"


@pytest.fixture(autouse=True)
def clear_caches():
    api._frames_cache.update(at=0.0, night="", count=0)
    api._retention_cache.update(at=0.0, value=None)
    yield


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    night = tmp_path / CAMERA / NIGHT
    night.mkdir(parents=True)
    return night


def _cfg(gap_s=5):
    cfg = config.Config()
    cam = cfg.camera(CAMERA)
    cam.night.gap_s = gap_s
    cam.day.gap_s = gap_s
    return cfg


def test_capture_block_reports_the_active_gap_and_exposure(store):
    cfg = _cfg(gap_s=7)
    daemon = {"state": "capturing", "exposure_us": 2_000_000, "frame_at": 1000.0}
    current = {"camera_id": CAMERA, "night": NIGHT}
    block = api._capture(cfg, daemon, current)
    assert block["gap_s"] == 7
    assert block["exposure_us"] == 2_000_000
    assert block["frame_at"] == 1000.0


def test_threshold_matches_the_watchdog_exactly(store):
    """Same function, so the pill and the notification cannot disagree."""
    cfg = _cfg(gap_s=5)
    daemon = {"state": "capturing", "exposure_us": 20_000_000, "frame_at": 1.0}
    block = api._capture(cfg, daemon, {"camera_id": CAMERA, "night": NIGHT})
    assert block["stall_threshold_s"] == stall_threshold_s(5, 20_000_000)


def test_frames_tonight_counts_only_frames(store):
    for i in range(4):
        (store / f"img_x{i}.jpg").write_bytes(b"j")
        (store / f"img_x{i}.json").write_bytes(b"{}")
        (store / f"thumb_img_x{i}.jpg").write_bytes(b"t")
    (store / f"timelapse_{NIGHT}.mp4").write_bytes(b"m")
    block = api._capture(_cfg(), {"state": "capturing"},
                         {"camera_id": CAMERA, "night": NIGHT})
    assert block["frames_tonight"] == 4


def test_frames_tonight_is_zero_for_an_unknown_night(store):
    block = api._capture(_cfg(), {}, {"camera_id": CAMERA, "night": "1999-01-01"})
    assert block["frames_tonight"] == 0


def test_frames_tonight_is_cached_per_night(store):
    (store / "img_a.jpg").write_bytes(b"j")
    first = api._capture(_cfg(), {}, {"camera_id": CAMERA, "night": NIGHT})
    (store / "img_b.jpg").write_bytes(b"j")
    second = api._capture(_cfg(), {}, {"camera_id": CAMERA, "night": NIGHT})
    assert second["frames_tonight"] == first["frames_tonight"], "recounted inside TTL"


def test_cache_does_not_leak_across_nights(store):
    """A night rollover must not report yesterday's count."""
    (store / "img_a.jpg").write_bytes(b"j")
    api._capture(_cfg(), {}, {"camera_id": CAMERA, "night": NIGHT})
    other = store.parent / "2026-08-16"
    other.mkdir()
    block = api._capture(_cfg(), {}, {"camera_id": CAMERA, "night": "2026-08-16"})
    assert block["frames_tonight"] == 0


def test_unknown_camera_yields_no_threshold(store):
    """Better to show nothing than a confidently wrong liveness verdict."""
    block = api._capture(config.Config(), {"state": "capturing"},
                         {"camera_id": "nope", "night": NIGHT})
    assert block["gap_s"] is None
    assert block["stall_threshold_s"] is None
