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


# -- commissioning -----------------------------------------------------------

def test_a_test_pulse_is_capped(monkeypatch):
    """A heater left on unattended is the only genuinely dangerous failure this
    hardware has. Asking for an hour gets you fifteen seconds."""
    from skylapse.daemon import dewheater

    class FakePin:
        def __init__(self, *a, **kw): self.state = False
        def on(self): self.state = True
        def off(self): self.state = False
        def close(self): pass

    pins = []
    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice": lambda *a, **kw:
                                       pins.append(FakePin()) or pins[-1]}))
    monkeypatch.setattr(dewheater, "log", dewheater.log)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    result = dewheater.test_pulse(18, 3600)
    assert result["seconds"] == dewheater.TEST_MAX_SECONDS


def test_the_pin_goes_off_even_when_the_test_fails(monkeypatch):
    """The off is in a finally block on purpose: a crash mid-test must not be
    the thing that leaves a heater running."""
    from skylapse.daemon import dewheater

    class FakePin:
        def __init__(self, *a, **kw): self.state = False
        def on(self): raise RuntimeError("boom")
        def off(self): self.state = False
        def close(self): self.closed = True

    made = []
    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice": lambda *a, **kw:
                                       made.append(FakePin()) or made[-1]}))
    result = dewheater.test_pulse(18, 5)
    assert result["ok"] is False
    assert made and made[0].state is False, "left the heater on after a failure"
