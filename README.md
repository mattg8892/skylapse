# Skylapse

**Allsky camera software for the Raspberry Pi.** Point a camera at the sky, and manage
the whole thing from your phone — live view, focus assist, timelapses, and RAW files,
without SSHing in every night.

<sub>[Getting started](#getting-started) · [Using it](#using-it) ·
[Troubleshooting](#troubleshooting) · [Development](#development) ·
[Design notes](DESIGN.md) · [Releases](https://github.com/mattg8892/skylapse/releases)</sub>

---

## What it does

- **Two camera families, one interface.** ZWO ASI over USB and Raspberry Pi camera
  modules over CSI. Both are verified on real hardware — see
  [what first contact changed](DESIGN.md#camera-drivers) for the specifics each one
  taught us.
- **Full-resolution JPEG every frame**, plus **DNG raw** on demand, on a schedule, or
  from a keeper button for when a meteor just went past.
- **Auto-exposure that tracks the sky**, with day/night/twilight profiles from sun
  altitude, and a manual mode for star-tracker rigs.
- **Nights browser** — scrub a whole night frame by frame, with a star-count chart to
  find the clear stretch, and download any frame as JPEG or DNG.
- **Focus assist** — a live view with 1×–10× zoom into the full-resolution sensor image
  and a sharpness score to chase while you turn the ring.
- **Dawn timelapses**, rendered automatically, with one-off length and quality overrides.
- **USB export** — copy nights to a stick, with a config backup alongside them.
- **Phone alerts** over [ntfy](https://ntfy.sh) when the camera stops capturing, and
  again when it recovers.
- **Falls back to its own Wi-Fi** if your network disappears, so a camera you cannot
  reach is still a camera you can walk up to — and an **access-point mode** you can
  switch on by hand when you are standing at it.
- **In-app updates** with automatic rollback if the new version doesn't come up healthy.

Capture, the web UI, and networking are three separate services, so a problem in one
cannot stop the others writing frames.

## What you need

| | |
|---|---|
| **Raspberry Pi** | Pi 5 recommended, Pi 4 works. [Pi 5](https://www.raspberrypi.com/products/raspberry-pi-5/) |
| **Power supply** | The official 5V/5A (Pi 5) or 5V/3A (Pi 4). Underpowering a USB3 camera shows up as mysterious disconnects mid-night, not as an obvious power error. |
| **Storage** | 64 GB+ microSD. A [high-endurance card](https://www.raspberrypi.com/documentation/computers/getting-started.html#recommended-sd-cards) if you plan to shoot RAW — see [storage](#storage-and-raw). |
| **Camera** | A [ZWO ASI](https://www.zwoastro.com/) USB camera, **or** a Pi camera module ([HQ Camera](https://www.raspberrypi.com/products/raspberry-pi-high-quality-camera/) / IMX477-class). |
| **Optional** | A DS3231 RTC module (~$5) — a Pi has no battery-backed clock, so it boots with a stale time until it reaches the network. |

Weatherproof housing, dew heater and lens are up to you; this is the software half.

## Getting started

### 1. Flash Raspberry Pi OS

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Choose **Raspberry Pi
OS (64-bit)**, Bookworm or newer.

In Imager's **customisation** settings (the gear icon), before writing:

- set a **hostname** — `skylapse` is a good choice, but give a second camera a
  different one: two `skylapse.local` on one network resolve to whichever answers first
- **enable SSH** and add your public key (or set a password)
- enter your **Wi-Fi** credentials and country

That customisation step is what makes the rest of this headless. Write the card, put it
in the Pi, and power up.

### 2. Log in and get the code

```bash
ssh <you>@skylapse.local          # or the Pi's IP address
git clone https://github.com/mattg8892/skylapse
cd skylapse
```

### 3. Run the installer

```bash
sudo ./install.sh
```

It installs system packages, creates a Python environment, builds the web interface, and
enables the services. It is safe to re-run — every step checks what is already there and
skips it — so this is also how you apply changes after a `git pull`.

> The installer runs Skylapse **from this checkout**, not from a copy elsewhere. Updating
> is `git pull && sudo systemctl restart skylapse-daemon`, or just the Updates card in
> Settings.

### 4. ZWO cameras only: install the SDK

Skipping this if you are using a Pi camera module — it needs nothing extra.

ZWO's SDK cannot be downloaded by a script (their portal is browser-only), so the
installer detects it and tells you if it is missing. The
[INDI project](https://github.com/indilib/indi-3rdparty/tree/master/libasi) redistributes
the same vendor binaries and is the easiest source:

```bash
# from https://github.com/indilib/indi-3rdparty/tree/master/libasi
#   armv8/libASICamera2.bin  -> the 64-bit Pi library
#   99-asi.rules             -> udev rules
sudo install -m 644 libASICamera2.bin /usr/local/lib/libASICamera2.so.1.41
sudo ln -sf libASICamera2.so.1.41 /usr/local/lib/libASICamera2.so.1
sudo ln -sf libASICamera2.so.1    /usr/local/lib/libASICamera2.so
sudo ldconfig
sudo install -m 644 99-asi.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Those udev rules matter for more than permissions: they raise the USB buffer limit that
large frames need. Replug the camera afterwards, or reboot.

Set `ZWO_ASI_LIB` if you install the library somewhere else.

### 5. Open the web interface

```
http://skylapse.local
```

The first visit runs a short setup: network, camera (with a test shot), where the camera
is, what to capture, and optionally a password. A couple of minutes on a phone, and
everything in it can be changed later in Settings.

After that the dashboard shows the latest frame, whether capture is healthy, and a
countdown to the next one. No login, no account, nothing in the cloud.

## Using it

**Dashboard** — latest frame, a status pill driven by whether frames are actually
arriving (not by what the daemon claims), a countdown to the next one, storage, and the
Save-RAW button.

**Nights** — every night captured, with frame counts and sizes. Open one to scrub the
filmstrip, jump between frames that have RAW files, watch the timelapse, or export to USB.

**Focus assist** — start it from the dashboard, then zoom to 8× and turn the ring until
the sharpness number peaks. Nothing is written to the card while focusing, and it exits
by itself after 15 minutes.

**Settings** — capture schedule (24/7 or night-only), exposure profiles, RAW policy,
timelapse options, phone alerts, config backup, and updates.

### Storage and RAW

Every-frame RAW is expensive: measured on a 12 MP camera it is **~37 GB per night**. The
settings screen shows your rig's own figure before you commit to it, and warns about
sustained write wear — a microSD card can fail suddenly after months of that. For RAW
work, use a high-endurance card or an external SSD.

Skylapse deletes the oldest nights automatically when free space runs low, frames first
and timelapses last.

### When the Wi-Fi goes away

If the camera can't reach a known network, it starts serving its own instead —
`Skylapse-Setup`, open by default — so a camera you can't reach over Wi-Fi is still one
you can walk up to. Join it and open **http://10.42.0.1**. When your network comes back,
the camera returns to it by itself.

It will not do that while your phone is connected to it. Rejoining Wi-Fi means dropping
the access point, and doing that halfway through somebody's setup is worse than waiting.
It also stays put for at least five minutes regardless, so a network that is flapping
can't leave you chasing it.

You can also switch to access-point mode by hand, from **Settings → Network** — useful
when you're standing at the camera and don't want to wait for anything to time out. That
choice sticks until you switch it back, and survives a reboot; there are timed options if
you'd rather it return to Wi-Fi on its own.

### Phone alerts

Settings → Notifications generates a private [ntfy](https://ntfy.sh) topic and shows you
what to subscribe to in the free app ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)). You get told
when capture stops and when it recovers. Everything is off by default.

The topic name is the only thing protecting it — treat it as a secret.

## Troubleshooting

**The web page doesn't load.** Check the services: `systemctl status skylapse-api
skylapse-daemon`. Logs are `journalctl -u skylapse-daemon -f`, which prints one line per
captured frame.

**"No camera detected" with a ZWO attached.** Confirm the SDK is installed
(`ldconfig -p | grep ASICamera`) and that the camera appears in `lsusb`. A camera that
enumerates but fails to capture is usually power — use the official supply.

**A Pi camera module doesn't enumerate.** Check `rpicam-hello --list-cameras`. If it says
`No cameras available!`:

1. Reseat the ribbon at both ends, contacts the right way round.
2. **Fully power off** — pull the plug for 15 seconds. A warm reboot does not drain the
   sensor's regulator, and some modules only come up after a cold start. This one is easy
   to mistake for a broken cable.
3. If it still fails, `sudo dmesg | grep imx477` tells you which: no lines at all means
   the overlay isn't loading; `failed to read chip id` means the sensor isn't answering,
   which is then genuinely a cable or module fault.

**Frames look green.** Both sensor families read green high — two of every four
photosites are green, and green is the most sensitive. Settings has red and blue sliders
per camera, with an "Auto from current frame" button to start from and a live preview. Set
it once per camera; RAW/DNG pixels are never changed, and the multipliers are recorded in
the file for your raw editor to apply.

**Capture stopped and you weren't told.** Turn on phone alerts (above). The watchdog
notices when frames stop arriving, not merely when the process dies.

## Development

No camera required — there is a simulator with a synthetic star field whose brightness
responds to exposure and gain, so auto-exposure behaves as it does on real hardware:

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

Three environment variables relocate everything: `SKYLAPSE_CONFIG`, `SKYLAPSE_IMAGES`,
`SKYLAPSE_RUN`. On a rig they default to `/etc/skylapse/config.yaml`,
`/var/lib/skylapse/images/` and `/run/skylapse/`.

### Architecture

| Service | Job |
|---|---|
| `skylapse-daemon` | Capture loop, camera drivers, JPEG/DNG pipeline, scheduling |
| `skylapse-api` | REST API and web interface (FastAPI + React) |
| `skylapse-netwatch` | Wi-Fi/access-point state machine, and the fallback when Wi-Fi is gone |

They share only a config file and status files, so none can take another down.
[DESIGN.md](DESIGN.md) has the full reasoning, including what each camera taught us on
first contact.

## Status

Working and verified on hardware: capture, both camera drivers, auto-exposure, hot-pixel
correction, DNG, timelapses, the nights browser, focus assist, USB export, phone alerts,
self-updating with rollback, and the Wi-Fi fallback and access-point mode — including the
guard that refuses to drop the access point while a phone is connected to it, which was
tested with a real phone because there is no other way to test it.

Not there yet:

- **No setup wizard.** Configuration is the Settings screen, and location has to be
  entered by hand. The access-point fallback works, but the first-boot flow that would
  let you hand the camera your Wi-Fi password through it is designed and not built —
  "Connect to a new network" on that screen is inert. Set Wi-Fi up in Pi Imager instead.
- **No access control.** Anyone on your network can reach the interface. Specified in
  [DESIGN.md](DESIGN.md), not yet built — don't port-forward it.

Issues and pull requests welcome.

## License

MIT. Clean-room implementation — no code from existing GPL allsky projects.
