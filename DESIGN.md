# Skylapse — Design Document

Open-source timelapse software for capturing the entire night sky, every night, on a
Raspberry Pi. Built to replace config-file-driven setups with something anyone can
install, point at the sky, and manage from a phone.

## Goals

0. **Free forever, for everyone.** Hard constraint governing all decisions: no cost to
   the maintainer, no cost to the user, ever. Consequences: no native app store apps
   (PWA instead — no $99/yr Apple fee), no cloud relay or hosted service of any kind
   (everything runs on the user's Pi; remote access via the user's own free
   Tailscale/WireGuard), free-tier-only infra (GitHub public repo + Actions + Releases,
   PyPI). Nothing in the project may create a recurring bill for anyone, and nothing
   may depend on a backend that could be shut down.
1. **Zero-code setup.** Flash SD image, boot, join hotspot, finish a wizard on your phone.
   No SSH, no config files, no terminal — ever.
2. **Raspberry Pi cameras first.** Pi camera modules over CSI — the HQ/IMX477 in
   particular — are the primary target: no vendor library, supportable end to end from
   the SD image, and what every feature is developed against. ZWO ASI (USB) ships behind
   the same driver interface as a **best-effort second**, verified on one model and not
   guaranteed to work on others. (This was the other way round until 2026-08-17; see
   [Choosing between two cameras](#choosing-between-two-cameras).)
3. **JPEG + RAW (DNG).** Full-res JPEG every frame for timelapse; DNG on demand, on
   schedule, or triggered — for editing keepers in Lightroom/Siril/PixInsight.
4. **Capture never depends on network.** Wi-Fi can flap, hotspot can cycle, the imaging
   daemon keeps writing frames to the SD card no matter what.
5. **Field-ready.** Full standalone (no internet) operation with browser-based time sync.

## Non-goals for v1

Keograms, startrails, timelapse rendering, meteor detection, multi-camera, cloud upload.
All are designed-for (append-only image store, JSON sidecars) but not built. Ship the
capture core first.

## Architecture

Three independent systemd services. A bug in one can never take down the others.

```
skylapse-daemon     capture loop, drivers, image pipeline, scheduler
skylapse-api        FastAPI: REST + WebSocket, serves the React frontend
skylapse-netwatch   network state machine over NetworkManager (D-Bus)
```

Shared state:
- `/etc/skylapse/config.yaml` — single config file, atomic writes (tmp + fsync + rename)
- `/var/lib/skylapse/images/YYYY-MM-DD/` — image store, JSON sidecar per frame
- `/run/skylapse/` — runtime status files (daemon heartbeat, netwatch state, latest frame path)

### External image store (SPEC — not built)

A supported path for putting the image store on a permanently attached USB SSD,
and the recommended setup for anyone running every-frame RAW. Two independent
arguments point the same way: **capacity** (measured on an ASI676MC, every-frame
RAW costs ~37 GB/night, so a 256 GB card holds about a week) and **endurance**
(that is sustained write volume a microSD card is not built for — they fail
suddenly, months in, having given no warning).

- `SKYLAPSE_IMAGES` points at the mount, e.g. `/mnt/skylapse-images`. Nothing in
  the daemon changes: the store location is already an environment variable.
- The drive is mounted by a systemd `.mount` unit, and `skylapse-daemon.service`
  gains `RequiresMountsFor=` on the image root plus `After=` the mount unit.
- **The daemon must refuse to run rather than write to the bare mountpoint.**
  This is the entire risk of the feature. If the SSD is absent, unplugged, or
  slow to enumerate at boot, an unguarded daemon writes happily into the empty
  directory *underneath* the mount — filling the SD card it was moved off, and
  scattering a night across two filesystems where nothing will find it again.
  `RequiresMountsFor=` handles the boot ordering; the daemon should additionally
  verify at startup that the image root is a mountpoint whenever the configured
  path is not on the root filesystem, and fail loudly if it is not.
- Cleanup, retention and the storage card need no changes — they already measure
  whatever filesystem the store sits on.
- The setup wizard offers this when it detects a non-boot SSD, and can format
  and populate the mount unit. Until then it is a documented manual setup.

Deliberately not in scope: network storage (a dropped NFS mount fails in far
more ways, mid-write), or striping a store across several drives.

## Camera drivers

```
CameraDriver (ABC)
├── PiCamDriver  picamera2/libcamera — raw bayer out        (primary)
└── ZwoDriver    zwoasi bindings over ZWO SDK — raw bayer out (best effort)
```

Both drivers return raw bayer + metadata; the pipeline owns debayer → JPEG and bayer → DNG
(via pidng). Identical output regardless of camera. Probe order: Pi CSI first, then ZWO on
USB.

### What first contact with real ZWO hardware changed

Bench session on a Pi 5 + **ASI676MC** (3552×3552, 12-bit, USB3), SDK v1.41. Three
assumptions in the driver did not survive; all three are now fixed in `drivers/zwo.py`.

1. **White balance is not preview-only — it is baked into the RAW buffer.** ZWO ships a
   non-neutral factory WB (`WB_R` 55 / `WB_B` 75) and applies those gains to RAW16 itself.
   Measured on an unchanged scene: B/R was **1.686** at the factory values, **1.097** at
   neutral 50/50, and **0.009** at an extreme 99/1 — so the gains demonstrably reach the
   raw data. Unhandled, this tints every JPEG blue *and* bakes a vendor colour cast into
   DNGs that Siril/PixInsight are entitled to assume are untouched sensor data. The driver
   now forces neutral WB at `open()`. Colour is the pipeline's job, not the firmware's.
2. **`get_id()` is not universally supported.** The ASI676MC raises `ZWO_IOError` from it,
   so the model-name fallback is the common path, not the rare one — and since that name
   already begins with the vendor, the `zwo-` prefix was applied twice
   (`zwo-zwo-asi676mc`). That string is the config registry key *and* the image folder
   name, so it had to be fixed before any real capture history accumulated. Cameras with a
   writable flash id are the exception; the registry design in the previous section should
   be read with that in mind.
3. **`zwoasi.capture()` waits on the sensor with no deadline.** Its poll loop spins on
   `ASI_EXP_WORKING` forever, so a camera that stops responding mid-exposure — a USB3
   brownout on a long exposure being the classic cause, and exactly what the hardware
   notes in the README warn about — would hang the capture loop indefinitely. That
   silently defeats the goal-4 promise that capture never stops, because the daemon's
   reopen-with-backoff recovery can only fire on a `CameraError` that never arrives. The
   driver now runs its own bounded poll (exposure + 15s margin) and raises `CameraError`
   on timeout, making the existing recovery path reachable.

**Consequence — the JPEG path has no white balance, and now it shows.** Neutralising the
camera WB is right for raw fidelity, but it revealed that `pipeline/process.py` debayers
and rescales without ever applying colour multipliers. On the same bench scene the neutral
raw means are R 12258 / **G 18030** / B 13300 — green runs ~1.4× the others because RGGB
has two green photosites per quad and green QE is highest. So JPEGs went from a blue cast
(ZWO's 55/75 acting as an accidental, wrong white balance) to a green one (no white balance
at all). The factory defaults were masking a missing pipeline stage. Fixing it belongs in
the pipeline, not the driver: JPEG encode needs per-camera WB multipliers — measured once
per sensor, or a grey-world estimate for the sky — while DNG keeps the neutral raw data and
carries the multipliers as `AsShotNeutral` metadata for Siril/PixInsight to apply.
**Implemented 2026-08-16 — see [White balance](#white-balance) below.**

Verified working as designed, no change needed: 12-bit sensor data arrives scaled to the
full 16-bit range (low bits populated, max 65534), so `mean_brightness`'s `255/65535`
assumption is correct; exposures of 5s and 15s complete cleanly; `99-asi.rules` raises
`usbfs_memory_mb` from 16 to 1024 on plug-in, which USB3 frames of this size need.

Known upstream annoyance, not patched: zwoasi 0.2.0's `Camera.__del__` raises
`TypeError: 'NoneType' object is not callable` at interpreter shutdown (module globals are
torn down before the finaliser runs). It is noise in the log after the capture loop has
already exited, and it is a library bug rather than ours.

### The ZWO SDK is installed on demand, not shipped — IMPLEMENTED (2026-08-17)

ZWO's licence forbids redistributing their SDK and their download portal is
browser-only, so `libASICamera2` cannot be in the SD image. That made a ZWO rig
the one setup still requiring SSH — a direct contradiction of goal 1, and the
only entry in the README's limitations that needed a terminal.

Removing the ZWO driver entirely was considered and rejected: the problem is
*distribution*, not the driver. It is instead fetched on request, which
redistributes nothing — the user asks for it and accepts ZWO's terms, and the
file comes from the [INDI project's
mirror](https://github.com/indilib/indi-3rdparty/tree/master/libasi) of the same
vendor binaries.

Shape, the same as the camera-overlay button that preceded it:

`Settings → Cameras` / wizard camera step → `POST /api/setup/zwo/install`
(`skylapse/zwosdk.py`) → `sudo -n skylapse-admin zwo-sdk` → download, verify,
install, `ldconfig`, udev reload → `systemctl restart skylapse-daemon`.

Decisions worth keeping:

- **Pinned and checksummed, not "latest".** The helper hardcodes an
  `indi-3rdparty` tag and the SHA-256 of both files. This is an unsigned
  third-party binary landing in `/usr/local/lib` as root; "whatever is at that
  URL today" is not an acceptable input to that. A mismatch installs nothing and
  leaves no partial file — a truncated library would be found by the loader and
  fail at `dlopen`, which reads as broken hardware.
- **Acceptance is in the request.** `accept_terms` has no default. The licence is
  the user's to accept, so the server never assumes it.
- **Restart, not reboot.** Nothing here touches the boot config. But the daemon
  only probes for cameras when it opens one, so without the restart a freshly
  installed SDK does nothing until the next night.
- **aarch64 only.** The mirror has armv6/armv7 builds; nobody here has run them,
  and a button that installs the wrong architecture and then reports a camera
  fault is worse than one that is honestly absent.
- **The Python bindings ship anyway.** `pip install -e .[zwo]` is pure Python and
  tiny, so it is unconditional — installing ZWO support later is then one
  download rather than rebuilding a venv on a camera in the field.

This closes the SSH gap. It does not make ZWO a first-class target: the UI says
plainly, above the button, that support is best effort, verified on one model,
and may not work at all.

### Choosing between two cameras

Probe order is simulator → Pi CSI → ZWO USB.

**It was the other way round until 2026-08-17.** ZWO led because it was declared
the primary target, which was a statement about the bench this was built on
rather than about the product. What the product actually is became clear once
the SD image existed: a Pi camera needs no vendor library, so it is the only
camera the image can support end to end; the ZWO SDK cannot legally be shipped
inside it. The ZWO driver is also verified against exactly one model, while the
Pi path covers every sensor Raspberry Pi OS carries an overlay for. So the
common case a probe order should favour is a Pi camera on the sky, and a rig
with both attached is more likely to be a Pi camera plus a ZWO that is not
pointed anywhere in particular than the reverse.

`config.active_camera` overrides that for the rig where the ZWO *is* the
sky-facing camera. It holds a `camera_id`, and the driver is taken from the part
before the first dash — every id is built as `<driver>-<hardware identity>`
(`picam-imx477`, `zwo-asi676mc`), so the config needs no second field that could
disagree with the id. A preference naming a camera that is not attached logs a
warning and falls back to the probe order: unplugging the preferred camera
should degrade to capturing with the other one, not stop the night.

Selection is exposed in both the wizard's camera step and Settings → Cameras,
which render the same component (`web/src/components/camera.jsx`). Settings
previously had no camera management at all — you plugged something in and hoped
the daemon noticed — so a second camera, or a first one the Pi could not
auto-detect, meant walking the wizard again or reaching for SSH.

### Pi camera: what first light changed (2026-08-15)

`drivers/picam.py` was a stub that had never touched hardware. Against an
Arducam UC-517 (IMX477, 4056x3040) on a Pi 5 it was wrong in five ways, each
measured rather than reasoned about.

1. **The default raw stream is not raw.** `create_still_configuration(raw=...)`
   without an explicit format yields `BGGR_PISP_COMP1` on a Pi 5 — the PiSP
   compressed format, ~1 byte per pixel. The stub fed that to the debayer as
   16-bit bayer. Ask for an unpacked format, then read back what libcamera
   actually provided; the reply is authoritative, not the request.
2. **Rows are padded.** A 4056-pixel row arrives with an 8128-byte stride —
   4064 uint16, eight pixels of padding. Reshaping to the nominal width shears
   the image progressively down the frame.
3. **Bayer order comes from the configured stream, not the sensor.** The sensor
   advertises `SRGGB12_CSI2P`; the delivered stream is `SBGGR16`. Reading the
   order off the sensor swaps red and blue.
4. **Control limits depend on the configuration and must be read after it.**
   Before `configure()` this sensor reports a 66ms exposure ceiling; after,
   694 seconds. The stub hardcoded 200s and gain 22; the sensor reports
   110µs–694s and 22.3. Frame duration must also be pinned around the exposure
   or libcamera silently clamps a 30s sub to the current frame rate.
5. **Controls reach the sensor several frames late.** Measured at seven frames
   on the IMX477, with the queue serving old-exposure frames meanwhile. Taking
   the first frame after a change files a 20ms exposure under a 2s label —
   100ms, 500ms and 2s captures came back byte-for-byte identical. Capture now
   discards until the metadata matches, judged on a tolerance because the
   sensor quantises to its line time (100000µs is honoured as 99954µs), and
   frames record what the sensor reports rather than what was asked.

Measured and deliberately unchanged: 12-bit samples arrive **left-shifted**
into the 16-bit container (max 65520 = 4095 << 4, low nibble always clear), so
the pipeline's existing full-range scaling is already correct — the opposite of
what "12-bit sensor" suggests.

A consequence worth its own note: profile defaults are ZWO-shaped, with gains in
the hundreds. AE spills into gain once exposure is capped, so a 22x sensor
carrying a 300 ceiling would keep asking for gain it cannot reach and never see
brightness respond — fine by day on exposure alone, stalled after dark. Profiles
are now clamped to the camera's reported limits on every open, downward only.

The raw defaults are still ZWO-shaped even now that Pi cameras lead, which reads
backwards but is harmless: the clamp runs on every open and only ever lowers, so
a Pi module gets Pi-sized ceilings before the first frame either way. Rewriting
the defaults would move them for every existing rig's stored profiles to fix
nothing.

Verified end to end on the module: own registry entry and image folder,
4056x3040 JPEGs with thumbnails and sidecars, a DNG that parses with
`CFAPattern` BGGR, and AE converging on a real scene (254.9 → 140.1 toward a
target of 120). **Not yet verified: the star-count path**, which is skipped in
daylight — it needs a night on this module.

Two things settled while getting there:

- **The venv must be built with `--system-site-packages`.** `picamera2` comes
  from apt with no wheel, so without that flag it is invisible to the venv and
  `PiCamDriver.probe()` returns `False` on hardware that is working perfectly —
  a silent, camera-shaped failure with no error anywhere. The rig's venv had
  been created by hand without the flag; `install.sh` now repairs that case
  rather than skipping any venv that merely exists. Verified afterwards that the
  project's own pinned numpy/OpenCV/FastAPI still take precedence over the
  system copies.
- **Enumeration needs a cold power cycle, not a reboot.** The module failed to
  answer across several reboots — `imx477: failed to read chip id 477` with
  `-121 EREMOTEIO` on one port and `-110 ETIMEDOUT` on the other — with the
  driver bound and the device tree correct. A full power-off restored it
  immediately; the sensor regulator does not drain across a warm reboot. Worth
  keeping as the diagnostic ladder: no `imx477` lines at all means a missing
  overlay, a failed chip-ID read means the sensor is not answering, and if
  reseating does not fix that, try a cold boot before suspecting the cable.
  `i2cdetect` on the sensor's bus is the tiebreaker — `UU` at 0x1a means the
  driver holds a live device.

An IR-CUT accessory, if fitted, would be a GPIO-switched day/night filter — a
future feature, deliberately not wired.

### Camera registry

Every camera reports a stable `camera_id` (ZWO flash id/serial, or model for Pi cams).
`config.cameras` is a registry keyed by that id, holding per-camera day/night profiles and
RAW policy — a known camera restores its settings on plug-in; a new one gets safe defaults
and appears in the UI as "New camera found". Image store is namespaced per camera
(`images/<camera_id>/YYYY-MM-DD/`) so multi-camera support (v2: one daemon instance per
camera via a systemd template unit, `active_camera` selector in v1 when several are
attached) never requires a store migration.

## White balance

Per-camera `wb_r` / `wb_b` multipliers in the registry, green fixed at 1.0 as the
reference. Per camera because they describe a sensor and the lens in front of it. Default
1.0/1.0 is bit-for-bit the pre-white-balance pipeline, verified by comparing encoded JPEGs
against the previous commit — a rig that has been running for weeks must not have its
colour shift under it by a release.

Applied after debayer and before the 8-bit conversion, so the sensor's headroom absorbs
the multiplication instead of quantising it into 8-bit steps, and clipped at full scale
rather than wrapping. Thumbnails come off the same array; the focus live view gets the
same treatment, because judging focus through a green filter is nobody's idea of a live
view. DNG pixel data is untouched — that is the point of shipping DNG — and the
multipliers ride along as `AsShotNeutral`, which DNG wants as the neutral *in camera
space*: the reciprocal, green normalised to 1.

**Auto is a seed, not an answer.** `/api/wb/suggest` returns grey-world multipliers from
the current frame and applies nothing. Grey-world assumes the scene averages to grey,
which a sky at dusk and a room lit by one warm lamp both fail to do, so no automatic
estimate can be right for every camera and lens. The manual sliders are the feature; the
button is where you start. It reports the unclamped figure alongside the clamped one, so a
suggestion that ran past the end of the range reads as a lens or lighting problem rather
than a considered answer.

`/api/wb/preview` renders a *pending* pair from a raw mosaic the capture loop leaves in
`/run`, decimated by whole 2x2 quads so it is still a mosaic. The saved JPEG cannot serve:
it has the applied multipliers baked in and whatever the last setting clipped is gone.
`/run` is a tmpfs, so this costs the card nothing.

### Metering: what the auto-exposure loop actually measures

`mean_brightness` averaged the raw mosaic, which is not a brightness. Two of every four
photosites in an RGGB quad are green, so a flat mean is half green by construction, before
the sensor's response is even considered. Metering is now WB-corrected Rec. 601 luminance
computed from per-plane means.

The direction of the fix is the opposite of the obvious guess and worth recording. Green
is 50% of the mosaic mean but **59%** of Rec. 601 luma, so switching to luma at neutral
multipliers moves the number about +2% and achieves nothing on its own. What moves it is
the white balance: correcting the cast lifts red and blue *up to* green, so the corrected
frame is genuinely brighter than the mosaic mean implied, and auto-exposure answers by
pulling exposure down.

Measured on the IMX477, a lit indoor scene, at the moment the multipliers were applied:

| | exposure | metered |
|---|---|---|
| last frame before | 52920 us | 127.8 |
| first frame after (1.78 / 1.111) | 50985 us | **145.9** |
| converged, 6 frames later | **34736 us** | 123.5 |

Exposure fell 34% and the frame stopped being both green and too bright. A neutral frame
meters exactly as it did — the luma weights sum to 1 — so mono cameras and any rig still
at 1.0/1.0 are not re-exposed by this.

## What the first real night changed (2026-08-17)

The first unattended night on the rig: 2205 frames, an IMX477 behind a 180-degree
fisheye, clear with dew arriving twice. Three things were wrong, and each had been
wrong since it was written — they had simply never had a night to be wrong on.

### The camera was keeping London's clock

Raspberry Pi OS Lite ships set to `Europe/London` and nothing in the SD image changes
it. Setup asks where the camera is and stores an IANA timezone, and nothing read it back.
So `day_folder()` — which rolls the night at "local" noon precisely so a night is never
split — rolled at noon in London, which is 6 AM in Racine. The night was cut in half at
05:59, 2205 frames in `2026-08-17` and the dawn in `2026-08-18`.

Fixed by using the timezone the camera is configured with, not the host's, for both the
night folder and the frame names — one function, `local_time()`, so the two can never
disagree again. Frame names had the same fault and would have been the clue: a frame
called `21:49` was written at 15:49 local.

The system timezone is still whatever the image shipped with. It is not worth a helper
verb to change it — nothing else reads it — but it does mean journal timestamps run on
London time on an unmodified card, which is worth knowing before reading logs.

### Dawn rendered the wrong folder

The dawn job took `max()` of the night directories: the newest name. Ordinarily the
rollover is at noon and dawn is hours away, so the newest folder *is* the night that just
ended and this worked by luck. With the split above, the newest folder was one created
minutes earlier — so at dawn it rendered the 25 frames that had landed since 06:00,
validated them correctly (all 25 were there), fired `timelapse_ready`, and produced a
2.08-second file. The 2205-frame night was never rendered at all.

The lesson is not "max() was wrong" but that the render target was being *derived
separately* from where the frames were being written. It now asks `day_folder()` for the
folder frames are going into right now, which at dawn is last night's, by construction.

Worth stating because it was the assumption going in: **the render validation was not at
fault, and the file was not corrupt.** It decoded cleanly, end to end, with no errors.
Post-render frame counting did exactly its job — it verified the file contained every
frame it had been given. It cannot know the wrong folder was handed to it.

### Star counting was counting noise

218,617 "stars" in an 8:15 PM twilight frame with none visible, and 248,138 on a 2:23 AM
frame too dewed to see through. The old detector thresholded the raw Bayer mosaic at a
fixed offset and counted every connected component.

Four faults, in the order they mattered: it ran on the mosaic, whose adjacent pixels are
different colour channels and therefore carry a built-in checkerboard; the threshold was
a fixed number rather than relative to the frame's own noise; single pixels counted; and
nothing rejected a shape that was obviously not a star. See `pipeline/analyze.py` — the
constants there are the measured separation from this night, not guesses.

Measured against three frames from it:

| frame | before | after |
|---|---|---|
| 20:15 twilight, no stars visible | 215,553 | **0** |
| 03:04 clear, best of the night | 60,264 | **748** |
| 02:23 dew-covered | 248,138 | **2** |

**The dew signature is worth keeping.** Across the night the new count rises steadily as
the sky darkens — 248 at 21:00 to 840 at 04:30 — and craters to single digits at exactly
02:30 and 05:00, the two dew episodes, recovering in between. That happens for a physical
reason: dew scatters light, which lifts the measured noise floor, which raises the
threshold, which culls the faint stars first. So **a count that craters on a night that
was clear an hour ago is a dew alarm**, and a better one than a dedicated sensor because
it measures the thing that actually matters — whether the sky is still visible through
the dome. Not built; the notification hook and the dew heater are the obvious consumers.

### The clip length setting was never honoured on a long night

Reported straight after the first good render: the clip is 1:13 and the setting says 30
seconds. It was right, and it had always been wrong — just less visibly, because before
the level cap a long night came out 37 seconds instead of 73.

Duration is frames over frame rate. The rate is not free: it is capped at 60, and h264
level caps it further by output size. So on a 2205-frame night the only way to reach 30
seconds is to put fewer frames in, and `clip_seconds` is now honoured by sampling evenly
across the night — 900 of 2204 frames at 30 fps, which is exactly 30 seconds.

Nothing is lost that is kept anywhere else: every frame is still on the card and still in
the nights browser. What changes is the pace of the clip, which is the thing the setting
was asking about in the first place. A night with fewer frames than the target is left
alone rather than padded by duplication.

### Auto-exposure was pinned all night and never said so

The night ran at 25s and gain 22 — both ceilings, on a module whose gain ceiling *is* 22
— for hours. Every frame was as bright as the rig could make it and the target was simply
out of reach. Nothing reported this, so the only symptom was a sky that looked darker
than it should have, with no indication of where to look.

Pinning at both ceilings for three consecutive frames now sets a status flag the
dashboard shows and logs once per episode. Three frames because one is a cloud crossing.
It is not an error and is not notified — it is a fact about the sky that the operator
needs in order to decide whether to spend more exposure on it.

## Capture scheduler

- Sun altitude via `astral` decides day / twilight / night profiles.
- **Timing model: exposure + gap.** The next capture starts `gap_s` after the previous
  frame ends — how a photographer thinks ("30s exposure, 10s gap"), and deterministic in
  auto mode too, where a fixed interval would silently stretch as AE changes exposure.
  `gap_s: 0` = back-to-back. AE's exposure ceiling is `max_exposure_us`, explicit.
- Auto-exposure: mean brightness of last frame nudged toward target, clamped to
  [min_exposure, max_exposure_us]. Manual override per profile.
- Filenames: `img_YYYYMMDD_HHMMSS.jpg` under a local-noon-rollover date folder.
- RAW modes: off / every_frame / every_nth / schedule window / keeper button (rolling RAM
  buffer of last N raw buffers, dumped to DNG on request).
- **Manual exposure override (tracker mode):** `auto_exposure: false` locks the profile's
  exact `exposure_us`/`gain` — AE never touches them (tested). Supports the
  camera-on-star-tracker use case: e.g. locked 30s subs, `gap_s: 0` for back-to-back frames (or 10 for a 10s breather), `raw.mode: every_frame` for stacking. Settings UI surfaces this
  as a per-camera "Auto exposure / Manual exposure" toggle with exposure+gain fields —
  a skin over the existing flag, not a new mechanism.
- **Manual safety stop (checkbox, default ON, manual mode only):** pauses capture when
  the sun calc says daylight OR after 3 consecutive near-saturated frames (mean >= 235).
  Protects a tracked sensor from a forgotten dawn (focused sun through a telephoto is
  real damage, not just blown frames). Paused state: status `paused_safety`, phone
  notification, resume via UI (POST /api/capture/resume) or automatically at next dusk —
  never un-pauses into the condition that tripped it. Irrelevant in auto mode by design.

## Network state machine

States: BOOT → TRY_WIFI → { CONNECTED | HOTSPOT } ; HOTSPOT → STANDALONE (user choice).

Modes (config): `auto` (default) | `standalone` ("always use standalone mode" checkbox) |
`wifi_only`.

### Loop guards — each one is a unit test in tests/test_statemachine.py

| # | Trap | Guard |
|---|------|-------|
| 1 | Flapping: edge-of-range connect/drop cycle tears hotspot up and down | Exponential backoff on reconnects (10s → 30s → 2m → 10m cap). Minimum hotspot dwell time (default 300s) before any Wi-Fi retry may tear it down. |
| 2 | Stranded phone: background rescan kills hotspot mid-setup | Never scan or switch while ≥1 client is associated to the hotspot. "Try again" in the UI warns about the ~90s disconnect before proceeding. |
| 3 | Wrong password: saved network in range, auth fails forever | Distinguish auth-failure from not-found. After 3 auth failures a network is marked `auth_failed` for the session, skipped, surfaced in UI as "wrong password?". |
| 4 | Zombie wizard: setup-complete flag lost to power cut | Flag written atomically *before* wizard's final screen; wizard is idempotent (re-entry shows current values). |
| 5 | Session standalone must self-heal | "Use in standalone mode" sets a session flag only. Reboot returns to TRY_WIFI unless config mode is `standalone`. |
| 6 | Watchdog of last resort | systemd `WatchdogSec=30` on netwatch; any hang → restart → deterministic BOOT state. Daemon unaffected. |

### What first contact with a real radio changed (2026-08-16)

The state machine above was right. Everything that drove NetworkManager on its behalf was
wrong, in ways no unit test written against it could have caught — the tests asserted the
decisions, and every fault was in the layer that observes and executes. Verified end to
end on the rig with Ethernet plugged in as a safety line, which turned out to be the only
reason two of these were survivable.

**The radio's mode is not its connection state.** `nmcli dev` reports wlan0 "connected"
whether it has joined a network or is serving one, so `_wifi_connected()` answered True
while the camera *was* the access point. The state machine was told the house network was
fine at the exact moment it was not. `iw dev wlan0 info` reports the operating mode
directly; every probe now goes through it.

**`iw station dump` lists the AP you are joined to.** On a client interface it therefore
counts 1 permanently, which would have frozen guard 2 forever — a hotspot that could
never be torn down, for a reason nobody would ever have guessed from the code.

**A connection's name is not its SSID.** netplan generates `netplan-wlan0-yourmomshouse`
for the SSID `yourmomshouse`, and the background rescan intersected connection *names*
with scan results. On any netplan-managed Pi that set is always empty, so the one
automatic route from hotspot back to Wi-Fi silently never fired: a camera that fell back
stayed fallen back forever. Exactly the failure the subsystem exists to prevent.

**`nmcli dev wifi hotspot` always applies WPA**, with a key it generates itself. With the
documented default of no password, the camera broadcast a network whose passphrase
existed only inside NetworkManager — visible to a phone, joinable by nobody. Found by
trying to join it from a real phone. The profile is now built explicitly.

**`nmcli con down` blocks autoconnect on the device.** It leaves it flagged "disconnected
by user or client" (reason 39), and NM will not autoconnect a device in that state. The
route home lowered the hotspot and then waited for an autoconnect that provably never
came — measured, the whole 90s grace window passed with the NM journal logging nothing at
all. Candidates are now activated by name, which as a side effect finally gave guard 3
the per-attempt error text it needs to tell a wrong password from an absent network.

**Every guard is a duration, so the clock has to be stamped per event.** The context took
its reading once per poll, and the event that records "the hotspot came up" is raised from
inside a call that has just blocked for the full 90s connect grace. The recorded start
time was ~90s in the past before the AP existed: a 300s dwell guard lasted 209s. Measured
again after the fix: 305.0s.

**Nothing reconciled the picture against the radio.** The service executed each action
once, at the transition, and never looked again. NetworkManager restarted — which
modifying a netplan-generated connection is enough to trigger — nmcli was unavailable for
about two seconds, the hotspot failed to come up, and nothing retried. Netwatch sat in
HOTSPOT reporting an access point that did not exist, and went on reporting it after NM
came back and autoconnected the radio underneath it. With no known network in range, that
camera is unreachable until someone power-cycles it. The poll now reconciles: a fallback
accepts Wi-Fi that appeared underneath it, and an access point that should be up and is
not gets raised again.

**A root-owned atomic write is not atomic.** netwatch runs as root because it drives
NetworkManager, and `tempfile.mkstemp` creates 0600 owned by the writer. One expiring
access-point session replaced `/etc/skylapse/config.yaml` with a root-only copy; the api
and daemon run as an ordinary user, so every page returned 500 and capture stopped.
`config.save()` now carries the original mode and owner across the replace.

**Guard 5 had no way out short of a reboot.** "Use in access point mode" sets a session
flag, and Try Again did not clear it — so a retry that failed landed back in HOTSPOT with
the background rescan permanently switched off, looking for all the world like a camera
waiting for its network to return that never once checks.

**The UI conflated a chosen access point with a failed one.** Both leave the camera
serving the same SSID. Switching to access-point mode from Settings therefore landed on
the "No Wi-Fi connection" screen, whose own "use in access point mode" button then
appeared to do nothing — it was dismissed by a flag nothing had ever set, so the state
moved underneath and the screen stayed on top of it. Reported from the rig as "i click use
in standalone mode it does nothing im just stuck".

### Manual access-point mode — IMPLEMENTED

The fallback covers "Wi-Fi broke". It cannot cover someone standing at the camera who
wants the access point *now*: there is no failure to wait out, and making them wait out a
connect timeout to reach a camera in front of them is absurd.

`POST /api/network/mode` takes `auto` | `hotspot` | `hotspot_timed(minutes)`. Sticky is
the default and the one that matters — it is what you want while you are working. It is
persisted to config rather than sent as a command, because a power cut in the field must
not quietly put the camera back on Wi-Fi while somebody is still working on it, and
netwatch re-reads it every poll because restarting the network service is precisely what
you cannot ask someone to do from a phone that is about to lose its connection. A timed
session's deadline is cleared from the file when it lapses, so a stale one cannot
resurrect access-point mode at the next boot.

Setting `auto` also issues the retry command, since the camera may be an access point by
session choice with the config never having said so — clearing a mode that was never set
would make "switch back to Wi-Fi" inert in exactly the case it is most likely to be
pressed.

### Time sync trust hierarchy

NTP (chrony, when internet) > RTC (DS3231 if detected) > browser time (sent by frontend
with every session start). Browser sync applies only when drift > 5s and no better source.
Never step backward during an active capture session — defer to session boundary. Timezone
comes from the browser alongside the timestamp.

## Web UI

React + Tailwind SPA served by the API. Screens:
- **Setup wizard** (first boot / captive portal): Wi-Fi join or standalone, camera detect
  + test shot, location (manual lat/long — must work offline), formats & intervals.
- **Connection screen** (hotspot, post-setup): saved networks with reason (out of range /
  wrong password?), Try again, Connect new network, Use in standalone mode +
  "Always use standalone mode" checkbox, time sync card when clock source is browser-only.
- **Dashboard**: latest frame (websocket push), tonight strip, capture status, storage
  gauge, persistent `Network: Standalone` badge when applicable.
- **Settings**: capture profiles, RAW policy, network mode, storage cleanup threshold.

## Roadmap: v1.5 features (post-hardware-validation, pre-announcement)

1. **Focus assist — IMPLEMENTED (daemon/focus.py).** POST /api/focus/start ->
   rapid 1s/high-gain throwaway frames (never saved), variance-of-Laplacian score on
   the center 25% of the frame, /api/status streams {score, best, trend} for the UI.
   Auto-exits after 15 minutes so a forgotten session can't consume the night;
   /api/focus/stop for manual exit. Tested: score is monotonic with focus error.
2. **Aurora alerts — IMPLEMENTED (daemon/aurora.py).** Polls NOAA SWPC K-index (free,
   keyless) every 30 min from the daemon loop. Threshold auto-derived from latitude
   (Kp7 at 42.7°N, Kp6 at 45.5°N, hemisphere-symmetric); alerts once per Kp episode,
   latched until the storm subsides; never fires in daylight; fetch failures silent.
   Current Kp exposed in /api/status. OVATION probability map: future refinement.
3. **Star count + sky quality trend — IMPLEMENTED (pipeline/analyze.py).** Per-frame
   threshold+blob count on the raw mosaic (skipped in daylight), stored in each JSON
   sidecar and in /api/status; GET /api/stars/{camera}/{night} returns the night's
   series for the chart. Tested: count scales with stars, craters under cloud blur.
4. **JPEG overlay — IMPLEMENTED (pipeline/analyze.py).** Optional (config `overlay`,
   default off): timestamp / exposure / gain / temp burned bottom-left with outlined
   text readable on any sky. Applied at JPEG encode; RAW/DNG never touched.
5. **Dawn timelapse auto-render — IMPLEMENTED (daemon/nightjobs.py).** Trigger: the
   night→day sun transition. Output: timelapse_YYYY-MM-DD.mp4 beside the frames
   (h264/yuv420p, CRF 20, fps derived from frame count targeting ~30s clips).
   Idempotent across restarts; fires the timelapse_ready notification. Storage cleanup
   (also implemented) deletes oldest nights' frames first and mp4s last — the
   timelapse is the keepsake — and never touches the newest night. Keeper RAW button
   (POST /api/keeper) and camera-offline notification wired in the same pass.
   User settings (per camera, settings card): auto-render on/off, clip length
   (default 30s; fps derived and clamped 12-60), quality Standard/High/Max (CRF
   23/20/17). Codec/pixel format/filenames deliberately not configurable. On-demand:
   POST /api/timelapse/render/{camera_id}/{night}; add ?force=true to re-render an
   existing night with the current settings (morning "make it longer" workflow).

### Notification system — IMPLEMENTED (skylapse/notify.py, settings card)

Two channels, one abstraction (`notify(event, title, body)`):
- **ntfy (default/primary)** — free app; wizard shows a QR for a generated private topic
  (`skylapse-<random>`); Skylapse POSTs to it. Works with zero prerequisites: no HTTPS,
  no Tailscale, no PWA. Self-hostable ntfy server documented for purists (goal 0 safe).
- **Web push (automatic upgrade)** — available once remote access is enabled (Tailscale
  HTTPS URL satisfies the browser secure-context requirement; PWA installed from it).

Settings UI: a Notifications card with a **master on/off switch** and per-event toggles.
Events: aurora possible · storage low · camera offline · timelapse ready. Everything is
**off by default**; the wizard offers aurora alerts as one opt-in step. Master off
silences all channels unconditionally.

## Roadmap: v2

6. **Automatic hot-pixel correction — IMPLEMENTED (pipeline/hotpixel.py), zero user effort by design.** No lens-cap dark
   sessions ever: median-stack a rolling sample of each night's own frames; pixels that
   stay bright while the sky rotates are defects. Map keyed by sensor-temp/exposure
   bucket (already in sidecars), refreshed nightly in the background, applied during
   debayer. The user never sees the feature.
7. **Dew heater control — IMPLEMENTED, EXPERIMENTAL (daemon/dewheater.py).** Gated by
   `dew_heater.experimental_enabled` (default OFF): flag off = subsystem never
   constructed, no I2C probe, no GPIO access, nothing in status (tested). BME280 on
   I2C (0x76/0x77 probed; BMP280 chip-id rejected — no humidity) -> Magnus dewpoint;
   gpiozero on a MOSFET-switched pin (BCM 18 default). Hysteresis on/off margins
   2C/4C with a tested dead band (no chatter); heater forced OFF on daemon exit.
   Deps in the `dewheater` extra. Hardware touch-points (_read_bme280, _set_gpio) are
   the only unverified code — first Pi bench session verifies and drops the
   experimental gate. Heater hardware options: resistor ring around the dome base
   (~3-5W at 12V, 3D-printed carrier), USB lens-warmer band, or nichrome.

Deliberately out of scope: plugin system, image stacking, ML cloud classification,
satellite ID overlays, MQTT/Home Assistant, FTP/website publishing. The identity is
lighter and more opinionated than the incumbents.

Repo extra (non-software): printable enclosure STL + parts list, when ready.

## In-app updates — IMPLEMENTED (skylapse/updater.py, settings card)

Skylapse updates itself from its own git checkout. **Never the OS**: no apt, no
firmware, no other packages. A camera that reboots into a broken userland because
its imaging app decided to upgrade the system is worse than one running last
month's build.

- **Channels.** `release` (default) polls the GitHub releases API unauthenticated
  — a public repo needs no token, and one call a day is nothing against the rate
  limit — and compares the tag against `skylapse.__version__`. `dev` follows
  `origin/main` instead, for development units where waiting for a tag defeats
  the point. Checks are cached for a day; the UI's "Check now" forces one.
- **Applying runs detached.** The update restarts `skylapse-api`, so it cannot
  run inside it — the restart would kill the updater mid-flight, possibly
  between `git checkout` and `pip install`. `/api/update/apply` spawns
  `python -m skylapse.updater apply <ref>` in a new session; it outlives both
  services and reports through `/run/skylapse/update.json`.
- **Only what changed gets rebuilt.** `pip install -e .` runs only if
  `pyproject.toml` differs, `npm run build` only if anything under `web/` does.
  Both are minutes on a Pi and most updates touch neither.
- **Auto-rollback.** The prior ref is recorded before anything changes. If the
  daemon is not both `active` under systemd *and* answering `/api/status` within
  60s of the restart, the updater checks the prior ref back out, re-runs the same
  install/build steps, and restarts again. Rolling back code but not dependencies
  would leave a mismatch harder to diagnose than the original failure. Health
  needs both signals: a daemon can be `active` while wedged before its first frame.
- **Deferred by default.** A restart drops frames, and the night you must not
  drop frames is a clear one, so an update waits for the next daytime window
  unless the operator picks "Now".
- **Restarting services needs privilege**, via `sudo -n systemctl restart`. On a
  dev unit that is the operator's passwordless sudo; a packaged appliance should
  narrow this to a polkit rule for those two units specifically.

## Access control (SPEC — build before the wizard)

Optional single password, OFF by default. The wizard's final screen offers "Protect this
camera with a password?" with Skip as a first-class option; existing installs get the same
via a Security card in Settings. Router-admin-page tier by design: one shared password, no
accounts.

- Stored hashed (argon2/bcrypt) in config — never plaintext.
- Login issues a long-lived (~30 day) session cookie: once per device.
- Protects UI + API by default. Optional sub-toggle **"public live view"**: latest frame
  visible without login; settings and controls still locked.
- Lockout recovery = physical access: remove the password entry over SSH, or (future
  appliance) hotspot setup mode offers a reset. Never requires a reflash.
- Tailscale remote access is already identity-authenticated; the password is a second layer
  there. Honest framing: the camera is never internet-exposed either way — this defends
  against the local LAN.
- Deliberately **not**: LAN HTTPS (self-signed warnings, and Tailscale gives real HTTPS
  remotely), multi-user, OAuth, API keys.
- Implementation: FastAPI session middleware + login screen + Settings card. Must land
  **before** the setup wizard so the wizard integrates it.

## Remote access (optional) — WRITTEN, NOT WORKING (skylapse/remote.py)

**Status as of 2026-08-17: parked, and the settings card says "coming soon".** It has now
been rewritten once and failed on hardware twice. The code and the endpoints stay; what is
switched off is offering it to someone as though it works. Marking it implemented when it
had never run on a camera is what let it sit broken for weeks, so it does not get that
label again until a real rig completes the flow.



Tailscale wrapped so the user never touches a terminal. Settings card "Enable remote
access": Skylapse runs `tailscale up`, renders the returned login URL as a QR code;
user installs the free Tailscale phone app (Google/Apple SSO), scans, done. Skylapse
polls `tailscale status` and then shows the permanent `https://skylapse.tail-*.ts.net`
URL (MagicDNS + auto-certs — which also unlocks installable PWA + web push). Off by
default; requires a free Tailscale account; camera is never exposed to the open
internet. Headscale documented as the self-hosted alternative for purists. Fits goal 0:
free tier, user's own account, nothing operated by the project.

### What using it on the rig changed (2026-08-17)

The card had been marked implemented since it was written, and it had never worked on a
camera. Three separate faults were found and fixed — and it *still* did not work when it
was tried on the rig afterwards, which is why it is parked rather than shipped. Whoever
picks this up starts by reproducing that failure, not by re-reading this list: everything
below was verified only in tests.

1. **Nothing ever installed Tailscale.** Not `install.sh`, not the SD image. So the card
   correctly reported "Tailscale isn't installed on this device" on every flashed
   camera — and that sentence was the entire card. A dead end in a product whose claim
   is that you never need a terminal is worse than a missing feature, because it looks
   like a broken one. It now installs on request, from Tailscale's own signed apt
   repository (they are not in Debian), through the same privileged helper as the ZWO
   SDK. Unlike the SDK there is nothing to checksum-pin, and that is right rather than
   lax: this is a signed repository, not a loose binary, and apt verifies every package
   against the keyring from then on.
2. **`tailscale up` needs root**, and the API deliberately does not have it. Even where
   someone had installed Tailscale by hand, the button could only fail. All of it —
   `up`, `serve`, `down`, and `status` where the socket is root-only — now goes through
   `skylapse-admin`.
3. **`enable_https_serve()` was called by nothing.** A camera that got through the login
   would show an `https://…ts.net` address pointing at a host serving nothing on 443.
   `serve()` now runs as soon as the login lands.

Still not preinstalled in the image, on purpose: a VPN client daemon on every camera by
default, and a third-party apt source in every image's `sources.list`, is a larger thing
to opt someone into than a feature they can turn on in one tap. `--accept-dns=false` on
`up`, because a camera that starts taking its resolver from a tailnet can lose its own
network for reasons nobody standing next to it can see.

The rule this leaves behind, which applies well beyond this card: **a state is not
finished until it carries an action or an explanation.** "It isn't installed" is a fact;
"it isn't installed, here is the button" is a feature.

## Licensing

MIT. All code clean-room — no code from GPL allsky projects.

Nothing proprietary is redistributed. ZWO's SDK is not in this repository and not
in the SD image; it is downloaded by the camera, at the user's request, after
they accept ZWO's terms — see [The ZWO SDK is installed on
demand](#the-zwo-sdk-is-installed-on-demand-not-shipped--implemented-2026-08-17).
