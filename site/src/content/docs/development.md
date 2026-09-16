---
title: Development
description: Run Skylapse without a camera using the simulator, install onto a Pi you already have, and how the three services fit together.
sidebar:
  order: 1
---

No camera required — there is a simulator with a synthetic star field whose brightness responds to exposure and gain, so auto-exposure behaves as it does on real hardware.

```bash
git clone https://github.com/mattg8892/skylapse && cd skylapse
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"          # needs a C compiler: pidng ships as source
pytest

SKYLAPSE_SIM=1 \
SKYLAPSE_CONFIG=./dev/config.yaml \
SKYLAPSE_IMAGES=./dev/images \
SKYLAPSE_RUN=./dev/run \
  skylapse-daemon
```

The web interface builds with `npm --prefix web install && npm --prefix web run build`.

## Installing onto a Pi you already have

Rather than flashing the image: clone the repo onto the Pi and run `sudo ./install.sh`. It installs system packages, builds the frontend and enables the three services, running Skylapse in place from the checkout — so updating is `git pull && sudo systemctl restart skylapse-daemon`. It is safe to re-run.

The SD image is this same script, run inside the image at build time (`image/build.sh`).

## Environment variables

Three variables relocate everything:

| Variable | Default on a rig | What it is |
|---|---|---|
| `SKYLAPSE_CONFIG` | `/etc/skylapse/config.yaml` | The single config file (atomic writes: tmp + fsync + rename) |
| `SKYLAPSE_IMAGES` | `/var/lib/skylapse/images/` | The image store, one folder per night, JSON sidecar per frame |
| `SKYLAPSE_RUN` | `/run/skylapse/` | Runtime status files: daemon heartbeat, netwatch state, latest frame path |

`SKYLAPSE_SIM=1` swaps the camera for the simulator.

## Architecture

Three independent systemd services. A bug in one can never take down the others.

| Service | Job |
|---|---|
| `skylapse-daemon` | Capture loop, camera drivers, JPEG/DNG pipeline, scheduling |
| `skylapse-api` | REST API and web interface (FastAPI + React) |
| `skylapse-netwatch` | Wi-Fi/access-point state machine over NetworkManager, and the fallback when Wi-Fi is gone |

They share only the config file and the status files, so none can take another down. [Design notes](/design/) has the full reasoning, including what each camera taught us on first contact.

## Tests

`pytest` runs the suite; the hardware touch-points are behind driver interfaces so almost everything runs against the simulator. Anything that claims to work on this site has additionally been run on a real Pi — see [Status](/status/).

## This site

The docs live in `site/` in the same repo, built with [Astro Starlight](https://starlight.astro.build). Every page has an **Edit page** link at the bottom. To run it locally:

```bash
cd site && npm install && npm run dev
```
