import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, ConfirmDialog, NumberField, Section, Segmented, Select, Slider,
  Toggle,
} from '../components/ui.jsx'
import { CameraPanel, useCameras } from '../components/camera.jsx'

// Capture / timelapse / overlay are per-camera (config.cameras is a registry
// keyed by hardware id). Notifications and remote access are global.

const EVENT_LABELS = {
  aurora: 'Aurora possible tonight',
  storage_low: 'Storage running low',
  camera_offline: 'Camera offline',
  timelapse_ready: 'Timelapse ready',
}

// A pixel budget rather than a width: h264 level follows macroblock count, so
// on a 4:3 or square sensor a "3840 wide" render is still level 6.0 and still
// unplayable. Measured on this rig.
const TIMELAPSE_RES_OPTIONS = [
  { value: '4k', label: '4K — plays everywhere (default)' },
  { value: '1080p', label: '1080p — smallest, streams best' },
  { value: 'full', label: 'Full sensor — may not play' },
]

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
  const [testResult, setTestResult] = useState(null)
  const [cameraInfo, refreshCameras] = useCameras()

  const reloadConfig = useCallback(() => {
    fetch('/api/config').then((r) => r.json()).then(setCfg).catch(() => {})
  }, [])

  useEffect(() => { reloadConfig() }, [reloadConfig])

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

  if (!cfg) return null
  const n = cfg.notifications
  const cameras = Object.entries(cfg.cameras ?? {})
  // Named in the section header, so 'This camera' is never a question.
  const activeName = (cfg.cameras?.[cfg.active_camera]?.label
    || cfg.cameras?.[cfg.active_camera]?.model
    || cameras[0]?.[1]?.label || cameras[0]?.[1]?.model
    || cameras[0]?.[0] || '')

  return (
    <div className="mt-6 flex flex-col gap-8">
      {/* Ordered by how often a setting is actually touched, not by the order
          the features happened to be built in. Everything a night gets tuned
          with is first and always visible; everything set once when the camera
          was installed is last and folded away. */}
      <Section title="This camera"
        subtitle={activeName
          ? `Imaging settings for ${activeName}`
          : 'Imaging settings, once a camera has been seen'}>
        {cameras.map(([id, cam]) => (
          <CameraSettings
            key={id} id={id} cam={cam} storage={storage}
            onCamera={(patch) => saveCamera(id, patch)}
            onProfile={(period, patch) => saveProfile(id, period, patch)} />
        ))}
      </Section>

      <Section title="Hardware"
        subtitle="What is attached, and adding a camera the Pi cannot find">
        {/* Cameras: the same panel the wizard uses. Settings used to have no way
            to add a camera at all — you plugged one in and hoped the daemon
            noticed — so a second camera, or a first one the Pi cannot auto-detect,
            meant walking setup again or reaching for SSH. */}
        <Card title="Cameras">
          <p className="mt-1 text-sm text-zinc-400">
            {cameras.length > 1
              ? 'Which camera is pointed at the sky, and how to add another.'
              : 'What’s attached, a test shot to prove it works, and how to add '
                + 'a camera the Pi can’t find by itself.'}
          </p>
          <div className="mt-4">
            <CameraPanel info={cameraInfo} refresh={refreshCameras}
              selected={cfg.active_camera}
              onSelect={(active_camera) =>
                save({ active_camera }, { ...cfg, active_camera })} />
          </div>
          {cameras.length === 0 && (
            <p className="mt-4 text-xs text-zinc-500">
              Capture settings appear below once a camera has been seen.
            </p>
          )}
        </Card>
      </Section>

      <Section title="Alerts"
        subtitle="What the camera tells your phone about, and when">
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
      </Section>

      <Section title="Set up once" collapsible
        summary="Network, remote access, password, updates and config backup — the parts of the camera you configure when you install it and then leave alone.">
        {/* Network */}
        <NetworkCard showToast={showToast} />
        {/* Remote access */}
        <RemoteCard />
        {/* Security */}
        <SecurityCard showToast={showToast} />
        <UpdateCard cfg={cfg} showToast={showToast}
          onChannel={(channel) =>
            save({ updates: { ...cfg.updates, channel } },
                 { ...cfg, updates: { ...cfg.updates, channel } })}
          onAutoCheck={(auto_check) =>
            save({ updates: { ...cfg.updates, auto_check } },
                 { ...cfg, updates: { ...cfg.updates, auto_check } })} />
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
      </Section>
    </div>
  )
}

