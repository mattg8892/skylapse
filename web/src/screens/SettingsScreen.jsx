import { useEffect, useState } from 'react'
import {
  Button, Card, NumberField, Segmented, Select, Toggle,
} from '../components/ui.jsx'

// Capture / timelapse / overlay are per-camera (config.cameras is a registry
// keyed by hardware id). Notifications and remote access are global.

const EVENT_LABELS = {
  aurora: 'Aurora possible tonight',
  storage_low: 'Storage running low',
  camera_offline: 'Camera offline',
  timelapse_ready: 'Timelapse ready',
}

const QUALITY_OPTIONS = [
  { value: 'standard', label: 'Standard' },
  { value: 'high', label: 'High' },
  { value: 'max', label: 'Max' },
]

const PERIOD_OPTIONS = [
  { value: 'night', label: 'Night' },
  { value: 'day', label: 'Day' },
]

const EXPOSURE_OPTIONS = [
  { value: 'auto', label: 'Auto exposure' },
  { value: 'manual', label: 'Manual exposure' },
]

export default function SettingsScreen({ showToast }) {
  const [cfg, setCfg] = useState(null)
  const [topic, setTopic] = useState(null)
  const [remote, setRemote] = useState(null)
  const [testResult, setTestResult] = useState(null)

  useEffect(() => {
    fetch('/api/config').then((r) => r.json()).then(setCfg).catch(() => {})
    fetch('/api/remote/status').then((r) => r.json()).then(setRemote).catch(() => {})
  }, [])

  /** PUT a partial config. Optimistic: the UI already shows the new value. */
  const save = async (patch, next) => {
    setCfg(next)
    await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).catch(() => showToast?.('Could not save settings'))
  }

  // config.cameras is replaced wholesale by PUT /api/config (model_copy with
  // update= swaps the whole key), so every camera edit sends the full registry.
  const saveCamera = (id, patch) => {
    const cameras = { ...cfg.cameras, [id]: { ...cfg.cameras[id], ...patch } }
    return save({ cameras }, { ...cfg, cameras })
  }

  const saveProfile = (id, period, patch) =>
    saveCamera(id, { [period]: { ...cfg.cameras[id][period], ...patch } })

  const saveNotifications = (notifications) =>
    save({ notifications }, { ...cfg, notifications })

  const setupNtfy = async () => {
    const r = await fetch('/api/notify/generate-topic', { method: 'POST' })
    setTopic(await r.json())
  }

  const sendTest = async () => {
    const r = await fetch('/api/notify/test', { method: 'POST' })
    const { sent } = await r.json()
    setTestResult(sent ? 'Sent — check your phone' : 'Not sent (check switch + topic)')
  }

  const enableRemote = async () => {
    await fetch('/api/remote/enable', { method: 'POST' })
    const id = setInterval(async () => {
      const st = await (await fetch('/api/remote/status')).json()
      setRemote(st)
      if (st.connected) clearInterval(id)
    }, 3000)
  }

  if (!cfg) return null
  const n = cfg.notifications
  const cameras = Object.entries(cfg.cameras ?? {})

  return (
    <div className="mt-6 flex flex-col gap-5">
      {cameras.length === 0 && (
        <Card title="Camera">
          <p className="mt-1 text-sm text-zinc-500">
            No camera has been seen yet. Capture settings appear once one is detected.
          </p>
        </Card>
      )}

      {cameras.map(([id, cam]) => (
        <CameraSettings
          key={id} id={id} cam={cam}
          onCamera={(patch) => saveCamera(id, patch)}
          onProfile={(period, patch) => saveProfile(id, period, patch)} />
      ))}

      {/* Notifications */}
      <Card title="Notifications"
        right={<Toggle checked={n.enabled} label="Enable notifications"
          onChange={(enabled) => saveNotifications({ ...n, enabled })} />}>
        <p className="mt-1 text-sm text-zinc-400">
          Alerts on your phone via the free ntfy app. Off by default.
        </p>

        {n.enabled && (
          <div className="mt-4 space-y-3">
            {!n.ntfy_topic && !topic ? (
              <Button onClick={setupNtfy} className="w-full">Set up phone alerts</Button>
            ) : (
              <div className="rounded-lg bg-zinc-800/60 p-3 text-sm">
                <p className="text-zinc-400">In the ntfy app, subscribe to topic:</p>
                <code className="text-sky-400">{topic?.topic ?? n.ntfy_topic}</code>
              </div>
            )}

            <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
              {Object.entries(EVENT_LABELS).map(([key, label]) => (
                <li key={key}
                  className="flex items-center justify-between px-3 py-2.5 text-sm">
                  <span>{label}</span>
                  <input type="checkbox" checked={n.events?.[key] ?? false}
                    onChange={(e) => saveNotifications({
                      ...n, events: { ...n.events, [key]: e.target.checked },
                    })} />
                </li>
              ))}
            </ul>

            <Button onClick={sendTest} className="w-full">Send test notification</Button>
            {testResult && <p className="text-sm text-zinc-400">{testResult}</p>}
          </div>
        )}
      </Card>

      {/* Remote access */}
      <Card title="Remote access">
        <p className="mt-1 text-sm text-zinc-400">
          View your camera from anywhere with your own free Tailscale account.
          Your camera is never exposed to the open internet.
        </p>

        {!remote?.installed ? (
          <p className="mt-3 text-sm text-zinc-500">
            Tailscale isn’t installed on this device.
          </p>
        ) : remote.connected ? (
          <div className="mt-3 rounded-lg bg-emerald-950 p-3 text-sm">
            <p className="text-emerald-300">Remote access enabled</p>
            <a href={remote.url} className="text-sky-400 underline">{remote.url}</a>
          </div>
        ) : remote.auth_url ? (
          <div className="mt-3 text-sm">
            <p className="text-zinc-400">
              1. Install the free Tailscale app on your phone and sign in.<br />
              2. Scan this code to approve the camera:
            </p>
            <div className="mt-3 flex justify-center rounded-lg bg-zinc-800 p-4"
              dangerouslySetInnerHTML={{ __html: remote.qr_svg }} />
          </div>
        ) : (
          <Button onClick={enableRemote} className="mt-3 w-full">
            Enable remote access
          </Button>
        )}
      </Card>
    </div>
  )
}

