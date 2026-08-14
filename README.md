# Skylapse

**Allsky camera software that anyone can set up.** Point a camera at the sky, flash an SD
card, and manage everything from your phone — no SSH, no config files, no terminal.

- **ZWO ASI + Raspberry Pi cameras** behind one driver interface
- **Full-res JPEG every frame** for timelapses, **DNG raw** on demand or on schedule for
  editing the keepers in Lightroom, Siril, or PixInsight
- **Setup wizard over a Wi-Fi hotspot** — like setting up a smart plug
- **True standalone mode** for dark-site field use with no internet, including
  browser-based time sync
- **Capture never stops.** Networking, the web UI, and the imaging loop are separate
  services; Wi-Fi problems can't cost you a night of sky.

## Status

Early scaffold — architecture and the network state machine are designed, tested, and
documented ([DESIGN.md](DESIGN.md)); drivers and services are wired but not yet
field-hardened. Not ready for production rigs yet. Watch/star for progress.

## Quick start (development)

```bash
git clone https://github.com/mattg8892/skylapse
cd skylapse
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev,zwo]"
pytest                       # state machine guard tests
SKYLAPSE_CONFIG=./dev-config.yaml uvicorn skylapse.api.main:app --reload
```

On a Pi with a ZWO camera attached, `skylapse-daemon` starts capturing immediately;
the web UI lives at `http://skylapse.local`.

**No camera? Develop against the simulator** — a synthetic star field (with a drifting
satellite streak) whose brightness responds to exposure and gain, so the auto-exposure
loop behaves like it does on real hardware:

```bash
SKYLAPSE_SIM=1 skylapse-daemon
```

## Updating

Skylapse updates itself. Settings → Updates shows the running version, offers
whatever the latest release is, and applies it with one button — defaulting to
the next daytime window so a restart never interrupts a clear night. If the
camera doesn't come back healthy within a minute, the previous version is
restored automatically.

It updates **Skylapse only** — never the OS, apt packages, or firmware.

Development units can follow `main` instead of tagged releases via the
development-channel toggle on the same card.

## Architecture

Three independent systemd services sharing a config file and status files — no direct
coupling, so a bug in one can never take down another:

| Service | Job |
|---|---|
| `skylapse-daemon` | Capture loop, camera drivers, JPEG/DNG pipeline, day/night scheduling |
| `skylapse-api` | REST API + web UI (FastAPI + React) |
| `skylapse-netwatch` | Network state machine: Wi-Fi ↔ hotspot ↔ standalone, with loop guards |

Full details, including the network state machine and its six anti-loop guards, are in
[DESIGN.md](DESIGN.md).

## Hardware notes

- Pi 4/5 recommended. USB3 ZWO cameras on long exposures want a solid 5V/3A+ supply —
  brownouts show up as mysterious USB disconnects.
- A DS3231 RTC module (~$5, GPIO) is strongly recommended for standalone field use.
- ZWO SDK: the installer fetches `libASICamera2.so`; set `ZWO_ASI_LIB` if using a
  custom location.

## License

MIT. Clean-room implementation — no code from existing GPL allsky projects.
