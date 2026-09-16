---
title: Hardware & parts list
description: Every part in the Skylapse reference sky-camera rig, with links and prices, and why the power is built the way it is.
sidebar:
  order: 2
---

Every part in (or headed into) the reference sky-camera rig this software is developed against. Prices checked **2026-09-15** on Amazon.com / Harbor Freight, before tax and shipping — prices move, re-check before ordering.

| # | Part | Role in the build | Source | Price |
|---|------|-------------------|--------|-------|
| 1 | Raspberry Pi 5 kit, 4GB (board + case + Active Cooler) | Compute — runs Skylapse (capture daemon, API, watchdog) | [RasTech / Amazon B0FNBYQWX3](https://www.amazon.com/dp/B0FNBYQWX3) | $158.99 |
| 2 | Arducam HQ Camera, 12.3MP IMX477 (no-lens variant) | Camera sensor — Skylapse's Pi/CSI driver | [Arducam / Amazon B0D3WYQF2Q](https://www.amazon.com/dp/B0D3WYQF2Q) | $51.99 |
| 3 | 2.5mm F1.2 1/2.5″ CS-mount lens | Wide-angle sky lens for the HQ camera | [Amazon B0C46GP6HV](https://www.amazon.com/dp/B0C46GP6HV) | $12.49 |
| 4 | Dew heater — **Skylapse 12V heater ring** <span class="pill">coming soon</span> | Heater around the dome — software-controlled via the MOSFET module below. Any resistive element works today | [Dew heater](/dew-heater/) | ~$20 (est.) |
| 5 | Weatherproof enclosure | Housing for Pi, camera, PSU and electronics | TBD | $20–$50 |
| 6 | Mean Well LRS-50-12 (12V 4.2A) | 12V main power supply | [Amazon B019GYODX0](https://www.amazon.com/dp/B019GYODX0) | $17.99 |
| 7 | Waterproof 12V→5V 5A buck converter, USB-C output (2-pack) | Clean 5V for the Pi from the 12V rail | [Amazon B0FD735LFG](https://www.amazon.com/dp/B0FD735LFG) | $15.99 |
| 8 | ANMBEST dual-MOSFET trigger/PWM switch module, 5–36V 15A (5-pack) | Switches the dew heater from a Pi GPIO — straight-through screw-terminal wiring | [Amazon B07NWD8W26](https://www.amazon.com/dp/B07NWD8W26) | $7.99 |
| 9 | BME280 3.3V sensor module (2-pack) | Temperature / humidity / pressure — drives the automatic dew heater | [Amazon B0DSVNCVVV](https://www.amazon.com/dp/B0DSVNCVVV) | $12.99 |
| 10 | 25 ft 14/3 indoor/outdoor extension cord | AC power run to the camera | [Harbor Freight item 62920](https://www.harborfreight.com/25-ft-x-143-gauge-indooroutdoor-extension-cord-orange-62920.html) | $14.99 |

**Total: ~$333–$363** depending on enclosure choice. Multi-packs (buck, MOSFET, BME280) include spares.

:::tip[Skip the kit markup]
The Pi 5 kit (#1) is priced well above the board plus the official Active Cooler bought separately from [PiShop](https://www.pishop.us/), [Adafruit](https://www.adafruit.com/) or [CanaKit](https://www.canakit.com/). The bundled ABS case is not used inside a weatherproof enclosure anyway. Price-check before buying.
:::

## Why this power architecture

The 12V-rail design — one 12V supply, a buck converter for the Pi, 12V direct for the heater — is not arbitrary. Running a 5V heater and a Pi 5 from one 5V supply was measured on the reference rig causing repeated undervoltage and hard PMIC latch-offs: the camera dies with a red LED and stays dead until physically unplugged. Splitting the loads ends that. The Heater tab carries the same warning.

The Pi 5's undervoltage threshold is 4.75V; whatever feeds it wants headroom above 5.0V and short, thick wiring.

```
AC (extension cord)
  │
  ▼
Mean Well LRS-50-12 ── 12V ─┬─► MOSFET module ─► heater ring
                            │        ▲
                            │        └─ Pi GPIO (Skylapse)
                            │
                            └─► 12V→5V buck ─► USB-C ─► Pi 5
                                                          │ CSI
                                                          ▼
                                                    HQ Camera + lens
```

## Notes per part

- **Pi 5 kit (#1)** — see the tip above; you are paying for the case.
- **Lens (#3)** — fixed-focus CCTV lens; focus is set once against a live view (focus assist on the dashboard) and locked. For a true 180° view see the fisheye discussion on [Cameras & lenses](/cameras/#the-lens-matters-as-much-as-the-camera).
- **Heater ring (#4)** — a Skylapse-made 12V ring is coming; until then wire any resistive element as shown on the [Dew heater](/dew-heater/) page.
- **MOSFET module (#8)** — signal ground is common with power ground on the board; with the single-12V architecture that is automatically satisfied.
- **BME280 (#9)** — must be a BME280, **not** a BMP280. The BMP cannot measure humidity, and humidity is the whole input to a dewpoint. Skylapse rejects BMP280s by chip id rather than compute nonsense.
- The dew heater also runs without any sensor at all — manual on/off switch on the Heater tab (v0.5.27+).

## Optional extras

- **DS3231 RTC module** (~$5) — a Pi has no battery-backed clock. Without one, it boots with a stale time until it reaches the network, which matters for a camera that may spend a night on its own access point.
- **External USB SSD** — recommended if you shoot every-frame RAW. See [Storage & RAW](/storage-and-raw/).
