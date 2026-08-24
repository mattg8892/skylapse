"""Dew heater control. EXPERIMENTAL — unverified on real hardware.

Gated by config.dew_heater.experimental_enabled (default OFF). While off,
this module is completely inert: no I2C probing, no GPIO access, no status.

Hardware (per DESIGN.md): BME280 on I2C for temp/humidity -> dewpoint
(Magnus formula); logic-level MOSFET on a GPIO pin switches the heater.
Hysteresis: ON when dome temp is within `on_margin_c` of dewpoint, OFF once
it clears `off_margin_c` — no chatter. Auto-hides if no BME280 responds.

The controller logic below is pure and unit-tested. The two hardware
touch-points (_read_bme280, _set_gpio) are thin, isolated, and are the ONLY
unverified code — first Pi session verifies them and removes this banner.
"""
from __future__ import annotations

import logging
import math

log = logging.getLogger("skylapse.dewheater")

BME280_ADDR = 0x76           # 0x77 on some boards; both probed


def dewpoint_c(temp_c: float, humidity_pct: float) -> float:
    """Magnus formula. Accurate within ~0.1C over -45..60C."""
    a, b = 17.62, 243.12
    rh = max(0.1, min(100.0, humidity_pct))
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return (b * gamma) / (a - gamma)


class HeaterController:
    """Pure hysteresis logic: no I/O, fully testable.

    ON  when (temp - dewpoint) <= on_margin_c
    OFF when (temp - dewpoint) >= off_margin_c
    Holds previous state in the dead band between the two.
    """

    def __init__(self, on_margin_c: float = 2.0, off_margin_c: float = 4.0) -> None:
        assert off_margin_c > on_margin_c, "hysteresis band must be positive"
        self.on_margin = on_margin_c
        self.off_margin = off_margin_c
        self.heating = False

    def update(self, temp_c: float, humidity_pct: float) -> bool:
        margin = temp_c - dewpoint_c(temp_c, humidity_pct)
        if margin <= self.on_margin:
            self.heating = True
        elif margin >= self.off_margin:
            self.heating = False
        return self.heating


class DewHeater:
    """Hardware orchestration. Constructed only when the experimental flag is
    on; degrades to disabled if the sensor or GPIO stack is absent."""

    def __init__(self, gpio_pin: int, on_margin_c: float, off_margin_c: float) -> None:
        self.controller = HeaterController(on_margin_c, off_margin_c)
        self.gpio_pin = gpio_pin
        self.available = self._probe_sensor()
        self.last: dict | None = None
        if not self.available:
            log.info("Dew heater: no BME280 detected; feature hidden")

    def tick(self) -> dict | None:
        """One control cycle. Returns status dict for the dashboard, or None."""
        if not self.available:
            return None
        reading = self._read_bme280()
        if reading is None:
            return self.last
        temp, hum = reading
        heating = self.controller.update(temp, hum)
        self._set_gpio(heating)
        self.last = {
            "temp_c": round(temp, 1),
            "humidity_pct": round(hum, 1),
            "dewpoint_c": round(dewpoint_c(temp, hum), 1),
            "heating": heating,
        }
        return self.last

    def off(self) -> None:
        """Safe shutdown: heater hard-off (daemon exit path)."""
        if self.available:
            self._set_gpio(False)

    # -- hardware touch-points: UNVERIFIED, isolated on purpose --------------

    def _probe_sensor(self) -> bool:
        self._addr = find_sensor()
        return self._addr is not None

    def _read_bme280(self) -> tuple[float, float] | None:
        return read_sensor(self._addr)

    def _set_gpio(self, on: bool) -> None:
        try:
            from gpiozero import OutputDevice
            if not hasattr(self, "_pin"):
                self._pin = OutputDevice(self.gpio_pin, active_high=True,
                                         initial_value=False)
            self._pin.value = on
        except Exception as exc:
            log.debug("GPIO set failed: %s", exc)


# -- sensor -----------------------------------------------------------------

def find_sensor() -> int | None:
    """The I2C address a BME280 answers on, or None.

    A BMP280 reports chip id 0x58 and is rejected: it looks identical, sits at
    the same addresses, and cannot measure humidity -- which is the whole input
    to a dewpoint. Better to report no sensor than to compute nonsense.
    """
    try:
        import smbus2
        bus = smbus2.SMBus(1)
    except Exception:
        return None
    for addr in (BME280_ADDR, 0x77):
        try:
            if bus.read_byte_data(addr, 0xD0) == 0x60:
                return addr
        except OSError:
            continue
    return None


def read_sensor(addr: int | None) -> tuple[float, float] | None:
    """(temperature C, relative humidity %) or None."""
    if addr is None:
        return None
    try:
        import bme280
        import smbus2
        bus = smbus2.SMBus(1)
        params = bme280.load_calibration_params(bus, addr)
        sample = bme280.sample(bus, addr, params)
        return sample.temperature, sample.humidity
    except Exception as exc:
        log.debug("BME280 read failed: %s", exc)
        return None


