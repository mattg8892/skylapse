---
title: Design notes
description: The goals that govern every decision in Skylapse, and where to read the full design document.
sidebar:
  order: 2
---

The full design document — 800+ lines, including what each camera taught us on first contact, the auto-exposure model, the network state machine and the roadmap — is [DESIGN.md in the repo](https://github.com/mattg8892/skylapse/blob/main/DESIGN.md). This page is the short version: the goals, because they decide everything else.

## Goals

0. **Free forever, for everyone.** The hard constraint governing all decisions: no cost to the maintainer, no cost to the user, ever. Consequences: no native app-store apps (a PWA instead — no $99/yr Apple fee), no cloud relay or hosted service of any kind (everything runs on the user's Pi; remote access via the user's own free Tailscale/WireGuard), free-tier-only infrastructure (GitHub public repo + Actions + Releases, PyPI). Nothing in the project may create a recurring bill for anyone, and nothing may depend on a backend that could be shut down.
1. **Zero-code setup.** Flash SD image, boot, join hotspot, finish a wizard on your phone. No SSH, no config files, no terminal — ever.
2. **Raspberry Pi cameras first.** Pi camera modules over CSI — the HQ/IMX477 in particular — are the primary target: no vendor library, supportable end to end from the SD image, and what every feature is developed against. ZWO ASI (USB) ships behind the same driver interface as a best-effort second, verified on one model and not guaranteed to work on others. (This was the other way round until 2026-08-17.)
3. **JPEG + RAW (DNG).** Full-res JPEG every frame for timelapse; DNG on demand, on schedule, or triggered — for editing keepers in Lightroom, Siril or PixInsight.
4. **Capture never depends on network.** Wi-Fi can flap, the hotspot can cycle, the imaging daemon keeps writing frames to the SD card no matter what.
5. **Field-ready.** Full standalone (no internet) operation with browser-based time sync.

## Architecture in one picture

```
skylapse-daemon     capture loop, drivers, image pipeline, scheduler
skylapse-api        FastAPI: REST + WebSocket, serves the React frontend
skylapse-netwatch   network state machine over NetworkManager (D-Bus)
```

Shared state, and nothing else:

- `/etc/skylapse/config.yaml` — single config file, atomic writes (tmp + fsync + rename)
- `/var/lib/skylapse/images/<camera>/<date>/` — image store, JSON sidecar per frame
- `/run/skylapse/` — runtime status files (daemon heartbeat, netwatch state, latest frame path)

## Updates never touch the OS

Skylapse updates itself from its own git checkout — never the OS: no apt, no firmware, no other packages. A camera that reboots into a broken userland because its imaging app decided to upgrade the system is worse than one running last month's build. The `release` channel polls GitHub releases once a day (unauthenticated, a public repo needs no token); `dev` follows `origin/main` for development units. If the new version doesn't come up healthy, it rolls back.

## Clean-room, MIT

MIT licensed. A clean-room implementation — no code from existing GPL sky-camera projects.
