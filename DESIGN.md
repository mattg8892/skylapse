# Skylapse — Design Document

Open-source allsky camera software for Raspberry Pi. Built to replace config-file-driven
setups with something anyone can install, point at the sky, and manage from a phone.

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
2. **Dual camera support.** ZWO ASI (USB) and Raspberry Pi camera modules (CSI) behind one
   driver interface. ZWO is the primary target; Pi cam ships behind the same interface.
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

## Camera drivers

```
CameraDriver (ABC)
├── ZwoDriver    zwoasi bindings over ZWO SDK — raw bayer out
└── PiCamDriver  picamera2/libcamera — raw bayer out
```

Both drivers return raw bayer + metadata; the pipeline owns debayer → JPEG and bayer → DNG
(via pidng). Identical output regardless of camera. Probe order: ZWO on USB first, then CSI.

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

Verified working as designed, no change needed: 12-bit sensor data arrives scaled to the
full 16-bit range (low bits populated, max 65534), so `mean_brightness`'s `255/65535`
assumption is correct; exposures of 5s and 15s complete cleanly; `99-asi.rules` raises
`usbfs_memory_mb` from 16 to 1024 on plug-in, which USB3 frames of this size need.

Known upstream annoyance, not patched: zwoasi 0.2.0's `Camera.__del__` raises
`TypeError: 'NoneType' object is not callable` at interpreter shutdown (module globals are
torn down before the finaliser runs). It is noise in the log after the capture loop has
already exited, and it is a library bug rather than ours.

### Camera registry

Every camera reports a stable `camera_id` (ZWO flash id/serial, or model for Pi cams).
`config.cameras` is a registry keyed by that id, holding per-camera day/night profiles and
RAW policy — a known camera restores its settings on plug-in; a new one gets safe defaults
and appears in the UI as "New camera found". Image store is namespaced per camera
(`images/<camera_id>/YYYY-MM-DD/`) so multi-camera support (v2: one daemon instance per
camera via a systemd template unit, `active_camera` selector in v1 when several are
attached) never requires a store migration.

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

## Remote access (optional, wizard-driven) — IMPLEMENTED (skylapse/remote.py, settings card)

Tailscale wrapped so the user never touches a terminal. Settings card "Enable remote
access": Skylapse runs `tailscale up`, renders the returned login URL as a QR code;
user installs the free Tailscale phone app (Google/Apple SSO), scans, done. Skylapse
polls `tailscale status` and then shows the permanent `https://skylapse.tail-*.ts.net`
URL (MagicDNS + auto-certs — which also unlocks installable PWA + web push). Off by
default; requires a free Tailscale account; camera is never exposed to the open
internet. Headscale documented as the self-hosted alternative for purists. Fits goal 0:
free tier, user's own account, nothing operated by the project.

## Licensing

MIT. All code clean-room — no code from GPL allsky projects.
