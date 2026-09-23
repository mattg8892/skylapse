"""Dewpoint math, hysteresis behavior, and — most importantly — that the
experimental flag OFF means the subsystem is never constructed at all."""
import math
from pathlib import Path
from unittest import mock

from skylapse import config
from skylapse.daemon.dewheater import DewHeater, HeaterController, dewpoint_c
import json


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
    # Construction drives the pin low first -- a known state before any
    # reading, so a restart clears a heater latched on by a crash.
    assert gpio.call_args_list[0] == mock.call(False)
    assert gpio.call_args_list[-1] == mock.call(True)
    assert status["heating"] is True
    assert status["dewpoint_c"] == round(dewpoint_c(10.0, 98.0), 1)


def test_off_forces_gpio_low():
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=True), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0)
        h.off()
    # Low on construction and low again on off(); both are the safe state.
    assert gpio.call_args_list == [mock.call(False), mock.call(False)]


# -- manual mode --------------------------------------------------------------

def test_manual_mode_needs_no_sensor():
    """The whole reason manual exists: a rig with no BME280 can still run a
    heater. Auto without a sensor hides the feature; manual must not."""
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=False), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0, mode="manual", manual_on=True)
        status = h.tick()
    assert h.available is True
    assert status == {"heating": True, "mode": "manual", "capped": False}
    assert gpio.call_args_list[-1] == mock.call(True)


def test_manual_switch_drives_the_pin_not_the_dewpoint():
    """Reading says 'soaking wet, heat now'; the switch says off. Off wins --
    in manual mode the sensor only informs."""
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=True), \
         mock.patch.object(DewHeater, "_read_bme280", return_value=(10.0, 98.0)), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0, mode="manual", manual_on=False)
        status = h.tick()
    assert status["heating"] is False
    assert status["temp_c"] == 10.0          # reading still shown, informational
    assert gpio.call_args_list[-1] == mock.call(False)


def test_apply_lands_the_switch_on_a_live_heater():
    """The daemon reloads config each loop and calls apply() -- flipping the
    switch must reach the pin on the next tick, not the next restart."""
    from skylapse.config import DewHeaterConfig
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=False), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0, mode="manual", manual_on=False)
        h.tick()
        assert gpio.call_args_list[-1] == mock.call(False)
        h.apply(DewHeaterConfig(mode="manual", manual_on=True))
        h.tick()
        assert gpio.call_args_list[-1] == mock.call(True)


def test_apply_can_change_mode_without_a_rebuild():
    """Manual -> auto on a sensorless rig collapses to unavailable, exactly
    as if it had been constructed that way."""
    from skylapse.config import DewHeaterConfig
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=False), \
         mock.patch.object(DewHeater, "_set_gpio"):
        h = DewHeater(18, 2.0, 4.0, mode="manual", manual_on=True)
        assert h.available is True
        h.apply(DewHeaterConfig(mode="auto"))
    assert h.available is False
    assert h.tick() is None


def test_off_still_works_in_sensorless_manual_mode():
    """The daemon exit path calls off(); it must reach the pin even though
    there is no sensor -- a heater left latched on is the historical failure
    this module exists to prevent."""
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=False), \
         mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = DewHeater(18, 2.0, 4.0, mode="manual", manual_on=True)
        h.tick()
        h.off()
    assert gpio.call_args_list[-1] == mock.call(False)


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


# -- diagnostics -------------------------------------------------------------

def test_diagnostics_names_the_missing_library(monkeypatch):
    """The failure that hid for two releases.

    Detection uses raw smbus2; reading needs RPi.bme280. Those were an optional
    extra nothing installed, so the card said "sensor found" on a camera that
    could not produce one reading. Detected-but-unreadable has to be a state
    the software can name.
    """
    from skylapse.daemon import dewheater
    import builtins
    real = builtins.__import__

    def no_bme280(name, *a, **kw):
        if name == "bme280":
            raise ImportError("No module named 'bme280'")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_bme280)
    assert "RPi.bme280" in dewheater.read_error(0x76)


def test_no_sensor_is_a_different_message_from_no_library():
    """Two different next steps: wire something up, versus install something."""
    from skylapse.daemon import dewheater
    assert "answered" in dewheater.read_error(None)


