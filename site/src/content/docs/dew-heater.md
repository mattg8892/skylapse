---
title: Dew heater
description: How Skylapse controls a dew heater from a BME280 sensor, and how to wire one today.
sidebar:
  order: 5
  badge:
    text: Coming soon
    variant: tip
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

:::caution[BME280, not BMP280]
The BMP280 looks identical and costs a dollar less, but it has **no humidity sensor** — and humidity is the whole input to a dewpoint. Skylapse checks the chip id and rejects a BMP280 rather than compute nonsense.
:::

## Wiring it today

The [reference build](/hardware/) already has everything except the heating element:

```
Pi GPIO 18 ─► MOSFET module (signal)
12V rail   ─► MOSFET module (power in)
              MOSFET module (power out) ─► heater ─► GND

BME280 ─► Pi I2C (SDA, SCL, 3.3V, GND)
```

Any resistive element works: a ring of power resistors around the dome base (~3–5 W at 12V), a USB lens-warmer band, or nichrome. Keep it on the **12V rail**, not the Pi's 5V — see [why the power is built this way](/hardware/#why-this-power-architecture). The complete hookup, every wire numbered, is on the [Hardware page](/hardware/#wiring).

## A Skylapse heater ring is coming <span class="pill">coming soon</span>

A purpose-made 12V heater ring for the dome, switched by Skylapse exactly as described above. First prototypes are in hand and measured to spec on the bench — 30.3Ω, ~4.2W at 12V, warm across the whole face in minutes. Estimated **~$20**. It goes on sale on the [Store](/store/) page once it has run real nights on the reference rig — announcements there and in the [GitHub release notes](https://github.com/mattg8892/skylapse/releases). Until then, the resistor ring above works fine.
