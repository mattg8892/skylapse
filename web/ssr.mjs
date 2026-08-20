import { renderToString } from 'react-dom/server.browser'
import React from 'react'
import SettingsScreen from './src/screens/SettingsScreen.jsx'

// The screen fetches on mount; SSR never runs effects, so a stub is enough.
global.fetch = async () => ({ ok: true, json: async () => ({}) })

const cfg = {
  cameras: {
    'picam-imx477': {
      label: '', driver: 'picam', model: 'Pi Camera (imx477)',
      capture_schedule: 'always',
      day: { gap_s: 180, auto_exposure: true, target_brightness: 120,
             exposure_us: 20000000, max_exposure_us: 100000, gain: 1,
             max_gain: 22, manual_safety_stop: true },
      night: { gap_s: 30, auto_exposure: true, target_brightness: 90,
               exposure_us: 20000000, max_exposure_us: 25000000, gain: 1,
               max_gain: 22, manual_safety_stop: true },
      raw: { mode: 'off', every_nth: 10, keeper_buffer_frames: 3 },
      timelapse: { auto_render: true, clip_seconds: 30, quality: 'high',
                   resolution: '4k' },
      overlay: false, wb_r: 1.945, wb_b: 1.465, wb_auto: true,
    },
  },
  active_camera: '',
  notifications: { enabled: false, events: {}, ntfy_topic: '' },
  updates: { channel: 'release', auto_check: true },
  network: {}, auth: {}, location: {},
}
try {
  renderToString(React.createElement(SettingsScreen, {
    showToast: () => {}, storage: { free_gb: 200 },
  }))
  console.log('rendered with no config (loading state): OK')
} catch (e) {
  console.log('CRASH (loading state):', e.message)
}
