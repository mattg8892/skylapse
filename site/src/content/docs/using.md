---
title: The screens
description: What each tab in Skylapse does — dashboard, nights, camera, heater, network and settings.
sidebar:
  order: 1
---

Skylapse is a web app served by the camera itself. Open it on a phone, tablet or desktop at `http://skylapse.local` (or `http://10.42.0.1` on the camera's own Wi-Fi). It installs as a PWA if you want an icon on your home screen.

## Dashboard

The latest frame, a status pill driven by whether frames are actually arriving (not by what the daemon claims), a countdown to the next one, storage, and the **Save RAW** button — the keeper button for when a meteor just went past.

Focus assist starts from here too.

## Nights

Every night captured, with frame counts and sizes. Open one to:

- **scrub the filmstrip** frame by frame, with a star-count chart to find the clear stretch
- **jump between frames that have RAW files**
- **watch the timelapse** rendered at dawn
- **download any frame** as JPEG or DNG
- **export to USB** — copy the night to a stick, with a config backup alongside it and integrity checks on the copy

Skylapse deletes the oldest nights automatically when free space runs low, frames first and timelapses last. See [Storage & RAW](/storage-and-raw/).

## Focus assist

Start it from the dashboard, then zoom to 8× and turn the ring until the sharpness number peaks. The live view zooms 1×–10× into the full-resolution sensor image. Nothing is written to the card while focusing, and it exits by itself after 15 minutes.

## Camera

Everything about the imaging itself, one tab per thing you tune:

- **Capture schedule** — 24/7 or night-only.
- **Exposure profiles** — day / night / twilight, chosen from sun altitude, plus a manual mode for star-tracker rigs.
- **RAW policy** — off, on demand, on a schedule, or every frame (read [Storage & RAW](/storage-and-raw/) first).
- **Timelapse options** — length and quality for the dawn render, with one-off overrides.
- **White balance** — red and blue sliders per camera with "Auto from current frame".
- **Hardware** — declare a board the Pi cannot detect, add a ZWO camera, install ZWO support.

## Heater

The dew heater's own tab: automatic from the BME280 dewpoint, or a manual on/off switch that needs no sensor at all, plus a timed test pulse for commissioning the wiring. See [Dew heater](/dew-heater/) — including the power-supply warning, which is there for measured reasons.

## Network

Wi-Fi and reaching the camera, together:

- **Wi-Fi & access point** — switch to access-point mode by hand, with timed options. See [Wi-Fi & access point](/network/).
- **Remote access** — view the camera from anywhere through your own free Tailscale account: one-tap install, QR-code sign-in, and a permanent `https://…ts.net` address. The camera is never exposed to the open internet.

## Settings

What is set once and rarely revisited:

- **Notifications** — phone alerts over ntfy. See [Phone alerts](/phone-alerts/).
- **Config backup**, an optional **password**, **logs** readable from the page, a clean **restart** button, and **updates** with automatic rollback if the new version doesn't come up healthy.

## Updates

Settings checks for a new release and installs it in place. If the new version does not come up healthy, Skylapse rolls back to the previous one by itself. Installing from a checkout instead of the image? Then updating is `git pull && sudo systemctl restart skylapse-daemon` — see [Development](/development/).
