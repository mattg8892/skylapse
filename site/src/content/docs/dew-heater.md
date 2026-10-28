---
title: Dew heater
description: How Skylapse controls a dew heater from a BME280 sensor, and how to wire one today.
sidebar:
  order: 5
---

A fisheye dome pointed at a clear sky radiates heat to space and drops below the air temperature. When it crosses the dewpoint, the dome fogs and the rest of the night is lost — the reference rig has logged dew arriving twice in a single night, between 02:30 and 05:00. A few watts of heat on the dome base fixes it, and Skylapse can run that heat itself.

## How Skylapse controls it

- A **BME280** on the Pi's I2C bus (address 0x76 or 0x77, probed automatically) gives temperature, humidity and pressure. Skylapse computes the **dewpoint** from them.
- When the temperature closes in on the dewpoint the heater switches **on**; once there is comfortable margin again it switches **off**. There is a dead band between the two thresholds, so the heater does not chatter on a marginal night.
- The heater is driven from a **GPIO pin through a MOSFET module** (BCM 18 by default). It is forced **off** whenever the capture daemon exits, so a stopped daemon can never leave the heater on.
- Or skip the sensor entirely: the **Heater tab** has a manual on/off switch (v0.5.27+).

## Two sensors: a thermostat

As of **v0.5.38**, a second BME280 mounted *inside* the dome upgrades the loop from a switch to a thermostat. Bridge the second board's **SDO pad to VCC** (one solder joint) so it answers at address 0x77, wire it to the same four I2C pins as the first, and Skylapse finds it by itself — the log says `Dome sensor found at 0x77`.

With both sensors present:

- The control margin becomes **dome temperature vs ambient dewpoint** — the actual surface being protected against the actual air threatening it, which no single sensor can measure. (The generous default margins exist precisely to compensate for measuring air instead of glass; a dome sensor removes the guesswork.)
- A **dome temperature limit** (default 45°C, adjustable on the Heater tab) cuts the heater no matter what asked for heat — **including the manual switch**, since a manual switch left on is exactly the overheat case. It latches, and releases once the dome has cooled 5°C below the limit, so it never chatters.

One sensor, at either address, behaves exactly as before.

:::note[First switch-on: turn I²C on]
Raspberry Pi OS ships the I²C bus **off**, so a freshly set-up camera reports *no BME280 detected* even when both sensors are wired perfectly. The Heater tab offers **Enable I²C**; tap it, then **reboot once** from Settings. The sensors are found the moment the camera is back — on the reference rig both appeared within seconds, outside at 0x76 and dome at 0x77.
:::

:::caution[BME280, not BMP280]
The BMP280 looks identical and costs a dollar less, but it has **no humidity sensor** — and humidity is the whole input to a dewpoint. Skylapse checks the chip id and rejects a BMP280 rather than compute nonsense.
:::

## Wiring it today

The [reference build](/hardware/) uses the Skylapse 12V heater ring below, but any resistive element wires in the same way:

```
Pi GPIO 18 ─► MOSFET module (signal)
12V rail   ─► MOSFET module (power in)
              MOSFET module (power out) ─► heater ─► GND

BME280 ─► Pi I2C (SDA, SCL, 3.3V, GND)
```

Any resistive element works: a ring of power resistors around the dome base (~3–5 W at 12V), a USB lens-warmer band, or nichrome. Keep it on the **12V rail**, not the Pi's 5V — see [why the power is built this way](/hardware/#why-this-power-architecture). The complete hookup, every wire numbered, is on the [Hardware page](/hardware/#wiring), and the MOSFET module's screws are called out one by one on a [photo of the board](/hardware/#the-mosfet-module-up-close) — its labels are on the back, which is easy to get wrong.

## The Skylapse heater ring

A purpose-made 12V heater ring for the dome, switched by Skylapse exactly as described above. The copper trace *is* the element — no components, two solder pads for the feed wires — so there is nothing on it to fail.

| | |
|---|---|
| Fits | 50 mm centre hole, 85 mm outer — the seat of a standard acrylic dome |
| Supply | 12 V from the rig's own rail, through the MOSFET module |
| Element | ~31 Ω, 0.39 A, ~4.2 W warm |
| Wiring | two pads, no polarity; 24 AWG is plenty |

It is in the reference rig now and verified under software control: with both BME280s in place, a two-minute heater test took the dome sensor from 18.9 °C to 23.6 °C while the outside sensor did not move, the dome limit armed, and the thermostat took over from there. It runs nightly on the reference rig from here on. It is sold at a small margin on the [Store](/store/) page — announcements there and in the [GitHub release notes](https://github.com/mattg8892/skylapse/releases). The resistor ring above still works fine if you would rather wind your own.
