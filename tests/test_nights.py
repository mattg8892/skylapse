"""Nights browser API: index, pagination, thumbnails, and path safety.

camera_id and night come straight from the URL, so the traversal cases here
are the point of the file as much as the happy paths.
"""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api

CAMERA = "zwo-test"
NIGHT = "2026-08-13"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A camera/night folder laid out the way the daemon writes one."""
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    night = tmp_path / CAMERA / NIGHT
    night.mkdir(parents=True)

    img = np.full((40, 60, 3), 90, dtype=np.uint8)
    for i in range(5):
        stem = f"img_2026081{i}_120000"
        cv2.imwrite(str(night / f"{stem}.jpg"), img)
        (night / f"{stem}.json").write_text(json.dumps({
            "timestamp": 1_786_000_000.0 + i, "exposure_us": 500_000,
            "gain": 100, "stars": 100 + i,
        }))
    # Only the first frame has a raw beside it.
    (night / "img_20260810_120000.dng").write_bytes(b"II*\x00" + b"\0" * 64)
    return night


@pytest.fixture()
def client():
    return TestClient(api.app)


def test_nights_index(store, client):
    body = client.get(f"/api/nights/{CAMERA}").json()
    assert len(body) == 1
    night = body[0]
    assert night["night"] == NIGHT
    assert night["frames"] == 5
    assert night["has_timelapse"] is False
    assert night["bytes"] > 0


def test_nights_index_reports_timelapse(store, client):
    (store / f"timelapse_{NIGHT}.mp4").write_bytes(b"\0" * 10)
    assert client.get(f"/api/nights/{CAMERA}").json()[0]["has_timelapse"] is True


def test_unknown_camera_is_404(store, client):
    assert client.get("/api/nights/nope").status_code == 404


def test_frame_index_carries_sidecar_data(store, client):
    body = client.get(f"/api/nights/{CAMERA}/{NIGHT}/frames").json()
    assert body["total"] == 5
    first = body["frames"][0]
    assert first["stars"] == 100
    assert first["exposure_us"] == 500_000
    assert first["has_dng"] is True
    assert body["frames"][1]["has_dng"] is False


def test_frame_index_pagination(store, client):
    body = client.get(f"/api/nights/{CAMERA}/{NIGHT}/frames?offset=2&limit=2").json()
    assert body["total"] == 5, "total must describe the night, not the window"
    assert len(body["frames"]) == 2
    assert body["offset"] == 2


def test_thumbnails_are_excluded_from_the_index(store, client):
    """Thumbs live beside the frames; counting them would double the night."""
    client.get(f"/api/nights/{CAMERA}/{NIGHT}/frame/img_20260810_120000.jpg?thumb=true")
    assert list(store.glob("thumb_*.jpg")), "thumbnail was not generated"
    assert client.get(f"/api/nights/{CAMERA}/{NIGHT}/frames").json()["total"] == 5
    assert client.get(f"/api/nights/{CAMERA}").json()[0]["frames"] == 5


def test_thumbnail_is_smaller_than_the_frame(store, client):
    r = client.get(
        f"/api/nights/{CAMERA}/{NIGHT}/frame/img_20260810_120000.jpg?thumb=true")
    assert r.status_code == 200
    thumb = next(store.glob("thumb_*.jpg"))
    got = cv2.imread(str(thumb))
    assert max(got.shape[:2]) <= api.THUMB_PX


def test_raw_download_is_an_attachment(store, client):
    r = client.get(f"/api/nights/{CAMERA}/{NIGHT}/raw/img_20260810_120000.dng")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]


def test_missing_frame_is_404(store, client):
    assert client.get(
        f"/api/nights/{CAMERA}/{NIGHT}/frame/img_nope.jpg").status_code == 404


@pytest.mark.parametrize("name", ["../../etc/passwd", "..%2f..%2fetc%2fpasswd"])
def test_frame_name_cannot_escape_the_night(store, client, name):
    r = client.get(f"/api/nights/{CAMERA}/{NIGHT}/frame/{name}")
    assert r.status_code in (400, 404), "path traversal must not be served"


def test_night_cannot_escape_the_image_root(store, client):
    r = client.get(f"/api/nights/{CAMERA}/../../frames")
    assert r.status_code in (400, 404)


def test_non_frame_files_are_not_served(store, client):
    """Only img_*.jpg — the sidecars and mp4 have their own routes or none."""
    r = client.get(f"/api/nights/{CAMERA}/{NIGHT}/frame/img_20260810_120000.json")
    assert r.status_code == 404


# -- on-demand renders are background work ------------------------------------

def test_an_on_demand_render_returns_before_the_encode(tmp_path, monkeypatch):
    """A render held uvicorn's one worker synchronously on 2026-09-17: every
    request from every client timed out until ffmpeg finished, minutes later.
    The endpoint's job is to START the render; the nights index reports the
    rest (`rendering` while the .part exists, the flags when it lands)."""
    import threading
    import time as _time
    from fastapi.testclient import TestClient
    from skylapse import config
    from skylapse.api import main as api
    from skylapse.daemon import nightjobs

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path / "img")
    config.save(config.Config())
    night = tmp_path / "img" / "picam-imx477" / "2026-09-15"
    night.mkdir(parents=True)

    release = threading.Event()
    started = threading.Event()

    def slow_render(folder, settings, force=False):
        started.set()
        release.wait(timeout=5)
        return {}

    monkeypatch.setattr(nightjobs, "render_all", slow_render)
    client = TestClient(api.app)
    t0 = _time.monotonic()
    r = client.post("/api/timelapse/render/picam-imx477/2026-09-15")
    took = _time.monotonic() - t0
    try:
        assert r.status_code == 200 and r.json()["started"] is True
        assert took < 2, f"the endpoint blocked for {took:.1f}s on the encode"
        assert started.wait(timeout=5), "the render never actually started"
        # And a second render while one runs is refused, not queued: two
        # 2-thread encodes plus capture is undervoltage territory.
        r2 = client.post("/api/timelapse/render/picam-imx477/2026-09-15")
        assert r2.status_code == 409
    finally:
        release.set()
        _time.sleep(0.1)          # let the worker release the lock