def read_error(addr: int | None) -> str:
    """Why a read failed, as a sentence, or "" if it did not.

    Detecting the chip and reading it use different libraries: the probe is raw
    smbus2, the read needs RPi.bme280. So a camera can report "sensor found"
    for ever and never produce a single reading -- which is exactly what
    happened, because the dew heater's dependencies were an optional extra that
    neither the installer nor the updater ever installed.
    """
    if addr is None:
        return "no BME280 answered on the bus"
    try:
        import bme280         # noqa: F401
    except Exception:
        return ("the RPi.bme280 library is not installed, so the sensor can be "
                "detected but not read")
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        params = bme280.load_calibration_params(bus, addr)
        bme280.sample(bus, addr, params)
        return ""
    except Exception as exc:
        return f"reading the sensor failed: {exc}"


def diagnostics() -> dict:
    """What is actually present, rather than what is supposed to be.

    Written after a heater that reported a successful test did not switch on:
    no LED on the MOSFET, nothing on a thermal camera. The test could say it
    had driven the pin, but not whether anything real was behind it -- and
    gpiozero will happily drive a mock. Facts beat a hunt.
    """
    info: dict = {}
    for name in ("smbus2", "bme280", "gpiozero"):
        try:
            __import__(name)
            info[name] = True
        except Exception:
            info[name] = False

    addr = find_sensor()
    info["sensor_addr"] = hex(addr) if addr is not None else None
    info["sensor_error"] = read_error(addr)
    info["pin_factory"] = pin_factory_name()
    return info


def pin_factory_name() -> str | None:
    """Which gpiozero backend is in play.

    A MockFactory switches nothing and reports success, which is
    indistinguishable from working hardware unless you go and look. On a Pi 5
    the backend must be lgpio -- the older RPi.GPIO path does not drive this
    board's pins at all.
    """
    try:
        from gpiozero import Device
    except Exception as exc:
        log.debug("gpiozero unavailable: %s", exc)
        return None
    factory = Device.pin_factory
    if factory is None:
        # gpiozero resolves its backend lazily, on the first Device. Asking
        # before then used to report "NoneType", which reads like a broken
        # install and is only "nobody has used a pin yet".
        return None
    return type(factory).__name__


# -- commissioning ----------------------------------------------------------

# The longest a manual test may run, whatever it is asked for. The pin goes low
# again even if the caller disappears mid-request.
#
# This was 15s, chosen before anyone had built one. Too short to be useful: a
# dew heater is a handful of watts spread over resistor bodies with real
# thermal mass, and fifteen seconds of that is not something a finger can
# detect. A test you cannot read the result of is not a test.
TEST_MAX_SECONDS = 120


def test_pulse(gpio_pin: int, seconds: float) -> dict:
    """Drive the heater pin, then hard off, and measure what it did.

    First power-on of a heater is the moment to be careful, and "switch it on
    and see" is not a thing anyone should have to do by editing config and
    waiting for the dewpoint to be met. The off is in a finally block so a
    crash or a dropped connection still ends with the heater off.

    The sensor is read either side of the pulse, because "does it get warm?"
    answered by hand is a poor test -- the resistors may be inside a dome, or
    behind glass, or simply cooler than a finger can resolve. A number settles
    it. No rise is not proof of a fault, though: it depends where the sensor
    sits relative to the heat, so the reading is reported rather than judged.
    """
    import time as _time

    seconds = max(0.5, min(float(seconds), TEST_MAX_SECONDS))
    try:
        from gpiozero import OutputDevice
    except Exception as exc:
        return {"ok": False, "error": f"no GPIO library available: {exc}"}

    addr = find_sensor()
    before = read_sensor(addr)

    pin = None
    try:
        pin = OutputDevice(gpio_pin, active_high=True, initial_value=False)
        pin.on()
        _time.sleep(seconds)
        after = read_sensor(addr)
        result = {"ok": True, "seconds": seconds, "gpio_pin": gpio_pin,
                  # Which backend actually drove the pin. A test that says it
                  # switched a heater on, on a rig where nothing switched, is
                  # worse than no test -- it sends you to check the wiring.
                  "pin_factory": pin_factory_name()}
        if before and after:
            result["temp_before_c"] = round(before[0], 2)
            result["temp_after_c"] = round(after[0], 2)
            result["rise_c"] = round(after[0] - before[0], 2)
        else:
            result["sensor_error"] = read_error(addr)
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if pin is not None:
            try:
                pin.off()
                pin.close()
            except Exception:
                log.error("Could not switch the heater pin off after a test")
