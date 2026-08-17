# Skylapse

**Timelapse software for capturing the entire night sky, every night.** Point a Raspberry
Pi camera straight up and run the whole thing from your phone — live view, focus assist,
timelapses and RAW files, with no terminal in sight.

<sub>[Getting started](#getting-started) · [Cameras](#cameras) · [Using it](#using-it) ·
[Troubleshooting](#troubleshooting) · [Development](#development) ·
[Design notes](DESIGN.md) · [Releases](https://github.com/mattg8892/skylapse/releases)</sub>

---

## What it does

- **Write a card, plug it in, set it up on your phone.** No terminal, no config files, no
  account, nothing in the cloud. If there is no Wi-Fi to join, the camera serves its own.
- **Built for Raspberry Pi camera modules**, the [HQ
  Camera](https://www.raspberrypi.com/products/raspberry-pi-high-quality-camera/) /
  IMX477 above all — that is the camera this is developed and tested against, and
  boards the Pi cannot auto-detect can be declared from the setup screen. **ZWO ASI USB
  cameras are supported second, on a best-effort basis** — see
  [cameras](#cameras) before you buy one for this.
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
| **Camera** | A Pi camera module — the [HQ Camera](https://www.raspberrypi.com/products/raspberry-pi-high-quality-camera/) / IMX477 is the recommended one and what this is developed against. A [ZWO ASI](https://www.zwoastro.com/) USB camera may also work; see [cameras](#cameras). |
| **Optional** | A DS3231 RTC module (~$5) — a Pi has no battery-backed clock, so it boots with a stale time until it reaches the network. |

Weatherproof housing, dew heater and lens are up to you; this is the software half.

## Getting started

### 1. Write the card

Download **`skylapse.img.xz`** from the
[latest release](https://github.com/mattg8892/skylapse/releases/latest), open
[Raspberry Pi Imager](https://www.raspberrypi.com/software/), choose **Use custom** and
select the file you downloaded.

Then open Imager's **customisation** settings (the gear icon) before writing:

- enter your **Wi-Fi** credentials and country
- set a **hostname** if you like — the image already answers to `skylapse`, but give a
  second camera a different name: two `skylapse.local` on one network resolve to
  whichever answers first
- **enable SSH** if you ever want a terminal. You do not need it.

Write the card, put it in the Pi, and power up. The first boot expands the filesystem and
takes a minute or two longer than later ones.

### 2. Open it

**If you entered Wi-Fi details**, the camera joins your network:

```
http://skylapse.local
```

**If you didn't**, the camera serves its own network instead. Join **`Skylapse-Setup`**
from your phone's Wi-Fi settings — it is open, no password — and go to:

```
http://10.42.0.1
```

Either way, setup runs on the first visit: network, camera with a live test shot, where
the camera is, what to capture, and optionally a password. A couple of minutes on a
phone, and every answer can be changed later in Settings.

That is the whole install. No terminal, no config files, no account, nothing in the cloud.

> **If it says "No camera detected"**, that is usually not a fault. Raspberry Pi OS
> identifies cameras by reading a chip that many third-party boards — including most
> HQ/IMX477 clones — simply do not have. Tap **"My camera isn't being detected"** on that
> screen, pick your sensor, and it restarts with the camera declared. No terminal needed
> for that either.

## Cameras

**Skylapse is a Raspberry Pi camera project first.** A Pi camera module on the CSI
ribbon is the supported path: it needs no vendor software, everything in the SD image
supports it end to end, and it is what every feature here is developed and tested
against. The **HQ Camera / IMX477** is the specific one this is built around, and the
one to buy if you are buying.

Other Pi-compatible modules — IMX708, IMX219, IMX519, OV5647, IMX296 and the many
third-party boards using those sensors — work through the same driver and can be
declared from the setup screen when the Pi cannot see them by itself.

### ZWO ASI cameras

Second, and honestly second. If you are choosing a camera for this project, choose a Pi
one.

- **It may not work with your camera.** The driver is verified against exactly one model
  (an ASI676MC). Other models go through code paths nobody here has run.
- **It needs a vendor library Skylapse cannot ship.** ZWO's licence does not allow their
  SDK to be redistributed and their download portal is browser-only, so it cannot be in
  the image.
- **It is not what new features are designed against.** A ZWO-only regression is likely
  to be found by you rather than by us.

You no longer need a terminal for it, though. In **Settings → Cameras** (or on the
camera screen during setup), open **Add another camera → ZWO ASI camera (USB)**, accept
ZWO's licence, and tap **Install ZWO support**. Skylapse downloads the library —
about 4 MB, so the camera needs internet access at that moment — verifies it against a
pinned checksum, installs it with the udev rules that raise the USB buffer limit large
frames need, and restarts capture. No reboot.

The binaries come from the [INDI project's
mirror](https://github.com/indilib/indi-3rdparty/tree/master/libasi) of ZWO's SDK,
pinned to a tagged release in `scripts/skylapse-admin`. 64-bit Raspberry Pi OS only.

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

**"No camera detected" with a ZWO attached.** First check Settings → Cameras says ZWO
support is installed; if it doesn't, install it there. If it does and the camera is still
missing, it is power or the model: a USB3 camera on a non-official supply enumerates
intermittently or not at all, and models other than the ASI676MC are not verified and may
simply not open. From a terminal, `lsusb` shows whether the camera is on the bus at all
and `journalctl -u skylapse-daemon` says how far the open got.

**A Pi camera module doesn't enumerate.** Check `rpicam-hello --list-cameras`. If it says
`No cameras available!`:

1. **Fully power off** — pull the plug for 15 seconds. A warm reboot does not drain the
   sensor's regulator, and some modules only come up after a cold start. This is first
   because it is the cheapest thing to try and, on this project's own hardware, it was
   twice the answer to a camera that looked broken — including once immediately after
   writing a fresh card.
2. Reseat the ribbon at both ends, contacts the right way round.
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

**Installing onto a Pi you already have**, rather than flashing the image: clone the repo
onto it and run `sudo ./install.sh`. It installs system packages, builds the frontend and
enables the three services, running Skylapse in place from the checkout — so updating is
`git pull && sudo systemctl restart skylapse-daemon`. It is safe to re-run. The SD image
is this same script, run inside the image at build time (`image/build.sh`).

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

Working and verified on hardware, most of it the hard way: the flashable image, first-run
setup on a phone, capture, auto-exposure, white balance, hot-pixel
correction, DNG, timelapses, the nights browser, focus assist, USB export, phone alerts,
an optional password, self-updating with rollback, and the Wi-Fi fallback and
access-point mode — including the guard that refuses to drop the access point while a
phone is connected to it, which was tested with a real phone because there is no other
way to test it.

The whole path in this README — write a card, power it up, join the camera's own Wi-Fi,
finish setup on a phone, and have it capturing — has been done end to end on a Pi 5 with
an IMX477, with no terminal at any point.

Not there yet:

- **No captive portal.** Joining the camera's own network works, but you have to type
  `10.42.0.1` yourself — it will not pop up a sign-in page the way a hotel network does.
- **No remote access yet.** Viewing the camera from outside your own network — over
  [Tailscale](https://tailscale.com), your account, nothing hosted by us — is written and
  does not work on real hardware yet. The settings card says so rather than offering a
  button that fails. On your own network everything works today, and the camera has never
  needed the internet to run.
- **ZWO support is best effort.** It is verified on one model, its SDK has to be
  downloaded on demand because it cannot be redistributed, and it may not work with your
  camera at all. Pi camera modules are the supported path — see [cameras](#cameras).
- **No white balance for mono sensors**, and no colour management beyond the per-camera
  multipliers.

Issues and pull requests welcome.

## License

MIT. Clean-room implementation — no code from existing GPL allsky projects.
