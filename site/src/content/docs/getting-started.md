---
title: Getting started
description: Write the SD card, power up the Pi, and finish setup from your phone. No terminal at any point.
sidebar:
  order: 1
---

The whole install is: write a card, power up, open a web page on your phone. Nothing here needs a keyboard plugged into the Pi.

## What you need

| | |
|---|---|
| **Raspberry Pi** | Pi 5 recommended, Pi 4 works. |
| **Power supply** | The official 5V/5A (Pi 5) or 5V/3A (Pi 4). Underpowering a USB3 camera shows up as mysterious disconnects mid-night, not as an obvious power error. |
| **Storage** | 64 GB+ microSD. A [high-endurance card](https://www.raspberrypi.com/documentation/computers/getting-started.html#recommended-sd-cards) if you plan to shoot RAW — see [Storage & RAW](/storage-and-raw/). |
| **Camera** | A Pi camera module. The [HQ Camera](https://www.raspberrypi.com/products/raspberry-pi-high-quality-camera/) / IMX477 is the recommended one and what Skylapse is developed against. A ZWO ASI USB camera may also work; see [Cameras](/cameras/). |
| **Lens** | A fisheye, or you are photographing a rectangle of sky rather than the sky. See [Cameras & lenses](/cameras/#the-lens-matters-as-much-as-the-camera). |
| **Optional** | A DS3231 RTC module (~$5) — a Pi has no battery-backed clock, so it boots with a stale time until it reaches the network. |

Weatherproof housing and dew heater are covered on the [Hardware](/hardware/) page. The complete reference build, with prices, is there too.

## 1. Write the card

1. Download **`skylapse.img.xz`** from the [latest release](https://github.com/mattg8892/skylapse/releases/latest).
2. Open [Raspberry Pi Imager](https://www.raspberrypi.com/software/), choose **Use custom**, and select the file you downloaded.
3. Open Imager's **customisation** settings (the gear icon) before writing:
   - enter your **Wi-Fi** credentials and country
   - set a **hostname** if you like — the image already answers to `skylapse`, but give a second camera a different name: two `skylapse.local` on one network resolve to whichever answers first
   - **enable SSH** if you ever want a terminal. You do not need it.
4. Write the card, put it in the Pi, and power up.

The first boot expands the filesystem and takes a minute or two longer than later ones.

## 2. Open it

**If you entered Wi-Fi details**, the camera joins your network:

```
http://skylapse.local
```

**If you didn't**, the camera serves its own network instead. Join **`Skylapse-Setup`** from your phone's Wi-Fi settings — it is open, no password — and go to:

```
http://10.42.0.1
```

Either way, setup runs on the first visit: network, camera with a live test shot, where the camera is, what to capture, and optionally a password. A couple of minutes on a phone, and every answer can be changed later from the tabs across the top.

That is the whole install. No terminal, no config files, no account, nothing in the cloud.

:::caution[If it says "No camera detected"]
That is usually not a fault. Raspberry Pi OS identifies cameras by reading a chip that many third-party boards — including most HQ/IMX477 clones — simply do not have. On the **Camera tab**, open **"My camera isn't being detected"**, pick your sensor, and tap **Enable and restart**. No terminal needed.

**Then pull the power for 15 seconds and plug it back in.** This is the step people miss. Restarting applies the setting, but it does not cut power to the sensor, and most camera boards will not come up until it has genuinely been off. On this project's own hardware the declare → restart → *power cycle* sequence works every time, and stopping after the restart does not. The page tells you when, and picks up by itself afterwards.
:::

## 3. Point it at the sky

Start **focus assist** from the dashboard, zoom to 8×, and turn the lens ring until the sharpness number peaks. Nothing is written to the card while focusing, and it exits by itself after 15 minutes. Then leave it: capture follows the schedule you set, and a dawn timelapse renders itself every morning.

Next: [the screens](/using/), or [turn on phone alerts](/phone-alerts/) so you know if it ever stops.
