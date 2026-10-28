---
title: Help & status
description: What to check when something is wrong, in order; what is verified on real hardware; what is not there yet; and where developers start.
sidebar:
  order: 4
---

Most of these have been hit on the project's own hardware. The first thing to try is listed first because it is the cheapest, not because it is the most likely.

## The web page doesn't load

If the camera can't reach your network it serves its own: join **`Skylapse-Setup`** and open `http://10.42.0.1`, typed with `http://`, with mobile data off if your phone keeps routing around a network with no internet. If the camera *has* joined your Wi-Fi, the setup network is gone by design and it answers at `http://skylapse.local`, or at whatever address your router gave it.

From a terminal, if you have one: `systemctl status skylapse-api skylapse-daemon`, and `journalctl -u skylapse-daemon -f` prints one line per captured frame. Logs are also readable from Settings without any terminal.

## "No camera detected" with a Pi camera module

Usually not a fault. Raspberry Pi OS identifies cameras by reading a chip that many third-party boards, including most HQ/IMX477 clones, do not have.

1. On the **Camera tab**, open **"My camera isn't being detected"**, pick your sensor, and tap **Enable and restart**.
2. **Then pull the power for 15 seconds and plug it back in.** Restarting applies the setting but does not cut power to the sensor, and most boards will not come up until it has genuinely been off.

If it still doesn't enumerate: a cold power-off first, then reseat the ribbon at both ends. `sudo dmesg | grep imx477` from a terminal tells you which it is: no lines means the overlay isn't loading, `failed to read chip id` means the sensor isn't answering and it is genuinely a cable or module fault.

## "No camera detected" with a ZWO attached

Check the Camera tab says ZWO support is installed. If it does and the camera is still missing, it is power or the model: a USB3 camera on a weak supply enumerates intermittently or not at all, and models other than the ASI676MC are not verified.

## The heater says "no BME280 detected"

On a fresh card that is the I²C bus being off, which is how Pi OS ships. Tap **Enable I²C** on the Heater tab and reboot once; see [the build step](/build/#7-switch-the-heater-on). If it still says so after the reboot, it is wiring, or the boards are BMP280s, which have no humidity sensor and are rejected on purpose.

## Frames look green

Both sensor families read green high. The Camera tab has red and blue sliders per camera with an "Auto from current frame" button. Set it once; RAW pixels are never changed.

## Capture stopped and you weren't told

Turn on [phone alerts](/using/#phone-alerts). The watchdog notices when frames stop arriving, not merely when the process dies.

## The Pi dies with a red LED and stays dead until unplugged

The Pi 5's PMIC latching off on undervoltage. On the reference rig it was caused by running a 5V heater and the Pi from one 5V supply. Put the heater on its own 12V rail, which is [how the reference rig is wired](/parts/#power-one-12v-supply), and make sure whatever feeds the Pi reads above 5.0V with short, thick wiring.

## The dome fogs over halfway through the night

Dew. Fit the heater and let Skylapse run it; see [the build](/build/#7-switch-the-heater-on).

## Still stuck

[Open an issue](https://github.com/mattg8892/skylapse/issues) with the daemon log from Settings and which camera and Pi you have.

## What is verified on hardware

The rule on this project: **only claim what has been witnessed working on real hardware.** Everything below has been run on a Pi 5 with a real camera.

- The flashable SD image and first-run setup on a phone, end to end, with no terminal at any point
- Capture, auto-exposure, white balance, hot-pixel correction, DNG
- Dawn timelapses, the nights browser, focus assist, USB export
- Phone alerts over ntfy, an optional password, self-updating with rollback
- Wi-Fi fallback and access-point mode, including the guard that refuses to drop the access point while a phone is connected to it
- Remote access over Tailscale from the Network tab (v0.5.29+)
- Dew heater control from a BME280, the manual switch (v0.5.27+), the two-sensor dome thermostat (v0.5.38+) and the Skylapse 12V heater ring, run together in the reference rig

## Not there yet

- **No captive portal.** You have to type `10.42.0.1` yourself.
- **The heater ring is not listed for sale yet.** The board is real and running; the [Store](/store/) goes live once the listings exist.
- **ZWO support is best effort**, verified on one model only.
- **No white balance for mono sensors**, and no colour management beyond the per-camera multipliers.
- **External image store on a USB SSD** is specified but not built as a guided feature.

Designed for but not built, with the image store laid out so they can be added without touching capture: keograms, startrails, meteor detection, multi-camera, cloud upload. Deliberately out of scope: a plugin system, image stacking, ML cloud classification, satellite overlays, MQTT/Home Assistant, FTP publishing.

## For developers

Skylapse is MIT licensed and a clean-room implementation. It runs without a camera: a simulator with a synthetic star field responds to exposure and gain, so auto-exposure behaves as it does on real hardware.

```bash
git clone https://github.com/mattg8892/skylapse && cd skylapse
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pytest
SKYLAPSE_SIM=1 SKYLAPSE_CONFIG=./dev/config.yaml SKYLAPSE_IMAGES=./dev/images SKYLAPSE_RUN=./dev/run skylapse-daemon
```

To install onto a Pi you already have instead of flashing the image, clone the repo onto it and run `sudo ./install.sh`. It builds the frontend and enables the three services, running in place from the checkout. Three independent services, capture, the web API and the network watchdog, share only a config file and a few status files, so a bug in one cannot take down the others.

The full design document, 800 lines including what each camera taught us on first contact, the auto-exposure model and the network state machine, is [DESIGN.md in the repo](https://github.com/mattg8892/skylapse/blob/main/DESIGN.md). The [README](https://github.com/mattg8892/skylapse#readme) covers the environment variables and the rest. This site lives in `site/` in the same repo; every page has an **Edit page** link at the bottom.
