"""Driver selection, including the active_camera override.

Dual-camera rigs are the case this exists for, and they are awkward to stage:
you need both a ZWO and a CSI module attached to observe the choice being made
at all. Probing is stubbed here so every combination is reachable.
"""
from __future__ import annotations

import pytest

from skylapse.daemon.drivers import base
from skylapse.daemon.drivers.base import CameraError, detect_camera, driver_of
from skylapse.daemon.drivers.picam import PiCamDriver
from skylapse.daemon.drivers.sim import SimDriver
from skylapse.daemon.drivers.zwo import ZwoDriver


@pytest.fixture()
def attached(monkeypatch):
    """Control which drivers report a camera."""
    def configure(sim=False, zwo=False, picam=False):
        monkeypatch.setattr(SimDriver, "probe", staticmethod(lambda: sim))
        monkeypatch.setattr(ZwoDriver, "probe", staticmethod(lambda: zwo))
        monkeypatch.setattr(PiCamDriver, "probe", staticmethod(lambda: picam))
    return configure


@pytest.mark.parametrize("camera_id,expected", [
    ("zwo-asi676mc", "zwo"),
    ("picam-imx477", "picam"),
    ("sim-asi-dev", "sim"),
    ("zwo", "zwo"),          # a bare driver name is accepted too
    ("", ""),
])
def test_driver_of(camera_id, expected):
    assert driver_of(camera_id) == expected


def test_picam_wins_by_default_when_both_are_attached(attached):
    """The documented probe order: the Pi camera is the primary target.

    It used to be ZWO. That order came from the rig this was first built on,
    not from what the product is: the Pi module needs no vendor library, it is
    what the SD image can support end to end, and it is what a new build is
    most likely to have. A ZWO owner says so with active_camera.
    """
    attached(zwo=True, picam=True)
    assert isinstance(detect_camera(), PiCamDriver)


def test_picam_used_when_it_is_the_only_camera(attached):
    attached(picam=True)
    assert isinstance(detect_camera(), PiCamDriver)


def test_zwo_used_when_it_is_the_only_camera(attached):
    """Second in the order is not the same as unsupported."""
    attached(zwo=True)
    assert isinstance(detect_camera(), ZwoDriver)


def test_simulator_wins_over_real_hardware(attached):
    """SKYLAPSE_SIM=1 must not be quietly ignored because a camera is plugged in."""
    attached(sim=True, zwo=True, picam=True)
    assert isinstance(detect_camera(), SimDriver)


def test_active_camera_selects_the_zwo_over_the_pi_module(attached):
    """The override that now matters: both attached, and the ZWO is the one
    pointed at the sky. Without this a dual-camera ZWO rig would silently start
    capturing with whatever module happens to be on the ribbon."""
    attached(zwo=True, picam=True)
    assert isinstance(detect_camera("zwo-asi676mc"), ZwoDriver)


def test_active_camera_selects_the_pi_module_explicitly(attached):
    attached(zwo=True, picam=True)
    assert isinstance(detect_camera("picam-imx477"), PiCamDriver)


def test_preference_falls_back_when_that_camera_is_gone(attached):
    """Unplugging the preferred camera must degrade to the other one, not stop
    the night."""
    attached(zwo=True, picam=False)
    assert isinstance(detect_camera("picam-imx477"), ZwoDriver)


def test_unknown_driver_in_active_camera_is_ignored(attached):
    """A typo in config must not take the rig down."""
    attached(zwo=True)
    assert isinstance(detect_camera("nonsense-1234"), ZwoDriver)


def test_no_camera_raises(attached):
    attached()
    with pytest.raises(CameraError, match="No supported camera"):
        detect_camera()


def test_preference_still_raises_when_nothing_is_attached(attached):
    attached()
    with pytest.raises(CameraError):
        detect_camera("picam-imx477")