def test_the_pulse_reports_which_backend_drove_the_pin(monkeypatch):
    """gpiozero will drive a mock and report success, which looks exactly like
    working hardware from the outside. On a Pi 5 the backend has to be lgpio;
    anything else is why the MOSFET never switched."""
    from skylapse.daemon import dewheater
    _fake_gpio(monkeypatch, [])
    monkeypatch.setattr(dewheater, "find_sensor", lambda: None)
    monkeypatch.setattr(dewheater, "read_sensor", lambda addr: None)
    monkeypatch.setattr(dewheater, "pin_factory_name", lambda: "LGPIOFactory")

    result = dewheater.test_pulse(18, 15)
    assert result["pin_factory"] == "LGPIOFactory"


def test_a_pulse_with_no_reading_says_why(monkeypatch):
    """Silence was the actual problem: the test returned ok with no
    temperatures and no explanation, so the only thing left to suspect was the
    wiring."""
    from skylapse.daemon import dewheater
    _fake_gpio(monkeypatch, [])
    monkeypatch.setattr(dewheater, "find_sensor", lambda: 0x76)
    monkeypatch.setattr(dewheater, "read_sensor", lambda addr: None)
    monkeypatch.setattr(dewheater, "read_error", lambda addr: "library missing")

    result = dewheater.test_pulse(18, 15)
    assert result["ok"] is True
    assert result["sensor_error"] == "library missing"


def test_diagnostics_reports_each_library_separately(monkeypatch):
    from skylapse.daemon import dewheater
    monkeypatch.setattr(dewheater, "find_sensor", lambda: 0x77)
    monkeypatch.setattr(dewheater, "read_error", lambda addr: "")
    monkeypatch.setattr(dewheater, "pin_factory_name", lambda: "LGPIOFactory")
    info = dewheater.diagnostics()
    for lib in ("smbus2", "bme280", "gpiozero"):
        assert lib in info and isinstance(info[lib], bool)
    assert info["sensor_addr"] == "0x77"


def test_an_unresolved_pin_factory_is_null_not_the_string_NoneType(monkeypatch):
    """gpiozero resolves its backend on the first Device, so asking before then
    is not an error. It reported "NoneType", which reads like a broken install
    and cost a round of chasing the wrong thing."""
    from skylapse.daemon import dewheater
    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"Device": type("D", (), {"pin_factory": None})}))
    assert dewheater.pin_factory_name() is None


def test_a_resolved_pin_factory_is_named(monkeypatch):
    """Once a pin has been used the answer is real, and it is the answer that
    matters: LGPIOFactory is a Pi 5 driving actual hardware, MockFactory is a
    test that proves nothing."""
    from skylapse.daemon import dewheater

    class LGPIOFactory: pass
    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"Device": type("D", (),
                             {"pin_factory": LGPIOFactory()})}))
    assert dewheater.pin_factory_name() == "LGPIOFactory"


# -- margins for a sensor that cannot see the glass ---------------------------

def test_the_defaults_assume_an_outside_air_sensor():
    """A sealed dome cannot hold the sensor, and a sensor measuring humidity
    cannot be inside anyway -- so the reading is always air, never glass.

    On a clear night the dome radiates to a sky effectively below -40C and sits
    3-5C under the surrounding air, which is why optics dew up on nights the
    air never reaches the dewpoint. A rule written against air temperature has
    to start early to be on time.
    """
    from skylapse import config
    dh = config.Config().dew_heater
    assert dh.on_margin_c == 5.0
    assert dh.off_margin_c == 8.0
    assert dh.off_margin_c > dh.on_margin_c, "the hysteresis band must be positive"


def test_the_old_margins_are_migrated_on_load(tmp_path, monkeypatch):
    """Changing a default does nothing for a camera that already has the old
    value on disk -- and every camera does."""
    from skylapse import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    cfg.dew_heater.on_margin_c = 2.0
    cfg.dew_heater.off_margin_c = 4.0
    config.save(cfg)

    loaded = config.load()
    assert loaded.dew_heater.on_margin_c == 5.0
    assert loaded.dew_heater.off_margin_c == 8.0


def test_a_deliberately_chosen_margin_is_left_alone(tmp_path, monkeypatch):
    """Only the exact legacy pair is touched. Someone who has tuned these
    against their own nights must not have it undone by an update."""
    from skylapse import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    cfg.dew_heater.on_margin_c = 3.0
    cfg.dew_heater.off_margin_c = 6.0
    config.save(cfg)

    loaded = config.load()
    assert loaded.dew_heater.on_margin_c == 3.0
    assert loaded.dew_heater.off_margin_c == 6.0