/* -- remote access --------------------------------------------------------- */

/**
 * Parked, and saying so.
 *
 * The working version of this card is in git — install Tailscale from the
 * vendor's apt repository, sign in with a QR code, publish the UI on the
 * tailnet's HTTPS name — and it did not survive contact with a real camera. The
 * endpoints are still there and still wired up; what is turned off is offering
 * them to someone as though they work.
 *
 * The card stays visible on purpose. Silently deleting a feature people were
 * told about reads as "it was removed"; saying it is coming reads as what is
 * actually true. What must not happen is the third option — a button that
 * fails, which is where this started.
 */
function RemoteCard() {
  return (
    <Card title="Remote access"
      right={<span className="rounded-full bg-zinc-800 px-2.5 py-1 text-xs
                              text-zinc-400">Coming soon</span>}>
      <p className="mt-1 text-sm text-zinc-500">
        View your camera from anywhere through your own free Tailscale account,
        with no port forwarding and nothing of yours on anybody’s server.
      </p>
      <p className="mt-3 text-sm text-zinc-500">
        It isn’t finished. Rather than leave a button here that doesn’t work,
        it’s switched off until it does — everything else on this page is
        unaffected, and the camera has never needed the internet to run.
      </p>
    </Card>
  )
}

/* -- network --------------------------------------------------------------- */

// Same list the wizard offers; kept here so an existing install can correct a
// guessed default without walking setup again.
const WIFI_COUNTRIES = [
  ['US', 'United States'], ['GB', 'United Kingdom'], ['DE', 'Germany'],
  ['FR', 'France'], ['NL', 'Netherlands'], ['CA', 'Canada'],
  ['AU', 'Australia'], ['NZ', 'New Zealand'], ['IE', 'Ireland'],
  ['IT', 'Italy'], ['ES', 'Spain'], ['SE', 'Sweden'], ['NO', 'Norway'],
  ['FI', 'Finland'], ['DK', 'Denmark'], ['PL', 'Poland'], ['CH', 'Switzerland'],
  ['AT', 'Austria'], ['BE', 'Belgium'], ['PT', 'Portugal'], ['CZ', 'Czechia'],
  ['JP', 'Japan'], ['IN', 'India'], ['BR', 'Brazil'], ['ZA', 'South Africa'],
  ['MX', 'Mexico'], ['SG', 'Singapore'],
].map(([value, label]) => ({ value, label: `${label} (${value})` }))

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
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    fetch('/api/network').then((r) => r.json()).then(setNet).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 10000)
    return () => clearInterval(id)
  }, [refresh])

  if (!net) return null
  // What the camera *is*, not what was persisted. "Use in access point mode"
  // on the fallback screen is a session choice that deliberately leaves the
  // config alone, so reading the mode here had the card offering to switch to
  // access-point mode while the camera was already serving one.
  const ap = net.access_point

  const apply = async (body) => {
    setPending(null)
    setBusy(true)
    const r = await fetch('/api/network/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => null)
    if (!r?.ok) {
      setBusy(false)
      return showToast?.('Could not change network mode')
    }
    // The switch takes a few seconds and may well take this page's connection
    // with it, so say what is about to happen rather than reporting success.
    showToast?.((await r.json()).note)
    // The switch takes a few seconds; leaving the buttons live until then is
    // how the fallback screen's equivalent got pressed six times in a row.
    setTimeout(() => { setBusy(false); refresh() }, 8000)
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
          <Button tone="accent" className="mt-3 w-full" disabled={busy}
            onClick={() => setPending({ mode: 'auto' })}>
            {busy ? 'Switching…' : 'Switch back to Wi-Fi'}
          </Button>
        </>
      ) : (
        <div className="mt-4 space-y-3">
          <Select label="Stay in access-point mode for" value={choice}
            onChange={setChoice} options={AP_DURATIONS} />
          <Button tone="warn" className="w-full" disabled={busy}
            onClick={() => setPending(choice === 'hotspot'
              ? { mode: 'hotspot' }
              : { mode: 'hotspot_timed', minutes: Number(choice) })}>
            {busy ? 'Switching…' : 'Switch to access-point mode'}
          </Button>
        </div>
      )}

      <div className="mt-5 border-t border-zinc-800 pt-4">
        <Select label="Wi-Fi country" value={net.country || ''}
          options={[{ value: '', label: 'Not set' }, ...WIFI_COUNTRIES]}
          onChange={async (country) => {
            if (!country) return
            await fetch('/api/network/country', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ country }),
            }).catch(() => showToast?.('Could not save the country'))
            refresh()
            showToast?.('Saved — applies next time the access point starts')
          }} />
        <p className="mt-2 text-xs text-zinc-500">
          Radio regulations differ by country. Until this is set the camera may
          not transmit at all, including the network it serves for setup.
        </p>
      </div>

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

          {/* An unlabelled Day/Night toggle is easy to miss entirely, and
              missing it means adjusting one set of settings while watching the
              other one run — which is exactly what happened, with the slider
              taking the blame. Everything below this line belongs to whichever
              side is selected. */}
          <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-3">
            <p className="mb-2 text-sm text-zinc-300">
              Which half of the day are you setting up?
            </p>
            <Segmented value={period} onChange={setPeriod} options={PERIOD_OPTIONS} />
            <p className="mt-2 text-xs text-zinc-500">
              Day and night keep <b className="text-zinc-400">separate</b>{' '}
              settings — gap, exposure and brightness below all belong to{' '}
              <b className="text-zinc-400">{period}</b> only, and changing them
              leaves the other alone. Twilight uses the night settings, since
              that is when the interesting sky starts.
            </p>
          </div>

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
            /* One control. Auto-exposure already reads every frame and works
               out the exposure the sky needs; the only judgement left to a
               person is whether they want it a bit brighter or darker than the
               camera's own idea, and that is a nudge, not a number. */
            <div className="space-y-4 rounded-xl bg-zinc-800/40 p-4">
              <ExposureNudge
                target={profile.target_brightness ?? DEFAULT_TARGET[period]}
                period={period} cameraId={id}
                onChange={(target_brightness) =>
                  onProfile(period, { target_brightness })} />

              <AdvancedExposure profile={profile}
                onProfile={(patch) => onProfile(period, patch)} />
            </div>
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
          <Select
            label="Resolution" value={cam.timelapse?.resolution ?? '4k'}
            options={TIMELAPSE_RES_OPTIONS}
            onChange={(resolution) =>
              onCamera({ timelapse: { ...cam.timelapse, resolution } })} />
          {(cam.timelapse?.resolution ?? '4k') === 'full' && (
            <p className="rounded-lg bg-amber-950 p-3 text-sm text-amber-300">
              Full sensor resolution encodes to h264 level 6.0, which phone and
              browser hardware decoders refuse — measured on this rig, a native
              render played back as garbage in both VLC and the browser while
              being a perfectly valid file. It is also several times larger and
              slower to render. Use it only if you are editing the result on a
              desktop.
            </p>
          )}
          <RenderPlan cameraId={id} />
          <p className="text-xs text-zinc-500">
            Resolution is a pixel budget, not a width, so it works out the same
            on a square sensor as on a wide one.
          </p>
        </div>
      </Card>

      <WhiteBalanceCard id={id} cam={cam} onCamera={onCamera} />

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

