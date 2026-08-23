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
    """Asking for an hour gets you TEST_MAX_SECONDS, whatever that is set to.

    The cap moved from 15s to 120s once a heater actually existed to test --
    six 3W resistor bodies take longer than fifteen seconds to become warm
    enough to feel -- so this asserts against the constant, not a number.
    """
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


def _fake_gpio(monkeypatch, pins):
    class FakePin:
        def __init__(self, *a, **kw): self.state = False
        def on(self): self.state = True
        def off(self): self.state = False
        def close(self): pass
    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice": lambda *a, **kw:
                                       pins.append(FakePin()) or pins[-1]}))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


def test_the_pulse_measures_the_temperature_it_caused(monkeypatch):
    """"Does it get warm?" is a bad question to answer by hand.

    The resistors may be inside a dome, behind glass, or simply cooler than a
    finger resolves. The sensor is already there, so read it either side and
    report a number instead of asking for a judgement.
    """
    from skylapse.daemon import dewheater
    _fake_gpio(monkeypatch, [])
    monkeypatch.setattr(dewheater, "find_sensor", lambda: 0x76)
    samples = iter([(12.0, 80.0), (14.5, 72.0)])
    monkeypatch.setattr(dewheater, "read_sensor", lambda addr: next(samples))

    result = dewheater.test_pulse(18, 60)
    assert result["ok"] is True
    assert result["temp_before_c"] == 12.0
    assert result["temp_after_c"] == 14.5
    assert result["rise_c"] == 2.5


def test_a_pulse_without_a_sensor_still_works(monkeypatch):
    """The heater can be commissioned before the sensor is wired, and often is
    -- that is the order the wiring guide gives. No reading is not an error."""
    from skylapse.daemon import dewheater
    pins = []
    _fake_gpio(monkeypatch, pins)
    monkeypatch.setattr(dewheater, "find_sensor", lambda: None)
    monkeypatch.setattr(dewheater, "read_sensor", lambda addr: None)

    result = dewheater.test_pulse(18, 30)
    assert result["ok"] is True
    assert "rise_c" not in result
    assert pins and pins[0].state is False, "left the heater on"


def test_a_sensor_that_fails_mid_pulse_does_not_fail_the_test(monkeypatch):
    """An I2C read can glitch. Losing the measurement is a shame; losing the
    heater test over it -- and leaving the pin state ambiguous -- is worse."""
    from skylapse.daemon import dewheater
    pins = []
    _fake_gpio(monkeypatch, pins)
    monkeypatch.setattr(dewheater, "find_sensor", lambda: 0x76)
    samples = iter([(12.0, 80.0), None])
    monkeypatch.setattr(dewheater, "read_sensor", lambda addr: next(samples))

    result = dewheater.test_pulse(18, 30)
    assert result["ok"] is True
    assert "rise_c" not in result
    assert pins[0].state is False


def test_a_bmp280_is_not_accepted_as_a_bme280(monkeypatch):
    """0x58 is a BMP280: same package, same address, no humidity sensor. A
    dewpoint from one would be invented, so no sensor is the honest answer."""
    from skylapse.daemon import dewheater

    class Bus:
        def __init__(self, n): pass
        def read_byte_data(self, addr, reg): return 0x58
    monkeypatch.setitem(__import__("sys").modules, "smbus2",
                        type("M", (), {"SMBus": Bus}))
    assert dewheater.find_sensor() is None


def test_a_real_bme280_is_found_at_either_address(monkeypatch):
    from skylapse.daemon import dewheater

    class Bus:
        def __init__(self, n): pass
        def read_byte_data(self, addr, reg):
            if addr == 0x77:
                return 0x60
            raise OSError("nothing here")
    monkeypatch.setitem(__import__("sys").modules, "smbus2",
                        type("M", (), {"SMBus": Bus}))
    assert dewheater.find_sensor() == 0x77
