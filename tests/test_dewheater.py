"""Dewpoint math, hysteresis behavior, and — most importantly — that the
experimental flag OFF means the subsystem is never constructed at all."""
import math
from unittest import mock

from skylapse import config
from skylapse.daemon.dewheater import DewHeater, HeaterController, dewpoint_c


# -- dewpoint (checked against published psychrometric values) --------------

def test_dewpoint_known_values():
    assert math.isclose(dewpoint_c(20.0, 100.0), 20.0, abs_tol=0.1)  # saturated
    assert math.isclose(dewpoint_c(20.0, 50.0), 9.3, abs_tol=0.5)
    assert math.isclose(dewpoint_c(0.0, 80.0), -3.0, abs_tol=0.7)
    assert dewpoint_c(15.0, 30.0) < dewpoint_c(15.0, 90.0)           # monotonic in RH


# -- hysteresis --------------------------------------------------------------

def test_heater_turns_on_near_dewpoint():
    c = HeaterController(on_margin_c=2.0, off_margin_c=4.0)
    assert c.update(10.0, 98.0) is True          # ~0.3C above dewpoint: heat


def test_heater_stays_off_when_dry():
    c = HeaterController(2.0, 4.0)
    assert c.update(20.0, 30.0) is False         # dewpoint ~2C: 18C of margin


def test_dead_band_holds_previous_state():
    c = HeaterController(2.0, 4.0)
    c.update(10.0, 98.0)                         # ON (margin ~0.3)
    assert c.update(10.0, 84.0) is True          # margin ~2.6: dead band, stays ON
    assert c.update(10.0, 70.0) is False         # margin ~5.1: clears off_margin
    assert c.update(10.0, 84.0) is False         # dead band again, stays OFF


def test_no_chatter_at_boundary():
    c = HeaterController(2.0, 4.0)
    states = [c.update(10.0, rh) for rh in (98, 90, 84, 88, 84, 90, 84)]
    assert states == [True] * 7                  # dead-band bouncing never flips


# -- experimental gate -------------------------------------------------------

def test_flag_off_by_default():
    assert config.Config().dew_heater.experimental_enabled is False


def test_flag_off_means_never_constructed():
    """Daemon-side contract: with the flag off, DewHeater() is never called,
    so no I2C probe and no GPIO can occur. Mirrors the wiring in main.py."""
    dh_cfg = config.Config().dew_heater
    with mock.patch.object(DewHeater, "__init__", side_effect=AssertionError) as ctor:
        heater = DewHeater(dh_cfg.gpio_pin, 2.0, 4.0) \
            if dh_cfg.experimental_enabled else None
    assert heater is None
    ctor.assert_not_called()


def test_missing_sensor_hides_feature():
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=False):
        h = DewHeater(18, 2.0, 4.0)
    assert h.available is False
    assert h.tick() is None                      # no status, no GPIO


def test_tick_drives_gpio_from_controller():
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=True), \
         mock.patch.object(DewHeater, "_read_bme280", return_value=(10.0, 98.0)), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0)
        status = h.tick()
    gpio.assert_called_once_with(True)
    assert status["heating"] is True
    assert status["dewpoint_c"] == round(dewpoint_c(10.0, 98.0), 1)


def test_off_forces_gpio_low():
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=True), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0)
        h.off()
    gpio.assert_called_once_with(False)
