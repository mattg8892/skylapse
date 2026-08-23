"""Deleting frames, which nothing could do until a night of setup junk needed it.

The only thing that removed anything was the low-space cleanup, and that takes
the OLDEST night first — the exact opposite of what "I have seven hundred
useless frames from getting the thing working" requires. The answer was to keep
them forever or take the card out.

These are destructive and immediate, so what is tested is mostly what must NOT
happen: escaping the night folder, orphaning half a frame, or taking frames that
arrived after the moment being trimmed to.
"""
from __future__ import annotations

import time

import numpy as np
import cv2
import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api


@pytest.fixture
def night(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path / "images")
    monkeypatch.setattr(api.config, "IMAGE_ROOT", tmp_path / "images")
    config.save(config.Config())
    folder = tmp_path / "images" / "picam-imx477" / "2026-08-19"
    folder.mkdir(parents=True)
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    for i in range(6):
        stem = f"img_2026081{9}_00000{i}"
        cv2.imwrite(str(folder / f"{stem}.jpg"), frame)
        (folder / f"{stem}.json").write_text('{"timestamp": 1}')
        (folder / f"thumb_{stem}.jpg").write_bytes(b"x")
    (folder / "timelapse_2026-08-19.mp4").write_bytes(b"mp4")
    return folder


@pytest.fixture
def client(night):
    return TestClient(api.app)


def test_deleting_a_frame_takes_its_whole_family(client, night):
    """A frame is a JPEG, a sidecar, sometimes a DNG and a thumbnail. Removing
    one and orphaning the rest leaves a folder that never empties and counts
    wrong."""
    (night / "img_20260819_000000.dng").write_bytes(b"dng")
    r = client.delete("/api/nights/picam-imx477/2026-08-19/frame/img_20260819_000000.jpg")
    assert r.status_code == 200 and r.json()["deleted"] == 4
    for suffix in (".jpg", ".json", ".dng"):
        assert not (night / f"img_20260819_000000{suffix}").exists()
    assert not (night / "thumb_img_20260819_000000.jpg").exists()
    assert (night / "img_20260819_000001.jpg").exists(), "took a neighbour with it"


def test_a_frame_name_cannot_escape_the_night(client, night):
    outside = night.parent.parent / "secret.jpg"
    outside.write_bytes(b"x")
    r = client.delete("/api/nights/picam-imx477/2026-08-19/frame/..%2F..%2Fsecret.jpg")
    assert r.status_code in (400, 404, 405)
    assert outside.exists(), "deleted a file outside the image store"


def test_only_captured_frames_can_be_deleted(client, night):
    """The mp4 is the distilled keepsake of the night and is not a frame."""
    r = client.delete(
        "/api/nights/picam-imx477/2026-08-19/frame/timelapse_2026-08-19.mp4")
    assert r.status_code == 404
    assert (night / "timelapse_2026-08-19.mp4").exists()


def test_deleting_a_whole_night_removes_the_folder(client, night):
    r = client.delete("/api/nights/picam-imx477/2026-08-19")
    body = r.json()
    assert body["frames_deleted"] == 6
    assert body["folder_removed"] is True
    assert not night.exists()


def test_trimming_to_a_moment_keeps_what_came_after(client, night):
    """The case this exists for: a setup evening that turns into a real night,
    in a folder the daemon is still writing to. Everything before the moment
    goes; the folder and everything after it stay."""
    import os
    frames = sorted(night.glob("img_*.jpg"))
    cutoff = time.time()
    for f in frames[:3]:                       # setup junk, before the cutoff
        os.utime(f, (cutoff - 600, cutoff - 600))
    for f in frames[3:]:                       # the real night, after it
        os.utime(f, (cutoff + 600, cutoff + 600))

    r = client.delete(f"/api/nights/picam-imx477/2026-08-19?before={cutoff}")
    body = r.json()
    assert body["frames_deleted"] == 3
    assert body["folder_removed"] is False
    assert night.exists(), "removed the folder the daemon is writing into"
    assert len(list(night.glob("img_*.jpg"))) == 3


def test_trimming_leaves_the_timelapse_alone(client, night):
    """Trimming the front of a night does not invalidate a render of it."""
    client.delete(f"/api/nights/picam-imx477/2026-08-19?before={time.time() + 60}")
    assert (night / "timelapse_2026-08-19.mp4").exists()


def test_deleting_a_night_that_is_not_there(client):
    assert client.delete("/api/nights/picam-imx477/1999-01-01").status_code == 404