/* -- per-camera capture / timelapse / overlay ------------------------------ */

function CameraSettings({ id, cam, onCamera, onProfile }) {
  // Day and night are independent profiles in config; editing only one of them
  // silently would be a trap, so the period is an explicit control.
  const [period, setPeriod] = useState('night')
  const profile = cam[period] ?? {}
  const manual = !profile.auto_exposure

  return (
    <>
      <Card title="Capture"
        right={<span className="text-sm text-zinc-500">{cam.label || id}</span>}>
        <div className="mt-3 space-y-4">
          <Segmented value={period} onChange={setPeriod} options={PERIOD_OPTIONS} />

          <NumberField
            label="Gap between frames" suffix="s" min={0} max={3600}
            value={profile.gap_s ?? 0}
            onChange={(gap_s) => onProfile(period, { gap_s })} />
          <p className="-mt-1 text-xs text-zinc-500">
            Measured from the end of one frame to the start of the next, so it
            stays predictable as auto-exposure changes the exposure.
          </p>

          <Segmented
            value={manual ? 'manual' : 'auto'} options={EXPOSURE_OPTIONS}
            onChange={(v) => onProfile(period, { auto_exposure: v === 'auto' })} />

          {manual ? (
            <div className="space-y-4 rounded-xl bg-zinc-800/40 p-4">
              <NumberField
                label="Exposure" suffix="s" min={0.001} max={600} step={0.5}
                value={Number(((profile.exposure_us ?? 0) / 1e6).toFixed(3))}
                onChange={(s) => onProfile(period, { exposure_us: Math.round(s * 1e6) })} />
              <NumberField
                label="Gain" min={0} max={600}
                value={profile.gain ?? 0}
                onChange={(gain) => onProfile(period, { gain })} />
              <label className="flex items-start justify-between gap-3 text-sm">
                <span>
                  <span className="text-zinc-300">Safety stop</span>
                  <span className="mt-1 block text-xs text-zinc-500">
                    Pause capture at daylight or after repeated near-saturated
                    frames. Protects a tracked sensor from a forgotten dawn.
                  </span>
                </span>
                <input type="checkbox" className="mt-1"
                  checked={profile.manual_safety_stop ?? true}
                  onChange={(e) =>
                    onProfile(period, { manual_safety_stop: e.target.checked })} />
              </label>
            </div>
          ) : (
            <p className="text-xs text-zinc-500">
              Exposure and gain track the target brightness automatically, capped
              at {((profile.max_exposure_us ?? 0) / 1e6).toFixed(1)}s and gain{' '}
              {profile.max_gain}.
            </p>
          )}
        </div>
      </Card>

      <Card title="Timelapse"
        right={<Toggle checked={cam.timelapse?.auto_render ?? true}
          label="Auto-render at dawn"
          onChange={(auto_render) =>
            onCamera({ timelapse: { ...cam.timelapse, auto_render } })} />}>
        <p className="mt-1 text-sm text-zinc-400">
          Render last night automatically at dawn.
        </p>
        <div className="mt-4 space-y-3">
          <NumberField
            label="Clip length" suffix="s" min={5} max={600} step={5}
            value={cam.timelapse?.clip_seconds ?? 30}
            onChange={(clip_seconds) =>
              onCamera({ timelapse: { ...cam.timelapse, clip_seconds } })} />
          <Select
            label="Quality" value={cam.timelapse?.quality ?? 'high'}
            options={QUALITY_OPTIONS}
            onChange={(quality) =>
              onCamera({ timelapse: { ...cam.timelapse, quality } })} />
          <p className="text-xs text-zinc-500">
            Frame rate is derived from the night’s frame count to hit that length,
            clamped to 12–60 fps.
          </p>
        </div>
      </Card>

      <Card title="Image overlay"
        right={<Toggle checked={cam.overlay ?? false} label="Burn overlay into JPEGs"
          onChange={(overlay) => onCamera({ overlay })} />}>
        <p className="mt-1 text-sm text-zinc-400">
          Burn timestamp, exposure, gain and sensor temperature into the corner of
          each JPEG. RAW/DNG files are never touched.
        </p>
      </Card>
    </>
  )
}
