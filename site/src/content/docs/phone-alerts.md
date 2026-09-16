---
title: Phone alerts
description: Get told on your phone when the camera stops capturing, and again when it recovers, using the free ntfy app.
sidebar:
  order: 4
---

**Settings → Notifications** generates a private [ntfy](https://ntfy.sh) topic and shows you what to subscribe to in the free app ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)). You get told when capture stops and when it recovers. Everything is off by default.

## How it decides

The watchdog notices when **frames stop arriving**, not merely when the process dies. A daemon that is alive but wedged, a camera that has silently disconnected, a card that has filled — all of them look the same to the watchdog: no new frame when there should be one. That is the alert.

## Keep the topic private

The topic name is the only thing protecting it — treat it as a secret. Anyone who knows it can read your alerts.

## Nothing hosted by us

ntfy is a public, free push service you subscribe to directly; Skylapse posts to it from your Pi. There is no Skylapse server in the loop, in keeping with the [free-forever rule](/design/#goals).
