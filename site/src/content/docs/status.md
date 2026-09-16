---
title: Status & roadmap
description: What is verified working on real hardware, what is written but not yet proven, and what is deliberately out of scope.
sidebar:
  order: 2
---

The rule on this project: **only claim what has been witnessed working on real hardware.** If a feature is on this page as working, it was run on a Pi with a real camera, most of it the hard way.

## Working and verified on hardware

- The flashable SD image and first-run setup on a phone
- Capture, auto-exposure, white balance, hot-pixel correction, DNG
- Dawn timelapses, the nights browser, focus assist, USB export
- Phone alerts over ntfy
- An optional password
- Self-updating with rollback
- Wi-Fi fallback and access-point mode — including the guard that refuses to drop the access point while a phone is connected to it, which was tested with a real phone because there is no other way to test it
- Dew heater control from a BME280, and the manual heater switch (v0.5.27+)

The whole path on [Getting started](/getting-started/) — write a card, power it up, join the camera's own Wi-Fi, finish setup on a phone, and have it capturing — has been done end to end on a Pi 5 with an IMX477, with no terminal at any point.

## Not there yet

- **No captive portal.** Joining the camera's own network works, but you have to type `10.42.0.1` yourself — it will not pop up a sign-in page the way a hotel network does.
- **No remote access yet.** Viewing the camera from outside your own network — over [Tailscale](https://tailscale.com), your account, nothing hosted by us — is written and does not work on real hardware yet. The settings card says so rather than offering a button that fails. On your own network everything works today, and the camera has never needed the internet to run.
- **ZWO support is best effort.** It is verified on one model (ASI676MC), its SDK has to be downloaded on demand because it cannot be redistributed, and it may not work with your camera at all. Pi camera modules are the supported path — see [Cameras](/cameras/).
- **No white balance for mono sensors**, and no colour management beyond the per-camera multipliers.
- **External image store on a USB SSD** is specified as a guided feature but not built; today `SKYLAPSE_IMAGES` relocates the store by hand.
- **The Skylapse dew heater ring** is coming — see [Dew heater](/dew-heater/).

## Designed for, not built

The image store is append-only with a JSON sidecar per frame precisely so these can be added later without touching capture: keograms, startrails, meteor detection, multi-camera, cloud upload.

## Deliberately out of scope

A plugin system, image stacking, ML cloud classification, satellite ID overlays, MQTT/Home Assistant, FTP/website publishing. The identity is lighter and more opinionated than the incumbents.

Issues and pull requests welcome — [github.com/mattg8892/skylapse](https://github.com/mattg8892/skylapse).
