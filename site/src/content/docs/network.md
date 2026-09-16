---
title: Wi-Fi & access point
description: What the camera does when your Wi-Fi goes away, and how to switch it to its own network by hand.
sidebar:
  order: 3
---

Capture never depends on the network. Wi-Fi can flap, the access point can cycle, and the capture daemon keeps writing frames to the card no matter what. Networking is its own service (`skylapse-netwatch`) precisely so a network problem cannot take capture down with it.

## When the Wi-Fi goes away

If the camera can't reach a known network, it starts serving its own instead — **`Skylapse-Setup`**, open by default — so a camera you can't reach over Wi-Fi is still one you can walk up to. Join it and open **http://10.42.0.1**. When your network comes back, the camera returns to it by itself.

Two guards keep this from being annoying:

- **It will not drop the access point while your phone is connected to it.** Rejoining Wi-Fi means dropping the access point, and doing that halfway through somebody's setup is worse than waiting.
- **It stays on the access point for at least five minutes regardless**, so a network that is flapping can't leave you chasing it.

## Switching by hand

From the **Network tab** you can switch to access-point mode yourself — useful when you're standing at the camera and don't want to wait for anything to time out. That choice sticks until you switch it back, and survives a reboot; there are timed options if you'd rather it return to Wi-Fi on its own.

## Two cameras on one network

The image answers to `skylapse.local`. Give a second camera a different hostname when you write its card (Raspberry Pi Imager → customisation), because two `skylapse.local` on one network resolve to whichever answers first.

## Remote access

Viewing the camera from outside your own network — over [Tailscale](https://tailscale.com), on your account, nothing hosted by us — is written but **does not work on real hardware yet**. The settings card says so rather than offering a button that fails. On your own network everything works today, and the camera has never needed the internet to run. See [Status & roadmap](/status/).

:::note[No captive portal yet]
Joining the camera's own network works, but you have to type `10.42.0.1` yourself — it will not pop up a sign-in page the way a hotel network does.
:::
