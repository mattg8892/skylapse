import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, ConfirmDialog, NumberField, Segmented, Select, Toggle,
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

const RAW_MODE_OPTIONS = [
  { value: 'off', label: 'Off' },
  { value: 'every_frame', label: 'Every frame' },
  { value: 'every_nth', label: 'Every Nth frame' },
  { value: 'window', label: 'Time window' },
]

const EXPOSURE_OPTIONS = [
  { value: 'auto', label: 'Auto exposure' },
  { value: 'manual', label: 'Manual exposure' },
]

const SCHEDULE_OPTIONS = [
  { value: 'always', label: '24/7' },
  { value: 'night_only', label: 'Night only' },
]

export default function SettingsScreen({ showToast, storage }) {
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
          key={id} id={id} cam={cam} storage={storage}
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

      <UpdateCard cfg={cfg} showToast={showToast}
        onChannel={(channel) =>
          save({ updates: { ...cfg.updates, channel } },
               { ...cfg, updates: { ...cfg.updates, channel } })} />

      {/* Backup */}
      <Card title="Backup">
        <p className="mt-1 text-sm text-zinc-400">
          Your settings as a YAML file. SD cards fail; keeping a copy elsewhere
          turns a rebuild into a restore. A copy is also written to any USB
          drive you export to.
        </p>
        <a href="/api/config/download"
          className="mt-4 block w-full rounded-lg border border-zinc-700 py-2.5
                     text-center text-sm hover:bg-zinc-800">
          Download config
        </a>
        <p className="mt-2 text-xs text-zinc-500">
          The hotspot password is redacted from this download.
        </p>
      </Card>

      {/* Network */}
      <NetworkCard showToast={showToast} />

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

/* -- network --------------------------------------------------------------- */

const AP_DURATIONS = [
  { value: 'hotspot', label: 'Until I switch it back' },
  { value: '60', label: '1 hour' },
  { value: '120', label: '2 hours' },
  { value: '480', label: '8 hours' },
]

/**
 * Manual access-point mode.
 *
 * The automatic fallback already covers "Wi-Fi broke". This covers the case it
 * cannot: someone standing at the camera who wants the access point now, with
 * no working Wi-Fi to break first and no timeout to wait out. Sticky is the
 * default duration because that is the one you want while you are working.
 */
function NetworkCard({ showToast }) {
  const [net, setNet] = useState(null)
  const [choice, setChoice] = useState('hotspot')
  const [pending, setPending] = useState(null)

  const refresh = useCallback(() => {
    fetch('/api/network').then((r) => r.json()).then(setNet).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 10000)
    return () => clearInterval(id)
  }, [refresh])

  if (!net) return null
  const ap = net.mode === 'hotspot' || net.mode === 'hotspot_timed'

  const apply = async (body) => {
    setPending(null)
    const r = await fetch('/api/network/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => null)
    if (!r?.ok) return showToast?.('Could not change network mode')
    // The switch takes a few seconds and may well take this page's connection
    // with it, so say what is about to happen rather than reporting success.
    showToast?.((await r.json()).note)
    setTimeout(refresh, 8000)
  }

  return (
    <Card title="Network">
      <p className="mt-1 text-sm text-zinc-400">
        {ap
          ? `The camera is its own access point, ${net.hotspot_ssid}.`
          : net.ssid
            ? `Connected to ${net.ssid}.`
            : 'Not connected to Wi-Fi.'}
        {net.remaining_s != null && ap && ' It returns to Wi-Fi when the time is up.'}
      </p>

      {ap ? (
        <>
          <p className="mt-3 text-sm text-zinc-500">
            {net.clients > 0
              ? `${net.clients} device${net.clients > 1 ? 's' : ''} connected.`
              : 'No devices connected yet.'}
            {' '}Join {net.hotspot_ssid}
            {net.hotspot_secured ? ' with your hotspot password' : ' (no password)'}
            , then open http://10.42.0.1
          </p>
          <Button tone="accent" className="mt-3 w-full"
            onClick={() => setPending({ mode: 'auto' })}>
            Switch back to Wi-Fi
          </Button>
        </>
      ) : (
        <div className="mt-4 space-y-3">
          <Select label="Stay in access-point mode for" value={choice}
            onChange={setChoice} options={AP_DURATIONS} />
          <Button tone="warn" className="w-full"
            onClick={() => setPending(choice === 'hotspot'
              ? { mode: 'hotspot' }
              : { mode: 'hotspot_timed', minutes: Number(choice) })}>
            Switch to access-point mode
          </Button>
        </div>
      )}

      <ConfirmDialog
        open={!!pending}
        title={pending?.mode === 'auto'
          ? 'Switch back to Wi-Fi?'
          : 'Switch to access-point mode?'}
        consequence={pending?.mode === 'auto'
          ? `The ${net.hotspot_ssid} network will disappear and any device on it — `
            + 'including this one — will be disconnected. The camera rejoins your '
            + 'Wi-Fi within about a minute.'
          : `This device will lose its connection to the camera. To get back, `
            + `join the ${net.hotspot_ssid} network from your Wi-Fi settings and `
            + 'open http://10.42.0.1'}
        confirmLabel={pending?.mode === 'auto' ? 'Rejoin Wi-Fi' : 'Switch anyway'}
        tone={pending?.mode === 'auto' ? 'accent' : 'warn'}
        onCancel={() => setPending(null)}
        onConfirm={() => apply(pending)} />
    </Card>
  )
}

/* -- per-camera capture / timelapse / overlay ------------------------------ */

function CameraSettings({ id, cam, storage, onCamera, onProfile }) {
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
          <div>
            <p className="mb-2 text-sm text-zinc-400">Capture</p>
            <Segmented
              value={cam.capture_schedule === 'night_only' ? 'night_only' : 'always'}
              options={SCHEDULE_OPTIONS}
              onChange={(capture_schedule) => onCamera({ capture_schedule })} />
            <p className="mt-2 text-xs text-zinc-500">
              Night only pauses capture while the sun is up and picks up again
              at dusk. Twilight still captures. Timelapse rendering, alerts and
              focus assist keep working either way.
            </p>
          </div>

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

      <RawCard raw={cam.raw ?? {}} storage={storage}
        onRaw={(patch) => onCamera({ raw: { ...cam.raw, ...patch } })} />
      {/* RawCard is defined below; kept out of this component so the capture,
          timelapse and RAW cards stay independently readable. */}

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

/* -- RAW policy ------------------------------------------------------------ */

function RawCard({ raw, storage, onRaw }) {
  const mode = raw.mode ?? 'off'
  // Surfaced here rather than only on the dashboard: this is the screen where
  // you turn on every-frame RAW, so this is where the cost belongs.
  const cost = storage?.nights_remaining != null && storage?.per_night_gb
    ? `About ${storage.nights_remaining} more nights at the current rate `
      + `(${storage.per_night_gb} GB/night${
        storage.basis === 'in_progress' ? ', measured from tonight so far' : ''}).`
    : null

  return (
    <Card title="RAW (DNG)">
      <p className="mt-1 text-sm text-zinc-400">
        Full sensor data beside each JPEG, for editing keepers in Lightroom,
        Siril or PixInsight. Roughly 25 MB per frame — far larger than a JPEG.
      </p>

      <div className="mt-4 space-y-3">
        <Select label="Save RAW" value={mode} options={RAW_MODE_OPTIONS}
          onChange={(m) => onRaw({ mode: m })} />

        {mode === 'every_nth' && (
          <NumberField label="Every" suffix="frames" min={2} max={1000}
            value={raw.every_nth ?? 10}
            onChange={(every_nth) => onRaw({ every_nth })} />
        )}

        {mode === 'window' && (
          <div className="space-y-3 rounded-xl bg-zinc-800/40 p-4">
            <TimeField label="From" value={raw.window_start ?? '22:00'}
              onChange={(window_start) => onRaw({ window_start })} />
            <TimeField label="Until" value={raw.window_end ?? '02:00'}
              onChange={(window_end) => onRaw({ window_end })} />
            <p className="text-xs text-zinc-500">
              Local time. A window that ends before it starts is read as
              crossing midnight.
            </p>
          </div>
        )}

        <NumberField label="Keeper buffer" suffix="frames" min={1} max={20}
          value={raw.keeper_buffer_frames ?? 3}
          onChange={(keeper_buffer_frames) => onRaw({ keeper_buffer_frames })} />
        <p className="-mt-1 text-xs text-zinc-500">
          How many recent frames are held in memory for the dashboard’s Save RAW
          button. Deeper costs RAM: about 25 MB per frame.
        </p>

        {mode === 'every_frame' && <EnduranceWarning storage={storage} />}

        {mode !== 'off' && cost && (
          <p className="rounded-lg bg-zinc-800/60 p-3 text-xs text-zinc-400">
            {cost}
          </p>
        )}
      </div>
    </Card>
  )
}

/* -- updates --------------------------------------------------------------- */

const RUNNING_STATES = ['waiting', 'running', 'rolling_back']

function UpdateCard({ cfg, showToast, onChannel }) {
  const [info, setInfo] = useState(null)
  const [progress, setProgress] = useState(null)
  const [checking, setChecking] = useState(false)
  const channel = cfg.updates?.channel ?? 'release'

  const load = useCallback(async (force) => {
    try {
      setInfo(await (await fetch(`/api/update/check${force ? '?force=true' : ''}`)).json())
    } catch { /* leave the previous answer showing */ }
  }, [])

  useEffect(() => { load(false) }, [load, channel])

  // Poll only while something is in flight — the API restarts mid-update, so
  // failures here are expected and must not clear what we already know.
  useEffect(() => {
    const id = setInterval(async () => {
      try {
        const s = await (await fetch('/api/update/status')).json()
        setProgress(s.state === 'idle' ? null : s)
        if (s.state === 'done') load(true)
      } catch { /* API restarting */ }
    }, 2000)
    return () => clearInterval(id)
  }, [load])

  const recheck = async () => {
    setChecking(true)
    await load(true)
    setChecking(false)
  }

  const apply = async (now) => {
    try {
      const r = await fetch('/api/update/apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_ref: info.target_ref, apply_now: now }),
      })
      const body = await r.json()
      if (!r.ok) showToast(body.detail ?? 'Could not start the update')
      else showToast(now ? 'Updating now — services will restart'
                         : 'Update queued for the next daytime window')
    } catch { showToast('Could not start the update') }
  }

  const busy = RUNNING_STATES.includes(progress?.state)

  return (
    <Card title="Updates"
      right={<span className="text-sm text-zinc-500">v{info?.current ?? '—'}</span>}>
      {info?.error ? (
        <p className="mt-1 text-sm text-amber-400">{info.error}</p>
      ) : info?.available ? (
        <>
          <p className="mt-1 text-sm text-sky-300">
            {channel === 'dev'
              ? `${info.commits_behind} commit${info.commits_behind === 1 ? '' : 's'} behind main (${info.latest})`
              : `Version ${info.latest} is available`}
          </p>
          {info.notes && (
            <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg
                            bg-zinc-800/60 p-3 text-xs text-zinc-400">
              {info.notes}
            </pre>
          )}
        </>
      ) : (
        <p className="mt-1 text-sm text-zinc-400">Up to date.</p>
      )}

      {progress && (
        <div className="mt-3 rounded-lg bg-zinc-800/60 p-3 text-sm">
          <p className={progress.state === 'error' ? 'text-rose-400'
            : progress.state === 'rolled_back' ? 'text-amber-300' : 'text-zinc-300'}>
            {progress.message ?? progress.state}
          </p>
          {progress.prior && (
            <p className="mt-1 text-xs text-zinc-500">
              Previous build {progress.prior} kept for rollback
            </p>
          )}
        </div>
      )}

      <div className="mt-4 flex gap-3">
        <Button onClick={recheck} disabled={checking}>
          {checking ? 'Checking…' : 'Check now'}
        </Button>
        {info?.available && (
          <>
            <Button tone="accent" className="flex-1" onClick={() => apply(false)}
              disabled={busy}>
              Update at next daytime
            </Button>
            <Button onClick={() => apply(true)} disabled={busy}>Now</Button>
          </>
        )}
      </div>

      <label className="mt-4 flex items-start justify-between gap-3 text-sm">
        <span>
          <span className="text-zinc-300">Development channel</span>
          <span className="mt-1 block text-xs text-zinc-500">
            Follow the latest commit on main instead of tagged releases. For
            development units — expect rough edges.
          </span>
        </span>
        <Toggle checked={channel === 'dev'} label="Development channel"
          onChange={(on) => onChannel(on ? 'dev' : 'release')} />
      </label>

      <p className="mt-3 text-xs text-zinc-500">
        Updates Skylapse only — never the operating system. If the camera
        doesn’t come back healthy within a minute, the previous version is
        restored automatically.
      </p>
    </Card>
  )
}

/**
 * Sustained-write warning for every-frame RAW.
 *
 * Inline and amber rather than a blocking modal: this is a real consideration,
 * not an error, and a modal trains people to dismiss without reading. It also
 * never goes away while the mode is active — it can be collapsed to one line,
 * but a one-time dismissal would be long forgotten by the time a card starts
 * failing, which is the moment it matters.
 */
function EnduranceWarning({ storage }) {
  const [open, setOpen] = useState(true)

  // The user's actual cost, not a generic figure — sensor size and cadence
  // vary enormously across rigs. Falls back to a hedged phrase when there is
  // nothing measured yet.
  const gb = storage?.every_frame_raw_gb
  const measured = storage?.raw_measured
  const frames = storage?.frames_per_night
  const partial = storage?.basis === 'in_progress'
  const rate = gb
    ? `~${gb} GB per night${measured ? '' : ' (estimated)'}`
    : '~30+ GB per night'

  return (
    <div className="rounded-lg border border-amber-700/60 bg-amber-950/40 p-3">
      <button onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-start gap-2 text-left text-sm text-amber-300">
        <span aria-hidden="true">⚠</span>
        <span className="flex-1">
          {open ? 'Heads up: heavy sustained writes' : `Heavy sustained writes — ${rate}`}
        </span>
        <span className="shrink-0 text-xs text-amber-500/80">
          {open ? 'Hide' : 'Details'}
        </span>
      </button>

      {open && (
        <div className="mt-2 space-y-2 text-xs text-amber-200/80">
          <p>
            Saving every frame as RAW writes <strong>{rate}</strong>
            {frames ? ` (${frames.toLocaleString()} frames at ` : ''}
            {frames
              ? `${Math.round((storage.raw_bytes_per_frame ?? 0) / 1e6)} MB each)`
              : ''}
            {partial && ' — measured from tonight so far, so a full night will be more'}
            . On a microSD card this is heavy sustained wear; cards can fail
            suddenly after months of it.
          </p>
          <p>
            Recommended: a high-endurance SD card or a USB SSD for the image
            store, and keep a config backup off the camera (Settings → Download
            config, or automatically with every USB export) so recovery is quick
            if the card does die.
          </p>
        </div>
      )}
    </div>
  )
}

function TimeField({ label, value, onChange }) {
  return (
    <label className="flex items-center justify-between gap-3 text-sm">
      <span className="text-zinc-400">{label}</span>
      <input type="time" value={value} onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5
                   tabular-nums outline-none focus:border-sky-600" />
    </label>
  )
}