/* -- security -------------------------------------------------------------- */

/**
 * The access-control card for cameras set up before there was a password, and
 * for changing or removing one afterwards (DESIGN.md, "Access control").
 *
 * One shared password, router-admin tier. Removing it is as easy as setting
 * it, on purpose: a security control you cannot back out of is one people
 * avoid turning on at all.
 */
function SecurityCard({ showToast }) {
  const [state, setState] = useState(null)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    fetch('/api/auth/status').then((r) => r.json()).then(setState).catch(() => {})
  }, [])
  useEffect(() => { refresh() }, [refresh])

  if (!state) return null

  const send = async (body, done) => {
    setBusy(true)
    const r = await fetch('/api/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => null)
    setBusy(false)
    if (!r?.ok) {
      const detail = await r?.json().catch(() => null)
      return showToast?.(detail?.detail || 'Could not change the password')
    }
    setCurrent(''); setNext('')
    refresh()
    showToast?.(done)
  }

  return (
    <Card title="Security">
      <p className="mt-1 text-sm text-zinc-400">
        {state.password_set
          ? 'This camera asks for a password.'
          : 'Anyone on your network can open this page and change settings.'}
        {' '}One shared password, like a router’s admin page — it defends
        against your local network, not the internet, which the camera is never
        exposed to either way.
      </p>

      <div className="mt-4 space-y-3">
        {state.password_set && (
          <input type="password" value={current} placeholder="Current password"
            autoComplete="current-password"
            onChange={(e) => setCurrent(e.target.value)}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900
                       px-3 py-2.5 text-sm" />
        )}
        <input type="password" value={next} autoComplete="new-password"
          placeholder={state.password_set ? 'New password' : 'Set a password'}
          onChange={(e) => setNext(e.target.value)}
          className="w-full rounded-lg border border-zinc-700 bg-zinc-900
                     px-3 py-2.5 text-sm" />

        <Button tone="accent" className="w-full" disabled={busy || !next}
          onClick={() => send({ current, password: next },
                              'Password saved')}>
          {state.password_set ? 'Change password' : 'Set password'}
        </Button>

        {state.password_set && (
          <>
            <label className="flex items-start gap-3 text-sm">
              <Toggle checked={state.public_live_view} label="Public live view"
                onChange={(public_live_view) =>
                  send({ current, public_live_view }, 'Saved')} />
              <span className="text-zinc-400">
                Show the latest frame without a password. Settings and controls
                stay locked.
              </span>
            </label>
            <Button tone="warn" className="w-full" disabled={busy || !current}
              onClick={() => send({ current, password: '' },
                                  'Password removed')}>
              Remove password
            </Button>
            <p className="text-xs text-zinc-500">
              Forgotten it? Clear the entry in /etc/skylapse/config.yaml over
              SSH. It never needs a reflash.
            </p>
          </>
        )}
      </div>

      <div className="mt-5 border-t border-zinc-800 pt-4">
        <Button className="w-full"
          onClick={async () => {
            await fetch('/api/setup/reset', { method: 'POST' }).catch(() => {})
            window.location.href = '/'
          }}>
          Run setup again
        </Button>
        <p className="mt-2 text-xs text-zinc-500">
          Walks the first-run wizard with your current settings filled in.
          Nothing is cleared.
        </p>
      </div>
    </Card>
  )
}

