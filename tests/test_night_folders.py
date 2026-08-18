"""Which night a frame belongs to, and which folder dawn renders.

Both were wrong on the night of 2026-08-17, and they were wrong together.

Raspberry Pi OS Lite ships set to Europe/London and nothing in the SD image
changes it, so a camera in Wisconsin was keeping London's clock. The folder
rolls at local noon precisely so a night is never split — but "local" was
London, and noon in London is 6 AM in Racine. The night was cut in two at
05:59: 2205 frames in 2026-08-17 and the dawn in 2026-08-18.

Then dawn fired. It rendered `max()` of the folder names — the newest, which
was the folder created minutes earlier — so the timelapse was built from the 25
frames that had arrived since the split, validated correctly against those 25,
and reported success. The night's 2205 frames were never rendered at all.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from skylapse import config
from skylapse.daemon.pipeline import process


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A config with a known location, and an image root under tmp."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(process, "IMAGE_ROOT", tmp_path / "images")
    cfg = config.Config()
    cfg.location.timezone = "America/Chicago"
    config.save(cfg)
    return tmp_path


def at(hour, minute=0, day=18, zone="America/Chicago"):
    return datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(zone)).timestamp()


def test_a_6am_frame_files_under_the_night_that_is_ending(store):
    """The reported bug, pinned. 6 AM is the tail of the previous night, not
    the start of a new one, and a night that gets split has no timelapse."""
    assert process.day_folder(at(6, 0), "cam").name == "2026-08-17"


@pytest.mark.parametrize("hour,expected", [
    (18, "2026-08-18"),      # dusk: the night that is starting
    (23, "2026-08-18"),
    (0, "2026-08-17"),       # past midnight: still last night
    (5, "2026-08-17"),
    (6, "2026-08-17"),       # where it broke
    (11, "2026-08-17"),      # right up to noon
    (12, "2026-08-18"),      # and over at noon, not before
])
def test_the_night_rolls_at_local_noon(store, hour, expected):
    assert process.day_folder(at(hour), "cam").name == expected


def test_the_camera_zone_decides_not_the_host(store):
    """The whole fault in one assertion. The host was on Europe/London; what
    matters is where the camera is, which setup already asked for and stored.

    Auckland, because it is a date away from every plausible host zone — so a
    pass here cannot be the host's clock agreeing by luck.
    """
    cfg = config.load()
    cfg.location.timezone = "Pacific/Auckland"
    config.save(cfg)
    # 06:00 in Auckland on the 18th, expressed as an instant.
    ts = at(6, 0, zone="Pacific/Auckland")
    assert process.day_folder(ts, "cam").name == "2026-08-17"


def test_an_unset_timezone_still_works(store):
    """A camera that has never finished setup must still file its frames."""
    cfg = config.load()
    cfg.location.timezone = ""
    config.save(cfg)
    assert process.day_folder(at(23), "cam").parent.name == "cam"


def test_dawn_renders_the_night_that_just_ended(store):
    """What the dawn job now asks for: the folder frames are being written to
    right now. At dawn the rollover is still hours away, so that is last
    night's folder — by construction, not by luck.

    The old code took max() of the directory names. Both folders exist here,
    exactly as they did on the rig, and the newest is the wrong answer.
    """
    root = process.IMAGE_ROOT / "cam"
    for night in ("2026-08-17", "2026-08-18"):
        (root / night).mkdir(parents=True, exist_ok=True)

    dawn = at(6, 10)                       # period flips night -> day about here
    assert process.day_folder(dawn, "cam").name == "2026-08-17"
    assert max(d for d in root.iterdir() if d.is_dir()).name == "2026-08-18", \
        "precondition: the newest folder is the wrong one to render"


def test_the_frame_name_agrees_with_the_folder(store):
    """They are the same clock now. A frame called 21:49 sitting in a folder
    chosen by a different timezone is how nobody noticed for a whole night."""
    ts = at(6, 30)
    assert process.day_folder(ts, "cam").name == "2026-08-17"
    assert process.local_time(ts).strftime("%Y%m%d_%H%M%S") == "20260818_063000"
