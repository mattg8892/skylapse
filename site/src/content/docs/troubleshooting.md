---
title: Troubleshooting
description: The web page won't load, the camera isn't detected, frames look green, capture stopped — what to check, in order.
sidebar:
  order: 1
---

Most of these have been hit on the project's own hardware. The first thing to try is listed first because it is the cheapest, not because it is the most likely.

## The web page doesn't load

Check the services: `systemctl status skylapse-api skylapse-daemon`. Logs are `journalctl -u skylapse-daemon -f`, which prints one line per captured frame.

If you never enabled SSH, the camera's own Wi-Fi is still there: if it can't reach your network it serves **`Skylapse-Setup`** — join it and open `http://10.42.0.1`. See [Wi-Fi & access point](/network/).

## "No camera detected" with a Pi camera module

That is usually not a fault. Raspberry Pi OS identifies cameras by reading a chip that many third-party boards — including most HQ/IMX477 clones — simply do not have.

1. On the **Camera tab**, open **"My camera isn't being detected"**, pick your sensor, and tap **Enable and restart**.
2. **Then pull the power for 15 seconds and plug it back in.** Restarting applies the setting, but it does not cut power to the sensor, and most camera boards will not come up until it has genuinely been off. On this project's own hardware the declare → restart → *power cycle* sequence works every time, and stopping after the restart does not.

## A Pi camera module doesn't enumerate at all

Check `rpicam-hello --list-cameras`. If it says `No cameras available!`:

1. **Fully power off** — pull the plug for 15 seconds. A warm reboot does not drain the sensor's regulator, and some modules only come up after a cold start. This is first because it is the cheapest thing to try and, on this project's own hardware, it was twice the answer to a camera that looked broken — including once immediately after writing a fresh card.
2. **Reseat the ribbon** at both ends, contacts the right way round.
3. If it still fails, `sudo dmesg | grep imx477` tells you which: no lines at all means the overlay isn't loading; `failed to read chip id` means the sensor isn't answering, which is then genuinely a cable or module fault.

## "No camera detected" with a ZWO attached

First check the **Camera tab** says ZWO support is installed; if it doesn't, install it there. If it does and the camera is still missing, it is power or the model: a USB3 camera on a non-official supply enumerates intermittently or not at all, and models other than the ASI676MC are not verified and may simply not open. From a terminal, `lsusb` shows whether the camera is on the bus at all and `journalctl -u skylapse-daemon` says how far the open got.

## Frames look green

Both sensor families read green high — two of every four photosites are green, and green is the most sensitive. The Camera tab has red and blue sliders per camera, with an "Auto from current frame" button to start from and a live preview. Set it once per camera; RAW/DNG pixels are never changed, and the multipliers are recorded in the file for your raw editor to apply.

## Capture stopped and you weren't told

Turn on [phone alerts](/phone-alerts/). The watchdog notices when frames stop arriving, not merely when the process dies.

## The Pi dies with a red LED and stays dead until unplugged

That is the Pi 5's PMIC latching off on undervoltage. On the reference rig it was caused by running a 5V dew heater and the Pi from one 5V supply. Put the heater on its own 12V rail — see [why the power is built this way](/hardware/#why-this-power-architecture) — and make sure whatever feeds the Pi has headroom above 5.0V and short, thick wiring.

## The dome fogs over halfway through the night

Dew. Fit a heater and let Skylapse run it from a BME280 — see [Dew heater](/dew-heater/).

## Two cameras, one keeps answering for the other

Two `skylapse.local` on one network resolve to whichever answers first. Give the second camera a different hostname when you write its card.

## Still stuck

[Open an issue](https://github.com/mattg8892/skylapse/issues) with the output of `journalctl -u skylapse-daemon -n 200` and which camera and Pi you have.
