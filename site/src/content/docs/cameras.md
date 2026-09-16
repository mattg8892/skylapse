---
title: Cameras & lenses
description: Which camera to buy for Skylapse, why the lens matters as much as the sensor, and where ZWO ASI cameras stand.
sidebar:
  order: 3
---

**Skylapse is a Raspberry Pi camera project first.** A Pi camera module on the CSI ribbon is the supported path: it needs no vendor software, everything in the SD image supports it end to end, and it is what every feature here is developed and tested against. The **HQ Camera / IMX477** is the specific one this is built around, and the one to buy if you are buying.

Other Pi-compatible modules — IMX708, IMX219, IMX519, OV5647, IMX296 and the many third-party boards using those sensors — work through the same driver and can be declared from the **Camera tab** when the Pi cannot see them by itself.

:::note[Third-party boards and "No camera detected"]
Raspberry Pi OS identifies cameras by reading a chip that many third-party boards — including most HQ/IMX477 clones — do not have. Declare the sensor on the Camera tab, restart, then **pull the power for 15 seconds**. The full sequence is on [Getting started](/getting-started/#2-open-it).
:::

## The lens matters as much as the camera

The HQ Camera ships bare, and the stock C/CS lenses see a narrow rectangle — fine for a bird box, useless for a sky. What you want is a **fisheye**, so a whole night's worth of sky lands inside one frame and the horizon comes out as a circle rather than a crop.

The development rig has used the **[Arducam 180° fisheye M12](https://www.amazon.com/dp/B0897QD6C2)** ([vendor page](https://www.arducam.com/arducam-180-degree-fisheye-1-2-3-m12-mount-with-lens-adapter-for-raspberry-pi-high-quality-camera.html)). Two things make it the easy pick rather than a lucky one:

- It is a **1/2.3" lens**, the HQ camera's own sensor format, so the image circle actually covers the sensor.
- It **includes the M12→CS adapter**. The HQ camera is C/CS mount and M12 lenses are not, so a bare M12 fisheye will not attach to it at all — the single most common way to buy the wrong thing here.

Any 180° fisheye of the right format works; this is the one that has been used, not an endorsement. Narrower fisheyes (Arducam sell 140° and 100°) trade sky for detail, which is a reasonable trade if you care more about one part of the sky than all of it. The current [reference build](/hardware/) runs a 2.5mm F1.2 CS-mount CCTV lens, which is the cheap, wide, fast option if you do not need the full horizon-to-horizon circle.

## ZWO ASI cameras

Second, and honestly second. If you are choosing a camera for this project, choose a Pi one.

- **It may not work with your camera.** The driver is verified against exactly one model (an ASI676MC). Other models go through code paths nobody here has run.
- **It needs a vendor library Skylapse cannot ship.** ZWO's licence does not allow their SDK to be redistributed and their download portal is browser-only, so it cannot be in the image.
- **It is not what new features are designed against.** A ZWO-only regression is likely to be found by you rather than by us.

You no longer need a terminal for it, though. On the **Camera tab** (or on the camera screen during setup), open **Add another camera → ZWO ASI camera (USB)**, accept ZWO's licence, and tap **Install ZWO support**. Skylapse downloads the library — about 4 MB, so the camera needs internet access at that moment — verifies it against a pinned checksum, installs it with the udev rules that raise the USB buffer limit large frames need, and restarts capture. No reboot.

The binaries come from the [INDI project's mirror](https://github.com/indilib/indi-3rdparty/tree/master/libasi) of ZWO's SDK, pinned to a tagged release in `scripts/skylapse-admin`. 64-bit Raspberry Pi OS only.

:::caution[Power a USB3 camera properly]
A USB3 camera on a non-official supply enumerates intermittently or not at all, and shows up as mysterious disconnects mid-night rather than an obvious power error. Use the official 5V/5A supply for a Pi 5.
:::

## White balance

Both sensor families read green high — two of every four photosites are green, and green is the most sensitive. The Camera tab has red and blue sliders per camera, with an "Auto from current frame" button to start from and a live preview. Set it once per camera; RAW/DNG pixels are never changed, and the multipliers are recorded in the file for your raw editor to apply.