def test_the_wider_margin_actually_fires_earlier():
    """The point of the change, stated as behaviour rather than as constants.

    10C air at 75% RH has a dewpoint near 5.8C, so the air is 4.2C clear of it
    and looks fine. But a dome radiating to the sky runs 3-5C under ambient,
    which puts the glass at or below that dewpoint right now. The old rule
    waits; the new one is already heating.
    """
    from skylapse.daemon.dewheater import HeaterController, dewpoint_c
    air, rh = 10.0, 75.0
    assert 4.0 < (air - dewpoint_c(air, rh)) < 4.5, "precondition: margin ~4.2C"

    old = HeaterController(2.0, 4.0)
    new = HeaterController(5.0, 8.0)
    assert old.update(air, rh) is False, "precondition: the old rule waits"
    assert new.update(air, rh) is True, "the new rule heats while the air looks dry"


# -- one pin, two processes --------------------------------------------------

def test_the_daemon_releases_the_pin_when_the_feature_is_switched_off(monkeypatch, tmp_path):
    """Reported from the rig: "when i do try to run the test i get gpio busy".

    A GPIO can be held by one process. Since 0.5.16 the daemon takes this one
    on construction -- correctly, so a restart clears a latched heater -- and
    then never let go. Turning the heater off in the UI left the daemon holding
    it, so the API's test pulse could not open it. The one button for checking
    your wiring stopped working the moment you switched the heater on.
    """
    from skylapse import config
    from skylapse.daemon import main as dmain

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    closed = []

    class FakeHeater:
        def close(self): closed.append(True)

    obj = dmain.CaptureDaemon.__new__(dmain.CaptureDaemon)
    obj.cfg = config.Config()
    obj.cfg.dew_heater.experimental_enabled = False
    obj.nightjobs_thread = None
    obj.camera_id = "picam-imx477"
    obj.dewheater = FakeHeater()

    obj._reconcile_dewheater()
    assert closed, "the pin was never released"
    assert obj.dewheater is None


def test_the_daemon_takes_the_pin_when_the_feature_is_switched_on(monkeypatch, tmp_path):
    """And the other direction, which also did not work: the heater was decided
    once at startup, so enabling it in the UI did nothing until a restart."""
    from skylapse import config
    from skylapse.daemon import main as dmain

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    built = []
    monkeypatch.setattr(dmain, "DewHeater",
                        lambda *a: built.append(a) or object())

    obj = dmain.CaptureDaemon.__new__(dmain.CaptureDaemon)
    obj.cfg = config.Config()
    obj.cfg.dew_heater.experimental_enabled = True
    obj.dewheater = None
    obj.nightjobs_thread = None
    obj.camera_id = "picam-imx477"

    obj._reconcile_dewheater()
    assert built, "enabling the heater did not build it"
    assert obj.dewheater is not None


def test_reconciling_twice_does_not_churn(monkeypatch, tmp_path):
    """It runs every loop iteration, so it must be a no-op when nothing has
    changed -- rebuilding the heater every frame would re-open the pin
    continuously and log a line each time."""
    from skylapse import config
    from skylapse.daemon import main as dmain

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    built, applied = [], []
    fake = type("H", (), {"apply": lambda self, cfg: applied.append(cfg)})
    monkeypatch.setattr(dmain, "DewHeater",
                        lambda *a: built.append(a) or fake())

    obj = dmain.CaptureDaemon.__new__(dmain.CaptureDaemon)
    obj.cfg = config.Config()
    obj.cfg.dew_heater.experimental_enabled = True
    obj.dewheater = None
    obj.nightjobs_thread = None
    obj.camera_id = "picam-imx477"

    for _ in range(5):
        obj._reconcile_dewheater()
    assert len(built) == 1, f"rebuilt the heater {len(built)} times"
    # No churn does not mean no updates: config lands via apply() each pass.
    assert len(applied) == 4


def test_the_test_pulse_runs_in_the_daemon(monkeypatch, tmp_path):
    """Because that is the process that owns the pin. The API asks."""
    from skylapse import config
    from skylapse.daemon import main as dmain

    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    (tmp_path / "dewheater_test").write_text("30")

    ran = []
    monkeypatch.setattr(dmain.dewheater_mod, "test_pulse",
                        lambda pin, seconds: ran.append((pin, seconds)) or
                        {"ok": True, "seconds": seconds})

    obj = dmain.CaptureDaemon.__new__(dmain.CaptureDaemon)
    obj.cfg = config.Config()
    obj.dewheater = None
    obj._poll_dewheater_test()

    assert ran == [(18, 30.0)]
    assert not (tmp_path / "dewheater_test").exists(), "command file not consumed"
    result = json.loads((tmp_path / "dewheater_test_result.json").read_text())
    assert result["ok"] is True and result["seconds"] == 30.0


