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

Any resistive element works: a ring of power resistors around the dome base (~3–5 W at 12V), a USB lens-warmer band, or nichrome. Keep it on the **12V rail**, not the Pi's 5V — see [why the power is built this way](/hardware/#why-this-power-architecture).

## A Skylapse heater ring is coming <span class="pill">coming soon</span>

A purpose-made 12V heater ring for the dome, switched by Skylapse exactly as described above, is in the works. Estimated **~$20**. It will be announced on the [Store](/store/) page and in the [GitHub release notes](https://github.com/mattg8892/skylapse/releases) when it is real and in hand — not before. Until then, the resistor ring above works fine.
