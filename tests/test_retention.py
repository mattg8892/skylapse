"""Storage retention and the every-frame RAW projection.

The projection drives a warning about hardware wear, so a wrong number is
worse than no number. It must reflect the rig's real frame count and real DNG
size, not a generic figure — sensor size and cadence vary by an order of
magnitude across supported cameras.
"""
from __future__ import annotations

import pytest

from skylapse import config
from skylapse.api import main as api

CAMERA = "cam"


@pytest.fixture(autouse=True)
def clear_cache():
    api._retention_cache.update(at=0.0, value=None)
    yield
    api._retention_cache.update(at=0.0, value=None)


def _night(root, name, frames=0, jpeg=1_000_000, dngs=0, dng=25_000_000):
    night = root / CAMERA / name
    night.mkdir(parents=True, exist_ok=True)
    for i in range(frames):
        (night / f"img_{name}_{i:04d}.jpg").write_bytes(b"j" * jpeg)
        (night / f"img_{name}_{i:04d}.json").write_bytes(b"{}")
    for i in range(dngs):
        (night / f"img_{name}_{i:04d}.dng").write_bytes(b"d" * dng)
    return night


def test_projection_uses_real_frame_count_and_real_dng_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    # Two nights so the *complete* one (the earlier) is measured.
    _night(tmp_path, "2026-08-10", frames=100, jpeg=1_000_000, dngs=2, dng=25_000_000)
    _night(tmp_path, "2026-08-11", frames=5)

    r = api._retention(config.Config())
    assert r["basis"] == "complete"
    assert r["frames_per_night"] == 100
    assert r["raw_measured"] is True
    assert r["raw_bytes_per_frame"] == 25_000_000
    # non-RAW footprint (100 jpegs + sidecars) + 100 DNGs
    assert r["every_frame_raw_gb"] == pytest.approx(2.6, abs=0.05)


def test_projection_far_exceeds_current_cost_when_raw_is_off(tmp_path, monkeypatch):
    """The whole point: the warning must not quote today's JPEG-only rate."""
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    _night(tmp_path, "2026-08-10", frames=200, jpeg=1_000_000, dngs=1)
    _night(tmp_path, "2026-08-11", frames=1)

    r = api._retention(config.Config())
    assert r["per_night_gb"] < 0.3          # what it costs today
    assert r["every_frame_raw_gb"] > 4.0    # what every-frame RAW would cost


def test_falls_back_when_no_dng_has_ever_been_written(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    _night(tmp_path, "2026-08-10", frames=10)
    _night(tmp_path, "2026-08-11", frames=1)

    r = api._retention(config.Config())
    assert r["raw_measured"] is False, "claimed a measurement it never made"
    assert r["raw_bytes_per_frame"] == api.RAW_BYTES_FALLBACK
    # 10 frames x 25 MB + ~10 MB of JPEGs = 0.26 GB, reported to one decimal.
    assert r["every_frame_raw_gb"] == 0.3


def test_in_progress_night_is_labelled(tmp_path, monkeypatch):
    """A partial night understates the projection; the UI says so."""
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    _night(tmp_path, "2026-08-11", frames=10)
    assert api._retention(config.Config())["basis"] == "in_progress"


def test_empty_store_projects_nothing(tmp_path, monkeypatch):
    """No data must produce no number rather than a fabricated one."""
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    r = api._retention(config.Config())
    assert r["every_frame_raw_gb"] is None
    assert r["frames_per_night"] is None


def test_thumbnails_do_not_inflate_the_projection(tmp_path, monkeypatch):
    """thumb_*.jpg are real disk cost but are not frames — counting them as
    frames would overstate the RAW projection by ~2x."""
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    night = _night(tmp_path, "2026-08-10", frames=50, dngs=1)
    for i in range(50):
        (night / f"thumb_img_2026-08-10_{i:04d}.jpg").write_bytes(b"t" * 5000)
    _night(tmp_path, "2026-08-11", frames=1)

    assert api._retention(config.Config())["frames_per_night"] == 50


def test_retention_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    _night(tmp_path, "2026-08-10", frames=5)
    _night(tmp_path, "2026-08-11", frames=1)
    first = api._retention(config.Config())
    _night(tmp_path, "2026-08-10", frames=200)          # store grows
    assert api._retention(config.Config()) == first, "recomputed inside the TTL"
