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
        try:
            import smbus2
            bus = smbus2.SMBus(1)
            for addr in (BME280_ADDR, 0x77):
                try:
                    chip_id = bus.read_byte_data(addr, 0xD0)
                    if chip_id == 0x60:              # BME280 (0x58 = BMP280: no
                        self._addr = addr            # humidity -> useless here)
                        return True
                except OSError:
                    continue
            return False
        except Exception:
            return False

    def _read_bme280(self) -> tuple[float, float] | None:
        try:
            import bme280
            import smbus2
            bus = smbus2.SMBus(1)
            params = bme280.load_calibration_params(bus, self._addr)
            sample = bme280.sample(bus, self._addr, params)
            return sample.temperature, sample.humidity
        except Exception as exc:
            log.debug("BME280 read failed: %s", exc)
            return None

    def _set_gpio(self, on: bool) -> None:
        try:
            from gpiozero import OutputDevice
            if not hasattr(self, "_pin"):
                self._pin = OutputDevice(self.gpio_pin, active_high=True,
                                         initial_value=False)
            self._pin.value = on
        except Exception as exc:
            log.debug("GPIO set failed: %s", exc)