/* -- white balance --------------------------------------------------------- */

const WB_MIN = 0.5
const WB_MAX = 3.0
// Long enough that dragging the slider does not queue a render per pixel of
// travel — each one debayers a frame on a Pi — short enough to feel live.
const WB_PREVIEW_DEBOUNCE_MS = 250

/**
 * Per-camera colour multipliers, green fixed at 1.0.
 *
 * Auto is a seed, not an answer: grey-world assumes the scene averages to
 * grey, which a sky at dusk or a room lit by one warm lamp does not. So the
 * button moves the sliders and stops there, and nothing reaches the camera
 * until Apply.
 */
function WhiteBalanceCard({ id, cam, onCamera }) {
  const applied = { r: cam.wb_r ?? 1.0, b: cam.wb_b ?? 1.0 }
  const [pending, setPending] = useState(applied)
  const [preview, setPreview] = useState(pending)
  const [suggestion, setSuggestion] = useState(null)
  const [noFrame, setNoFrame] = useState(false)

  // The preview lags the slider by a debounce; `pending` is what the numbers
  // and the Apply button read, `preview` is what the image is rendered from.
  useEffect(() => {
    const t = setTimeout(() => setPreview(pending), WB_PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [pending.r, pending.b])

  const dirty = pending.r !== applied.r || pending.b !== applied.b
  const neutral = pending.r === 1.0 && pending.b === 1.0

  const autoSeed = async () => {
    const r = await fetch(`/api/wb/suggest?camera_id=${encodeURIComponent(id)}`)
      .catch(() => null)
    if (!r?.ok) return setSuggestion({ error: true })
    const s = await r.json()
    setSuggestion(s)
    setPending({ r: s.r, b: s.b })
  }

  return (
    <Card title="White balance"
      right={<span className="text-sm text-zinc-500">{cam.label || id}</span>}>
      <p className="mt-1 text-sm text-zinc-400">
        Red and blue multipliers, with green as the reference. Both sensors read
        green high — two of every four photosites are green — so frames arrive
        with a green cast until these are set. RAW/DNG pixels are never changed;
        the multipliers are recorded in the file for your raw editor to apply.
      </p>

      <div className="mt-4 space-y-3">
        <Slider label="Red" value={pending.r} min={WB_MIN} max={WB_MAX} step={0.01}
          display={pending.r.toFixed(2)}
          onChange={(r) => setPending((p) => ({ ...p, r }))} />
        <Slider label="Blue" value={pending.b} min={WB_MIN} max={WB_MAX} step={0.01}
          display={pending.b.toFixed(2)}
          onChange={(b) => setPending((p) => ({ ...p, b }))} />
      </div>

      {!noFrame && (
        <figure className="mt-4">
          <img
            src={`/api/wb/preview?r=${preview.r}&b=${preview.b}`}
            alt="Current frame with the pending white balance applied"
            onError={() => setNoFrame(true)}
            className="w-full rounded-lg bg-zinc-800" />
          <figcaption className="mt-1 text-xs text-zinc-500">
            The most recent frame, rendered with the settings above. Reduced
            resolution — judge colour, not focus.
          </figcaption>
        </figure>
      )}
      {noFrame && (
        <p className="mt-4 rounded-lg bg-zinc-800/60 p-3 text-sm text-zinc-500">
          No frame captured yet, so there is nothing to preview.
        </p>
      )}

      <label className="mt-4 flex items-start justify-between gap-3 text-sm">
        <span>
          <span className="text-zinc-300">Let the camera set it</span>
          <span className="mt-1 block text-xs text-zinc-500">
            Measured every twentieth frame once the exposure has settled, and
            moved a fifth of the way each time so nothing passing can recolour
            a night. Moving either slider below turns this off — a value you
            set by hand should not be quietly overwritten.
          </span>
        </span>
        <Toggle checked={cam.wb_auto ?? true} label="Let the camera set it"
          onChange={(wb_auto) => onCamera({ wb_auto })} />
      </label>

      <div className="mt-4 flex flex-wrap gap-2">
        <Button onClick={autoSeed} className="flex-1">Auto from current frame</Button>
        <Button onClick={() => setPending({ r: 1.0, b: 1.0 })}
          disabled={neutral} className="flex-1">
          Reset to neutral
        </Button>
        <Button tone="accent" disabled={!dirty} className="w-full"
          onClick={() => onCamera({ wb_r: pending.r, wb_b: pending.b,
                                    wb_auto: false })}>
          {dirty ? 'Apply — takes effect from the next frame' : 'Applied'}
        </Button>
      </div>

      {suggestion?.error && (
        <p className="mt-3 text-sm text-amber-400">
          Couldn’t read a frame from this camera to measure.
        </p>
      )}
      {suggestion && !suggestion.error && (
        <p className="mt-3 text-sm text-zinc-500">
          Measured R {suggestion.means.r} / G {suggestion.means.g} /
          B {suggestion.means.b}, suggesting {suggestion.raw_r.toFixed(2)} and{' '}
          {suggestion.raw_b.toFixed(2)}.
          {suggestion.clamped
            ? ' That is beyond the slider range and has been limited — worth'
              + ' checking the lens and lighting before accepting it.'
            : ' A starting point: it assumes the scene averages to grey, so'
              + ' adjust from here on what you can see.'}
        </p>
      )}
    </Card>
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

function UpdateCard({ cfg, showToast, onChannel, onAutoCheck }) {
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
      ) : info && info.auto_check === false && !info.checked_at ? (
        /* Never say "up to date" about a check that has not happened. */
        <p className="mt-1 text-sm text-zinc-400">
          Not checked. Press Check now when you want to look.
        </p>
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
          <span className="text-zinc-300">Check automatically</span>
          <span className="mt-1 block text-xs text-zinc-500">
            Look for a new version once a day. Turn it off and the camera only
            asks when you press Check now.
          </span>
        </span>
        <Toggle checked={cfg.updates?.auto_check ?? true} label="Check automatically"
          onChange={onAutoCheck} />
      </label>

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
