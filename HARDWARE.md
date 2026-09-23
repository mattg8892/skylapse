# Skylapse — reference build parts list

Every part in (or headed into) the reference allsky rig this software is
developed against. Prices checked **2026-09-15** on Amazon.com / Harbor
Freight, before tax and shipping — prices move, re-check before ordering.

| # | Part | Role in the build | Source | Price |
|---|------|-------------------|--------|-------|
| 1 | Raspberry Pi 5 kit, 4GB (board + case + Active Cooler) | Compute — runs Skylapse (capture daemon, API, watchdog) | [RasTech / Amazon B0FNBYQWX3](https://www.amazon.com/dp/B0FNBYQWX3) | $158.99 |
| 2 | Arducam HQ Camera, 12.3MP IMX477 (no-lens variant) | Camera sensor — Skylapse's Pi/CSI driver | [Arducam / Amazon B0D3WYQF2Q](https://www.amazon.com/dp/B0D3WYQF2Q) | $51.99 |
| 3 | 2.5mm F1.2 1/2.5″ CS-mount lens | Wide-angle allsky lens for the HQ camera | [Amazon B0C46GP6HV](https://www.amazon.com/dp/B0C46GP6HV) | $12.49 |
| 4 | **Skylapse 12V PCB dew heater ring** *(coming soon)* | Dew heater around the dome — software-controlled via the MOSFET module below | Skylapse custom PCB | ~$20 (est.) |
| 5 | Weatherproof enclosure | Housing for Pi, camera, PSU and electronics | TBD | $20–$50 |
| 6 | Mean Well LRS-50-12 (12V 4.2A) | 12V main power supply | [Amazon B019GYODX0](https://www.amazon.com/dp/B019GYODX0) | $17.99 |
| 7 | Waterproof 12V→5V 5A buck converter, USB-C output (2-pack) | Clean 5V for the Pi from the 12V rail | [Amazon B0FD735LFG](https://www.amazon.com/dp/B0FD735LFG) | $15.99 |
| 8 | ANMBEST dual-MOSFET trigger/PWM switch module, 5–36V 15A (5-pack) | Switches the dew heater from a Pi GPIO — straight-through screw-terminal wiring | [Amazon B07NWD8W26](https://www.amazon.com/dp/B07NWD8W26) | $7.99 |
| 9 | BME280 3.3V sensor module (2-pack) | Temperature / humidity / pressure — drives the automatic dew heater | [Amazon B0DSVNCVVV](https://www.amazon.com/dp/B0DSVNCVVV) | $12.99 |
| 10 | 25 ft 14/3 indoor/outdoor extension cord | AC power run to the camera | [Harbor Freight item 62920](https://www.harborfreight.com/) | $14.99 |

**Total: ~$333–$363** depending on enclosure choice. Multi-packs (buck,
MOSFET, BME280) include spares.

## Wiring

The complete hookup — every conductor, numbered — is in
[docs/wiring-12v.svg](docs/wiring-12v.svg): one 12V supply feeding the Pi
through the buck and the dew heater through the MOSFET module, plus both
BME280 sensors (outside at 0x76, inside the dome at 0x77 via its SDO
solder bridge) sharing the I²C bus. Dots are joins; crossings without dots
are not connections.

## Why this power architecture

The 12V-rail design (one 12V supply → buck for the Pi, direct for the
heater) is not arbitrary: running a 5V heater and a Pi 5 from one 5V supply
was measured on the reference rig causing repeated undervoltage and hard
PMIC latch-offs — the camera dies with a red LED and stays dead until
physically unplugged. Splitting the loads ends that. The heater card in
Settings carries the same warning. The Pi 5's undervoltage threshold is
4.75V; whatever feeds it wants headroom above 5.0V and short, thick wiring.

## Notes per part

- **Pi 5 kit (#1)**: kit price is well above board + official cooler bought
  separately (PiShop/Adafruit/CanaKit) — price-check before buying.
- **Lens (#3)**: fixed-focus CCTV lens; focus is set once against a live
  view (Settings → focus assist) and locked.
- **Heater ring (#4)**: custom PCB, 5V and 12V variants designed and at the
  fab; ordering and availability details coming.
- **MOSFET module (#8)**: signal ground is common with power ground on the
  board; with the single-12V architecture that is automatically satisfied.
- **BME280 (#9)**: must be a BME280, not a BMP280 — the BMP cannot measure
  humidity, and humidity is the whole input to a dewpoint. Skylapse rejects
  BMP280s by chip id rather than compute nonsense.
- The dew heater also runs without any sensor at all — manual on/off switch
  in Settings (v0.5.27+).