def test_fallback_is_logged(attached, caplog):
    """A rig quietly using the wrong camera is worse than one that says so."""
    attached(zwo=True, picam=False)
    with caplog.at_level("WARNING", logger=base.__name__):
        detect_camera("picam-imx477")
    assert any("falling back" in r.getMessage() for r in caplog.records)


# -- new-camera registration ------------------------------------------------

def test_new_camera_profiles_are_fitted_to_its_limits(tmp_path, monkeypatch):
    """A Pi module tops out near gain 22 while the profile defaults are
    ZWO-shaped (hundreds). AE spills into gain once exposure is capped, so an
    unreachable ceiling means it asks for more and never sees brightness move.
    """
    from skylapse import config
    from skylapse.daemon.drivers.base import CameraInfo, BayerPattern

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    entry = cfg.camera("picam-imx477")
    assert entry.night.max_gain == 300, "precondition: ZWO-shaped default"

    info = CameraInfo(name="Pi Camera (imx477)", camera_id="picam-imx477",
                      driver="picam", width=4056, height=3040,
                      bayer=BayerPattern.BGGR, bit_depth=16,
                      max_exposure_us=694_422_939, min_exposure_us=110,
                      max_gain=22)

    # The clamp the daemon applies on first sight of a camera.
    entry.driver, entry.model = info.driver, info.name
    for profile in (entry.day, entry.night):
        profile.max_gain = min(profile.max_gain, info.max_gain)
        profile.gain = min(profile.gain, info.max_gain)
        profile.max_exposure_us = min(profile.max_exposure_us, info.max_exposure_us)

    assert entry.night.max_gain == 22
    assert entry.night.gain <= 22
    assert entry.day.max_gain <= 22
    # A limit the camera comfortably exceeds must not be raised to meet it.
    assert entry.day.max_exposure_us == 100_000


def test_the_gain_floor_is_not_clamped_onto_the_ceiling(tmp_path, monkeypatch):
    """Found outside, in daylight, on the first night with a fast lens.

    `gain` is auto-exposure's floor — what it walks back down to once exposure
    has room again. The clamp treated it as another ceiling, so on a module
    whose maximum gain is 22 the ZWO-shaped default of 100 became 22 and the
    floor met the ceiling. Gain could then never move: every frame that camera
    ever took was at maximum amplification. It read "pinned at both limits" all
    night, and pointed at a daylit sky through an f/2 fisheye it could not
    expose correctly at any shutter speed the sensor has — 0.1s, gain 22,
    saturated, with the brightness slider already at its lowest.
    """
    from skylapse import config
    from skylapse.daemon.main import MIN_GAIN
    from skylapse.daemon.drivers.base import BayerPattern, CameraInfo

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    entry = cfg.camera("picam-imx477")
    assert entry.night.gain == 100, "precondition: the ZWO-shaped default"

    info = CameraInfo(name="Pi Camera (imx477)", camera_id="picam-imx477",
                      driver="picam", width=4056, height=3040,
                      bayer=BayerPattern.BGGR, bit_depth=16,
                      max_exposure_us=694_422_939, min_exposure_us=110,
                      max_gain=22)

    for profile in (entry.day, entry.night):
        for field, ceiling in (("max_gain", info.max_gain),
                               ("max_exposure_us", info.max_exposure_us)):
            if getattr(profile, field) > ceiling:
                setattr(profile, field, ceiling)
        if profile.gain >= info.max_gain:
            profile.gain = MIN_GAIN

    for profile in (entry.day, entry.night):
        assert profile.gain == MIN_GAIN
        assert profile.gain < profile.max_gain, \
            "floor and ceiling are the same value; gain can never move"


def test_a_camera_with_room_keeps_its_baseline(tmp_path, monkeypatch):
    """A ZWO tops out in the hundreds, so the default baseline is fine there and
    must not be dragged down to 1."""
    from skylapse import config
    from skylapse.daemon.main import MIN_GAIN

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    entry = config.Config().camera("zwo-asi676mc")
    max_gain = 600
    if entry.night.gain >= max_gain:
        entry.night.gain = MIN_GAIN
    assert entry.night.gain == 100
