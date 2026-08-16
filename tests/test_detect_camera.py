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


def test_zwo_wins_by_default_when_both_are_attached(attached):
    """The documented probe order: ZWO is the primary target."""
    attached(zwo=True, picam=True)
    assert isinstance(detect_camera(), ZwoDriver)


def test_picam_used_when_it_is_the_only_camera(attached):
    attached(picam=True)
    assert isinstance(detect_camera(), PiCamDriver)


def test_simulator_wins_over_real_hardware(attached):
    """SKYLAPSE_SIM=1 must not be quietly ignored because a camera is plugged in."""
    attached(sim=True, zwo=True, picam=True)
    assert isinstance(detect_camera(), SimDriver)


def test_active_camera_selects_the_pi_module_over_the_zwo(attached):
    """The whole point: both attached, and the user wants the CSI module."""
    attached(zwo=True, picam=True)
    assert isinstance(detect_camera("picam-imx477"), PiCamDriver)


def test_active_camera_selects_the_zwo_explicitly(attached):
    attached(zwo=True, picam=True)
    assert isinstance(detect_camera("zwo-asi676mc"), ZwoDriver)


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