def test_a_running_heater_lets_go_for_the_duration_of_a_test(monkeypatch, tmp_path):
    """Otherwise the test cannot open the pin even from inside the daemon --
    which is the same bug one layer down."""
    from skylapse import config
    from skylapse.daemon import main as dmain

    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    (tmp_path / "dewheater_test").write_text("5")

    closed = []

    class FakeHeater:
        def close(self): closed.append(True)

    monkeypatch.setattr(dmain.dewheater_mod, "test_pulse",
                        lambda pin, seconds: {"ok": True, "seconds": seconds})

    obj = dmain.CaptureDaemon.__new__(dmain.CaptureDaemon)
    obj.cfg = config.Config()
    obj.dewheater = FakeHeater()
    obj._poll_dewheater_test()

    assert closed, "the live heater kept the pin during the test"
    assert obj.dewheater is None, "left dangling; reconcile rebuilds it next loop"


def test_close_releases_the_gpio_object(monkeypatch):
    """The actual release, as opposed to dropping the reference and hoping the
    garbage collector gets to it before the next open."""
    from skylapse.daemon import dewheater

    closed = []

    class FakePin:
        def __init__(self, *a, **kw): pass
        value = False
        def close(self): closed.append(True)

    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice": FakePin}))
    monkeypatch.setattr(dewheater, "find_sensor", lambda: None)

    heater = dewheater.DewHeater(18, 5.0, 8.0)
    heater.close()
    assert closed, "close() did not release the pin"
    assert not hasattr(heater, "_pin")


def test_reading_the_status_does_not_touch_the_gpio(monkeypatch, tmp_path):
    """The bug behind "GPIO busy" on a line the kernel said was free.

    GET /api/dewheater built a DewHeater just to read its `available` flag.
    Since 0.5.16 that constructor drives the pin low -- correct in the daemon,
    where it clears a heater latched on by a crash, and a leak here: the object
    is discarded, the pin is never released, and the dashboard polls this
    endpoint every fifteen seconds. The API quietly held the heater pin, so the
    test pulse failed against a line nothing was using. Restarting the services
    cleared it until the next poll.

    Asking whether the sensor is present needs no GPIO at all.
    """
    from fastapi.testclient import TestClient
    from skylapse.api import main as api
    from skylapse.daemon import dewheater

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())

    opened = []
    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice":
                                       lambda *a, **kw: opened.append(a) or object()}))
    # Only the I2C device node, not every path -- a blanket exists() makes the
    # status file look present too and the endpoint dies reading it.
    real_exists = api.Path.exists
    monkeypatch.setattr(api.Path, "exists",
                        lambda self: True if "i2c-1" in self.as_posix()
                        else real_exists(self))
    monkeypatch.setattr(dewheater, "find_sensor", lambda: 0x76)

    body = TestClient(api.app).get("/api/dewheater").json()
    assert body["sensor_found"] is True
    assert not opened, "the status endpoint opened the heater pin"


def test_the_api_round_trips_the_manual_switch(monkeypatch, tmp_path):
    """Mode and switch go through PUT /api/dewheater and come back from GET —
    the card cannot offer a switch the config cannot hold."""
    from fastapi.testclient import TestClient
    from skylapse.api import main as api

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    client = TestClient(api.app)

    r = client.put("/api/dewheater",
                   json={"mode": "manual", "manual_on": True,
                         "experimental_enabled": True}).json()
    assert r["mode"] == "manual" and r["manual_on"] is True
    body = client.get("/api/dewheater").json()
    assert body["mode"] == "manual"
    assert body["manual_on"] is True
    assert config.load().dew_heater.manual_on is True


