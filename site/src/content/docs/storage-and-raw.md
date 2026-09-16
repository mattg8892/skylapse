---
title: Storage & RAW
description: What every-frame RAW costs in space and card wear, and how Skylapse manages the image store.
sidebar:
  order: 2
---

Every-frame RAW is expensive: measured on a 12 MP camera it is **~37 GB per night**. A 256 GB card holds about a week. The settings screen shows your rig's own figure before you commit to it, and warns about sustained write wear — a microSD card can fail suddenly after months of that, having given no warning.

## Recommendations

| You want | Use |
|---|---|
| JPEG timelapses, RAW for keepers only | Any 64 GB+ microSD. Tap **Save RAW** on the dashboard when something happens. |
| RAW on a schedule (every N minutes) | A [high-endurance microSD](https://www.raspberrypi.com/documentation/computers/getting-started.html#recommended-sd-cards), 128 GB+. |
| Every-frame RAW | An external USB SSD. Card endurance is the problem, not just capacity. |

## How the image store works

- Frames land in `/var/lib/skylapse/images/<camera>/<date>/`, one night per folder, with the day rolling over at **local noon** so a whole night stays together.
- Every frame has a JSON sidecar next to it with its capture metadata — what the nights browser charts and what a future keogram/startrail tool would read.
- Skylapse **deletes the oldest nights automatically** when free space runs low: frames first, timelapses last, so a full card never stops capture.
- **DNG pixels are never changed.** White-balance multipliers are recorded in the file for your raw editor (Lightroom, Siril, PixInsight) to apply.

## Getting nights off the camera

- **Download** any frame or timelapse from the nights browser.
- **USB export** — plug a stick into the Pi, open the night, tap export. The copy is verified, and a config backup goes alongside it.
- **External image store** on a permanently attached USB SSD is the intended setup for every-frame RAW. It is specified but not built yet as a guided feature — see [Status & roadmap](/status/). Today the `SKYLAPSE_IMAGES` variable relocates the store (see [Development](/development/#environment-variables)).
