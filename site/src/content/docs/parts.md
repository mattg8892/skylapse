---
title: Parts list
description: Every part in the Skylapse reference sky camera, with links and prices, and why each one was chosen over the alternatives.
sidebar:
  order: 1
---

This is the rig the software is developed and tested against, part by part, with the reason each one is there. Nothing on this page is theoretical: every item has been bought, wired and run on a real night. Prices were checked **2026-09-15** on Amazon.com and Harbor Freight, before tax and shipping. Prices move, so re-check before ordering.

| # | Part | What it does | Source | Price |
|---|------|--------------|--------|-------|
| 1 | Raspberry Pi 5 kit, 4GB (board + case + Active Cooler) | Runs everything: capture, the web app, the network watchdog | [RasTech / Amazon B0FNBYQWX3](https://www.amazon.com/dp/B0FNBYQWX3) | $158.99 |
| 2 | Arducam HQ Camera, 12.3MP IMX477 (no-lens variant) | The sensor | [Arducam / Amazon B0D3WYQF2Q](https://www.amazon.com/dp/B0D3WYQF2Q) | $51.99 |
| 3 | 2.5mm F1.2 1/2.5″ CS-mount lens | Wide, fast sky lens for the HQ camera | [Amazon B0C46GP6HV](https://www.amazon.com/dp/B0C46GP6HV) | $12.49 |
| 4 | **Skylapse 12V heater ring** | Keeps the dome above the dewpoint, switched by the software | [Store](/store/) | ~$20 |
| 5 | Weatherproof enclosure | Houses the Pi, camera, supply and electronics | TBD | $20–$50 |
| 6 | Mean Well LRS-50-12 (12V 4.2A) | The one supply for the whole rig | [Amazon B019GYODX0](https://www.amazon.com/dp/B019GYODX0) | $17.99 |
| 7 | Waterproof 12V→5V 5A buck converter, USB-C output (2-pack) | Clean 5V for the Pi from the 12V rail | [Amazon B0FD735LFG](https://www.amazon.com/dp/B0FD735LFG) | $15.99 |
| 8 | ANMBEST dual-MOSFET trigger/PWM switch module, 5–36V 15A (5-pack) | Switches the heater from a Pi GPIO pin | [Amazon B07NWD8W26](https://www.amazon.com/dp/B07NWD8W26) | $7.99 |
| 9 | BME280 3.3V sensor module (2-pack) | Temperature, humidity and pressure: one outside, one inside the dome | [Amazon B0DSVNCVVV](https://www.amazon.com/dp/B0DSVNCVVV) | $12.99 |
| 10 | 25 ft 14/3 indoor/outdoor extension cord | AC power out to the camera | [Harbor Freight item 62920](https://www.harborfreight.com/25-ft-x-143-gauge-indooroutdoor-extension-cord-orange-62920.html) | $14.99 |

**Total: about $333–$363** depending on the enclosure. The multi-packs (buck, MOSFET, BME280) leave you spares.

You also need a **64 GB or larger microSD card**. If you plan to shoot RAW on a schedule, make it a [high-endurance card](https://www.raspberrypi.com/documentation/computers/getting-started.html#recommended-sd-cards), 128 GB or more. Every-frame RAW is about 37 GB a night on this sensor and wants an external SSD instead; see [Storage & RAW](/using/#storage--raw).

## Compute: the Pi 5

A Pi 4 works. The Pi 5 is recommended because the dawn timelapse render and the full-resolution focus assist are both noticeably faster on it, and because its camera stack is what the project is tested on.

:::tip[Skip the kit markup]
The kit (#1) is priced well above the bare board plus the official Active Cooler bought separately from [PiShop](https://www.pishop.us/), [Adafruit](https://www.adafruit.com/) or [CanaKit](https://www.canakit.com/). The bundled ABS case goes unused inside a weatherproof enclosure anyway.
:::

## Camera and lens

**Skylapse is a Raspberry Pi camera project first.** A camera module on the CSI ribbon needs no vendor software, everything in the SD image supports it end to end, and it is what every feature is developed against. The **HQ Camera / IMX477** (#2) is the specific one it is built around. Other Pi-compatible modules (IMX708, IMX219, IMX519, OV5647, IMX296 and the third-party boards built on them) work through the same driver and can be declared from the Camera tab when the Pi cannot see them by itself.

The HQ camera ships bare, and a stock C/CS lens sees a narrow rectangle: fine for a bird box, useless for a sky. The reference rig runs a **2.5mm F1.2 CS-mount CCTV lens** (#3): cheap, wide and fast, and it threads straight onto the HQ camera's C-CS ring. Focus is set once against the live view and locked.

If you want the whole horizon-to-horizon circle, use a **180° fisheye** instead. The development rig has used the [Arducam 180° fisheye M12](https://www.amazon.com/dp/B0897QD6C2). Two things make it the safe pick:

- It is a **1/2.3″ lens**, the HQ camera's own format, so the image circle covers the sensor.
- It **includes the M12→CS adapter**. The HQ camera is C/CS mount and M12 lenses are not, so a bare M12 fisheye will not attach at all. That is the single most common way to buy the wrong thing here.

Narrower fisheyes (Arducam sell 140° and 100°) trade sky for detail, a fair trade if you care more about one part of the sky than all of it.

:::note[ZWO ASI cameras: second, and honestly second]
A ZWO USB camera may work. The driver is verified on exactly one model (an ASI676MC), ZWO's licence forbids shipping their SDK in the image (the Camera tab downloads it on demand, about 4 MB), and new features are not designed against it. If you are buying a camera for this project, buy a Pi one.
:::

## Power: one 12V supply

The rig runs from a single **Mean Well LRS-50-12** (#6). The Pi gets its 5V through a **12V→5V 5A buck converter with a USB-C lead** (#7), and the heater runs from the 12V rail directly.

This is not arbitrary. Running a 5V heater and a Pi 5 from one 5V supply was measured on the reference rig causing repeated undervoltage and hard PMIC latch-offs: the camera dies with a red LED and stays dead until physically unplugged. Splitting the loads ended it. The Pi 5's undervoltage threshold is 4.75V, so whatever feeds it wants headroom above 5.0V and short, thick wiring. Meter the buck's output before you trust it.

```
AC (extension cord)
  │
  ▼
Mean Well LRS-50-12 ── 12V ─┬─► MOSFET module ─► heater ring
                            │        ▲
                            │        └─ Pi GPIO 18 (Skylapse)
                            │
                            └─► 12V→5V buck ─► USB-C ─► Pi 5
                                                          │ CSI
                                                          ▼
                                                    HQ Camera + lens
```

The **extension cord** (#10) is the AC run out to wherever the camera lives. 14-gauge outdoor cord, because it sits outside through every season.

## Dew heater: ring, switch, sensors

A dome pointed at a clear sky radiates heat to space and drops below the air temperature. When it crosses the dewpoint it fogs, and the rest of the night is lost. The reference rig has logged dew arriving twice in one night, between 02:30 and 05:00. A few watts on the dome base fixes it, and three parts let the software run that heat itself.

**The Skylapse 12V heater ring** (#4) is a ring-shaped circuit board whose copper trace *is* the heating element. No components, two solder pads, nothing to fail.

| | |
|---|---|
| Fits | 50 mm centre hole, 85 mm outer: the seat of a standard acrylic dome |
| Element | ~31 Ω, 0.39 A, ~4.2 W warm, from the 12V rail |
| Wiring | two pads, no polarity; 24 AWG is plenty |

It is in the reference rig and verified under software control: a two-minute test took the dome sensor from 18.9 °C to 23.6 °C with the outside sensor unchanged. Any resistive element wires in the same way if you would rather wind your own: power resistors around the dome base (3–5 W at 12V), a USB lens-warmer band, or nichrome.

**The MOSFET module** (#8) is the switch. The Pi's GPIO 18 drives its trigger input, the 12V rail goes in one side and the ring hangs off the other. Its signal ground is common with its power ground, which the single-12V design satisfies automatically. Its terminal labels are on the back of the board, so the [Build page](/build/#3-wire-the-heater-switch) shows every wire landed on a photo of the front.

**Two BME280 sensors** (#9) make the heater a thermostat. One sits outside, in the air, and gives the dewpoint. The second sits inside the dome, with its SDO pad bridged to VCC (one solder joint) so it answers at a second address, and reports the temperature of the surface actually being protected. The software holds the dome a set margin above the dewpoint and caps it at 45 °C. One sensor still works; it just regulates against the air instead of the glass.

:::caution[BME280, not BMP280]
The BMP280 looks identical and costs a dollar less, but it has **no humidity sensor**, and humidity is the whole input to a dewpoint. Skylapse checks the chip id and rejects a BMP280 rather than compute nonsense.
:::

## Enclosure

The reference rig lives in a watertight project box with an acrylic dome on top. The box is still a work in progress, which is why #5 is a range and not a link. The two rules it has to satisfy: the dome seats on the heater ring, and the outside sensor is in free air and shade, not in the box with the supply.

## Optional extras

- **DS3231 RTC module** (~$5). A Pi has no battery-backed clock, so it boots with a stale time until it reaches the network. That matters for a camera that may spend a night on its own access point.
- **External USB SSD**, if you shoot every-frame RAW. Card endurance is the problem, not just capacity.