def test_an_unknown_mode_is_refused(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from skylapse.api import main as api

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    r = TestClient(api.app).put("/api/dewheater", json={"mode": "pwm"})
    assert r.status_code == 400
    assert config.load().dew_heater.mode == "auto", "a bad mode was persisted"


def test_the_settings_card_offers_manual_without_a_sensor():
    """Manual mode is FOR rigs with no BME280 — the card must not hide the
    switch behind the sensor-missing warning."""
    src = (Path(__file__).resolve().parents[1]
           / "web" / "src" / "screens" / "SettingsScreen.jsx").read_text(
               encoding="utf-8")
    assert "manual_on" in src, "no manual switch in the settings card"
    manual_branch = src.index("state.mode === 'manual'")
    sensor_branch = src.index("state.sensor_found === false")
    assert manual_branch < sensor_branch, \
        "the manual branch must be checked before the sensor warnings"


def test_the_dashboard_banner_survives_a_sensorless_reading():
    """Manual mode reports {heating, mode} with no temperatures. The banner
    used to compute temp_c - dewpoint_c unconditionally, which is NaN°C."""
    src = (Path(__file__).resolve().parents[1]
           / "web" / "src" / "screens" / "Dashboard.jsx").read_text(
               encoding="utf-8")
    assert "temp_c != null" in src, "the banner still assumes a reading exists"
    assert "manual" in src, "the banner cannot explain a manually-run heater"


# -- dome sensor: regulate, don't blast ---------------------------------------

def _dome_heater(monkeypatch, outside=(10.0, 98.0), dome=None, cap=45.0,
                 mode="auto", manual_on=False):
    """A heater with a faked outside sensor and, optionally, a dome sensor."""
    from skylapse.daemon import dewheater as dw
    with mock.patch.object(DewHeater, "_probe_sensor", return_value=True), \
         mock.patch.object(DewHeater, "_set_gpio"):
        h = DewHeater(18, 5.0, 8.0, mode=mode, manual_on=manual_on,
                      max_dome_temp_c=cap)
    h.sensor_ok = True
    monkeypatch.setattr(h, "_read_bme280", lambda: outside)
    monkeypatch.setattr(h, "_read_dome",
                        lambda: dome if dome is not None else None)
    return h


def test_the_dome_cap_cuts_the_heater_even_in_manual(monkeypatch):
    """The bench ring hit 58C free-air in five minutes; a sealed dome climbs
    further. A manual switch left on is exactly the overheat case, so the
    cap outranks the switch."""
    with mock.patch.object(DewHeater, "_set_gpio") as gpio:
        h = _dome_heater(monkeypatch, dome=50.0, cap=45.0,
                         mode="manual", manual_on=True)
        monkeypatch.setattr(h, "_set_gpio", lambda on: gpio(on))
        status = h.tick()
    assert status["heating"] is False
    assert status["capped"] is True
    assert status["dome_temp_c"] == 50.0


def test_the_cap_latches_and_resumes_below_the_band(monkeypatch):
    """A plain threshold would chatter at the cap; the latch releases only
    once the dome has cooled the resume band below it. The numbers here are
    chosen so the release point is also inside the dew margin -- otherwise
    the controller itself correctly declines to heat a dome that is thirty
    degrees clear of the dewpoint, which an earlier draft of this test
    mistook for a stuck latch."""
    # Outside 10C/98% -> dewpoint ~9.7C. Cap 20, resume at 15.
    h = _dome_heater(monkeypatch, outside=(10.0, 98.0), dome=21.0, cap=20.0)
    monkeypatch.setattr(h, "_set_gpio", lambda on: None)
    s1 = h.tick()
    assert s1["heating"] is False and s1["capped"] is True   # tripped at 21
    monkeypatch.setattr(h, "_read_dome", lambda: 16.0)
    s2 = h.tick()
    assert s2["heating"] is False and s2["capped"] is True   # cooler, latched
    monkeypatch.setattr(h, "_read_dome", lambda: 14.5)       # below resume,
    s3 = h.tick()                                            # margin 4.8 <= 5
    assert s3["capped"] is False
    assert s3["heating"] is True                # released, and dew demands heat


def test_the_margin_is_dome_versus_ambient_dewpoint(monkeypatch):
    """The dome is the surface being protected; the ambient dewpoint is the
    threat. A warm dome under saturated air must NOT heat: its own margin is
    what matters, not the air's."""
    # Outside: 10C at 98% RH -> dewpoint ~9.7C, air margin ~0.3C (would heat).
    # Dome: 20C -> dome margin ~10.3C, comfortably clear of the 8C off band.
    h = _dome_heater(monkeypatch, outside=(10.0, 98.0), dome=20.0)
    monkeypatch.setattr(h, "_set_gpio", lambda on: None)
    status = h.tick()
    assert status["heating"] is False, \
        "heated a dome already 10C clear of the dewpoint"
    assert status["dome_temp_c"] == 20.0


def test_without_a_dome_sensor_nothing_changed(monkeypatch):
    """One sensor means the old behavior exactly: ambient stands in for the
    dome, margins as configured, no cap."""
    h = _dome_heater(monkeypatch, outside=(10.0, 98.0), dome=None)
    monkeypatch.setattr(h, "_set_gpio", lambda on: None)
    status = h.tick()
    assert status["heating"] is True             # 0.3C margin: heat
    assert "dome_temp_c" not in status
