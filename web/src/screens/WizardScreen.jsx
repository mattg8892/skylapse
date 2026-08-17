// The setup wizard. One question per screen, big targets, progress dots —
// this is a phone in a shed, usually at dusk, usually by someone who has just
// finished mounting the thing and would like it to work now.
//
// Every screen commits its own piece to a server-side draft as it goes, so a
// locked phone or a dropped connection costs nothing. The config is written
// once, at the end, in a single atomic save (DESIGN.md guard 4).
//
// The hotspot/captive-portal entry path is deliberately not built yet — it
// waits on netwatch being radio-verified. The network screen is shaped so that
// lands as an addition rather than a rewrite.
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Select, Toggle } from '../components/ui.jsx'
import JoinNetwork from '../components/JoinNetwork.jsx'
import { CameraPanel, useCameras } from '../components/camera.jsx'
import {
  canContinue, canGoBack, formatCoords, nextStep, prevStep, resolveLocation,
  startsWhen, STEPS, stepIndex,
} from '../lib/wizard.js'

const SCHEDULE_OPTIONS = [
  { value: 'always', label: '24/7 — day and night' },
  { value: 'night_only', label: 'Night only — idle while the sun is up' },
]

// Enough of the world to cover the overwhelming majority of users, ordered by
// where Pis actually get sold. The Wi-Fi radio may not legally transmit until
// one of these is set, so it is not an optional nicety — an unset country is a
// camera that cannot serve the access point its own setup runs on.
const COUNTRIES = [
  ['US', 'United States'], ['GB', 'United Kingdom'], ['DE', 'Germany'],
  ['FR', 'France'], ['NL', 'Netherlands'], ['CA', 'Canada'],
  ['AU', 'Australia'], ['NZ', 'New Zealand'], ['IE', 'Ireland'],
  ['IT', 'Italy'], ['ES', 'Spain'], ['SE', 'Sweden'], ['NO', 'Norway'],
  ['FI', 'Finland'], ['DK', 'Denmark'], ['PL', 'Poland'], ['CH', 'Switzerland'],
  ['AT', 'Austria'], ['BE', 'Belgium'], ['PT', 'Portugal'], ['CZ', 'Czechia'],
  ['JP', 'Japan'], ['IN', 'India'], ['BR', 'Brazil'], ['ZA', 'South Africa'],
  ['MX', 'Mexico'], ['SG', 'Singapore'],
].map(([value, label]) => ({ value, label: `${label} (${value})` }))

const RAW_OPTIONS = [
  { value: 'off', label: 'Keepers only (recommended)' },
  { value: 'every_frame', label: 'Every frame' },
]

export default function WizardScreen({ preview = false, onDone }) {
  const [draft, setDraft] = useState(null)
  const [step, setStep] = useState('welcome')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch('/api/setup/draft')
      .then((r) => r.json())
      .then(({ draft: d }) => { setDraft(d); setStep(d?.step || 'welcome') })
      .catch(() => setDraft({}))
  }, [])

  /** Commit this screen's piece. Section-wise, so Back never blanks anything. */
  const patch = useCallback(async (piece) => {
    setDraft((current) => mergeLocal(current, piece))
    if (preview) return                      // dev walkthrough writes nothing
    await fetch('/api/setup/draft', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(piece),
    }).catch(() => {})
  }, [preview])

  const go = async (target) => {
    setError('')
    await patch({ step: target })
    setStep(target)
    window.scrollTo(0, 0)
  }

  const finish = async () => {
    if (preview) return onDone?.()
    setBusy(true)
    const r = await fetch('/api/setup/complete', { method: 'POST' })
      .catch(() => null)
    setBusy(false)
    if (!r?.ok) {
      const detail = await r?.json().catch(() => null)
      return setError(detail?.detail || 'Could not save your settings.')
    }
    const { summary } = await r.json()
    setDraft((current) => ({ ...current, summary }))
    setStep('done')
  }

  if (!draft) return <Splash>Starting setup…</Splash>

  const Screen = SCREENS[step] ?? Welcome
  const last = step === 'done'

  return (
    <div className="mx-auto min-h-screen max-w-md px-4 py-8">
      <Dots step={step} />
      {preview && (
        <p className="mt-3 rounded-lg bg-sky-950 px-3 py-2 text-center text-xs text-sky-300">
          Preview — nothing you do here is saved.
        </p>
      )}

      <div className="mt-6">
        <Screen draft={draft} patch={patch} setError={setError} />
      </div>

      {error && (
        <p className="mt-4 rounded-lg bg-red-950 p-3 text-sm text-red-300">{error}</p>
      )}

      <div className="mt-8 flex gap-2">
        {canGoBack(step) && !last && (
          <Button onClick={() => go(prevStep(step))} className="flex-1">Back</Button>
        )}
        {last ? (
          <Button tone="accent" className="w-full" onClick={() => onDone?.()}>
            Open the dashboard
          </Button>
        ) : step === 'notifications' ? (
          <Button tone="accent" className="flex-1" disabled={busy} onClick={finish}>
            {busy ? 'Saving…' : 'Finish setup'}
          </Button>
        ) : (
          <Button tone="accent" className="flex-1"
            disabled={!canContinue(step, draft)}
            onClick={() => go(nextStep(step))}>
            {step === 'welcome' ? 'Start' : 'Continue'}
          </Button>
        )}
      </div>
    </div>
  )
}

function mergeLocal(current, piece) {
  const merged = { ...current }
  for (const [key, value] of Object.entries(piece)) {
    merged[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? { ...(merged[key] ?? {}), ...value }
      : value
  }
  return merged
}

function Splash({ children }) {
  return (
    <div className="grid min-h-screen place-items-center text-zinc-400">{children}</div>
  )
}

function Dots({ step }) {
  const at = stepIndex(step)
  return (
    <div className="flex justify-center gap-1.5" aria-label={`Step ${at + 1} of ${STEPS.length}`}>
      {STEPS.map((name, i) => (
        <span key={name}
          className={`h-1.5 rounded-full transition-all ${
            i === at ? 'w-6 bg-sky-400' : i < at ? 'w-1.5 bg-sky-800' : 'w-1.5 bg-zinc-700'}`} />
      ))}
    </div>
  )
}

function Heading({ title, children }) {
  return (
    <>
      <h1 className="text-2xl font-medium">{title}</h1>
      {children && <p className="mt-2 text-zinc-400">{children}</p>}
    </>
  )
}

/* -- 2. welcome ------------------------------------------------------------ */

function Welcome() {
  const [status, setStatus] = useState(null)
  useEffect(() => {
    fetch('/api/status').then((r) => r.json()).then(setStatus).catch(() => {})
  }, [])

  // The browser already offered its clock on app start (App.jsx). This surfaces
  // what that machinery did rather than asking again — a Pi has no
  // battery-backed clock, so a fresh one boots in the past and says so.
  const cameraTime = status?.server_time
    ? new Date(status.server_time * 1000)
    : null
  const drift = cameraTime ? Math.abs(Date.now() - cameraTime.getTime()) / 1000 : 0

  return (
    <>
      <p className="text-sm uppercase tracking-widest text-sky-400">Skylapse</p>
      <Heading title="Let’s point this at the sky.">
        Six short questions. You can change any of it later.
      </Heading>
      {cameraTime && (
        <p className="mt-6 text-sm text-zinc-500">
          Camera time: {cameraTime.toLocaleString()}
          {drift < 5 && (
            <span className="block text-emerald-400">Clock synced from this phone.</span>
          )}
        </p>
      )}
    </>
  )
}

/* -- 3. network ------------------------------------------------------------ */

/**
 * A first guess at the Wi-Fi country, from the phone's own locale.
 *
 * The phone is in the same room as the camera, so its region is a far better
 * default than any constant — and it is only a default: the control is right
 * there, pre-filled rather than pre-decided.
 */
function guessCountry() {
  try {
    const region = new Intl.Locale(navigator.language).region
    if (region && COUNTRIES.some((c) => c.value === region)) return region
  } catch { /* older browsers: fall through */ }
  return 'US'
}


function Network({ draft, patch, setError }) {
  const [net, setNet] = useState(null)
  const [expanded, setExpanded] = useState(false)
  const mode = draft.network?.mode === 'standalone' ? 'standalone' : 'auto'

  useEffect(() => {
    fetch('/api/network').then((r) => r.json()).then(setNet).catch(() => setNet({}))
  }, [])

  const online = net?.state === 'connected' || net?.ssid
  return (
    <>
      <Heading title="Network">
        Skylapse keeps capturing whether or not it has a network. This is just
        how you reach it.
      </Heading>

      <div className="mt-6 rounded-xl border border-zinc-800 bg-zinc-900 p-4">
        {online ? (
          <p className="text-emerald-300">
            Connected to {net.ssid || 'your network'}
          </p>
        ) : net?.access_point ? (
          <p className="text-amber-300">
            You’re on the camera’s own network, {net.hotspot_ssid}.
          </p>
        ) : (
          <p className="text-zinc-400">Not connected to Wi-Fi.</p>
        )}
      </div>

      <div className="mt-4">
        <Select label="Wi-Fi country"
          value={draft.network?.country || guessCountry()}
          options={COUNTRIES}
          onChange={(country) => patch({ network: { country } })} />
        <p className="mt-1 text-xs text-zinc-500">
          Radio regulations differ by country, and the camera may not transmit
          at all until this is set — including the network it serves for setup.
        </p>
      </div>

      <div className="mt-4 space-y-2">
        <Choice checked={mode === 'auto'}
          label={online ? `Stay on ${net.ssid}` : 'Connect to a Wi-Fi network'}
          detail={online
            ? 'The camera keeps using this network, and falls back to its own if yours ever disappears.'
            : 'Pick your network below. The camera falls back to its own if yours ever disappears.'}
          onSelect={() => patch({ network: { mode: 'auto' } })} />
        <Choice checked={mode === 'standalone'} label="Don’t use Wi-Fi at all"
          detail="The camera always serves its own network and never looks for
                  yours. For a shed with no coverage, or a dark-sky site."
          onSelect={() => patch({ network: { mode: 'standalone' } })} />
      </div>

      {mode === 'standalone' ? (
        <div className="mt-4 space-y-2">
          <p className="rounded-lg bg-amber-950 p-3 text-sm text-amber-300">
            {online
              ? `When you finish setup, the camera will leave ${net.ssid} and
                 serve its own. To reach it again, join
                 ${net.hotspot_ssid || 'Skylapse-Setup'} from your Wi-Fi settings
                 and open http://10.42.0.1`
              : `The camera keeps serving
                 ${net?.hotspot_ssid || 'Skylapse-Setup'} and never looks for
                 yours. You stay right where you are.`}
          </p>
          <p className="text-sm text-zinc-500">
            Capture, storage, RAW and the timelapse all work exactly the same
            with no network. You can switch to Wi-Fi later in Settings.
          </p>
        </div>
      ) : online ? (
        // Already on a network: the list is a detour, so it stays behind a link.
        !expanded ? (
          <button onClick={() => setExpanded(true)}
            className="mt-4 text-sm text-sky-400 underline">
            Use a different network
          </button>
        ) : (
          <div className="mt-4"><JoinNetwork onJoined={() => setError('')} /></div>
        )
      ) : (
        // Not on a network — which is the whole reason someone is reading this
        // over the camera's own access point. The list of networks IS the
        // screen; hiding it behind "use a different network" asks them to go
        // looking for the one thing they came here to do.
        <div className="mt-4">
          <p className="mb-2 text-sm text-zinc-400">Choose your network:</p>
          <JoinNetwork warnAboutDisconnect={net?.access_point}
            onJoined={() => setError('')} />
        </div>
      )}
    </>
  )
}

/** A big tappable radio row. Thumb-sized, because this is a phone. */
function Choice({ checked, label, detail, onSelect }) {
  return (
    <button onClick={onSelect} aria-pressed={checked}
      className={`w-full rounded-xl border p-4 text-left transition ${
        checked ? 'border-sky-500 bg-sky-600/10' : 'border-zinc-800 bg-zinc-900'}`}>
      <span className="flex items-start gap-3">
        <span className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center
                          rounded-full border-2 ${
          checked ? 'border-sky-400' : 'border-zinc-600'}`}>
          {checked && <span className="h-2.5 w-2.5 rounded-full bg-sky-400" />}
        </span>
        <span>
          <span className="block text-sm">{label}</span>
          <span className="mt-0.5 block text-sm text-zinc-500">{detail}</span>
        </span>
      </span>
    </button>
  )
}


/* -- 4. camera ------------------------------------------------------------- */

/**
 * Detection, a test shot, and the two ways to add a camera the Pi cannot see.
 *
 * All of it lives in components/camera.jsx, because the settings screen offers
 * exactly the same things to an installed camera — before that it had no
 * reliable way to add one at all, just plug it in and hope the daemon noticed.
 * The only difference here is that the choice goes into the wizard's draft
 * rather than straight to config.
 */
function Camera({ draft, patch }) {
  const [info, refresh] = useCameras()
  const found = (info?.cameras?.length ?? 0) > 0 || info?.detected

  return (
    <>
      <Heading title="Camera">
        {found ? 'Here’s what Skylapse can see.' : 'No camera detected yet.'}
      </Heading>
      <div className="mt-6">
        <CameraPanel info={info} refresh={refresh}
          selected={draft.camera?.camera_id}
          onSelect={(camera_id) => patch({ camera: { camera_id } })} />
      </div>
    </>
  )
}


/* -- 5. location ----------------------------------------------------------- */

function Location({ draft, patch }) {
  const [derived, setDerived] = useState(null)
  const [status, setStatus] = useState('')
  const [manual, setManual] = useState(false)
  const location = draft.location ?? {}

  const check = useCallback(async (lat, lon) => {
    if (typeof lat !== 'number' || typeof lon !== 'number') return
    const r = await fetch(
      `/api/setup/location/check?latitude=${lat}&longitude=${lon}`).catch(() => null)
    if (r?.ok) setDerived(await r.json())
  }, [])

  useEffect(() => {
    check(location.latitude, location.longitude)
  }, [location.latitude, location.longitude, check])

  const locate = async () => {
    setStatus('Locating…')
    const result = await resolveLocation({
      browser: () => new Promise((resolve, reject) =>
        navigator.geolocation.getCurrentPosition(resolve, reject,
                                                 { timeout: 10000 })),
      ip: async () => {
        const r = await fetch('/api/setup/location/estimate')
        if (!r.ok) throw new Error('no estimate')
        return r.json()
      },
    })
    if (result.source === 'manual') {
      setStatus(`${result.reason} Enter it by hand below.`)
      setManual(true)
      return
    }
    setStatus(result.approximate
      ? `Approximate — from this camera’s internet connection${
          result.place ? ` (${result.place})` : ''}.`
      : 'Located from this phone.')
    patch({ location: result })
  }

  const setField = (field) => (event) => {
    const value = event.target.value
    patch({ location: { [field]: value === '' ? null : Number(value),
                        source: 'manual' } })
  }

  return (
    <>
      <Heading title="Where is it?">
        Day and night scheduling and aurora alerts both depend on this. Nothing
        is sent anywhere — it stays on the camera.
      </Heading>

      <div className="mt-6 space-y-4">
        <Button tone="accent" onClick={locate} className="w-full">
          Use my location
        </Button>
        {status && <p className="text-sm text-zinc-400">{status}</p>}

        {(manual || location.source === 'manual') ? (
          <div className="grid grid-cols-2 gap-3">
            <label className="text-sm">
              <span className="text-zinc-400">Latitude</span>
              <input type="number" step="0.00001" inputMode="decimal"
                value={location.latitude ?? ''} onChange={setField('latitude')}
                className="mt-1 w-full rounded-lg border border-zinc-700
                           bg-zinc-900 px-3 py-2.5 text-sm" />
            </label>
            <label className="text-sm">
              <span className="text-zinc-400">Longitude</span>
              <input type="number" step="0.00001" inputMode="decimal"
                value={location.longitude ?? ''} onChange={setField('longitude')}
                className="mt-1 w-full rounded-lg border border-zinc-700
                           bg-zinc-900 px-3 py-2.5 text-sm" />
            </label>
          </div>
        ) : (
          <button onClick={() => setManual(true)}
            className="text-sm text-sky-400 underline">
            Enter coordinates by hand
          </button>
        )}

        {formatCoords(location.latitude, location.longitude) && (
          <Card>
            <p className="font-mono text-sm text-zinc-300">
              {formatCoords(location.latitude, location.longitude)}
            </p>
            {derived?.null_island && (
              <p className="mt-2 text-sm text-amber-400">
                That’s 0°, 0° — a spot in the Gulf of Guinea, and what an unset
                camera reads. Almost certainly not where you are.
              </p>
            )}
            {derived?.out_of_range && (
              <p className="mt-2 text-sm text-amber-400">
                Those coordinates aren’t on Earth.
              </p>
            )}
            {derived && !derived.null_island && !derived.out_of_range && (
              <dl className="mt-3 space-y-1 text-sm text-zinc-400">
                <div className="flex justify-between">
                  <dt>Sunset tonight</dt>
                  <dd className="text-zinc-200">
                    {derived.sunset
                      ? new Date(derived.sunset * 1000).toLocaleTimeString(
                          [], { hour: 'numeric', minute: '2-digit' })
                      : 'the sun doesn’t set here'}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt>Aurora alerts above</dt>
                  <dd className="text-zinc-200">Kp {derived.kp_threshold}</dd>
                </div>
              </dl>
            )}
          </Card>
        )}
      </div>
    </>
  )
}

/* -- 6. capture defaults --------------------------------------------------- */

function Capture({ draft, patch }) {
  const capture = draft.capture ?? {}
  const everyFrame = capture.raw_mode === 'every_frame'
  return (
    <>
      <Heading title="What should it capture?">
        Sensible defaults are already chosen. You can change all of this later
        in Settings.
      </Heading>

      <div className="mt-6 space-y-4">
        <Select label="Schedule" value={capture.schedule ?? 'always'}
          options={SCHEDULE_OPTIONS}
          onChange={(schedule) => patch({ capture: { schedule } })} />
        <Select label="RAW files" value={capture.raw_mode ?? 'off'}
          options={RAW_OPTIONS}
          onChange={(raw_mode) => patch({ capture: { raw_mode } })} />

        {everyFrame ? (
          <p className="rounded-lg bg-amber-950 p-3 text-sm text-amber-300">
            Measured on a 12 MP camera, every-frame RAW is about <b>37 GB a
            night</b>. That is sustained write volume a microSD card is not
            built for — they fail suddenly, months in, with no warning. Use a
            high-endurance card or an external SSD if you choose this.
          </p>
        ) : (
          <p className="text-sm text-zinc-500">
            Keepers-only still gives you the Save-RAW button on the dashboard,
            so you can grab the last few frames the moment something happens.
          </p>
        )}
      </div>
    </>
  )
}

/* -- 7. security ----------------------------------------------------------- */

function Security({ draft, patch }) {
  const security = draft.security ?? {}
  const [confirm, setConfirm] = useState('')
  const password = security.password ?? ''
  const mismatch = password && confirm && password !== confirm

  return (
    <>
      <Heading title="Protect this camera?">
        Optional, and off by default. One shared password, like a router admin
        page — it keeps other people on your network out of the settings.
      </Heading>

      <div className="mt-6 space-y-3">
        <input type="password" value={password} placeholder="Password (leave blank to skip)"
          onChange={(e) => patch({ security: { password: e.target.value } })}
          className="w-full rounded-lg border border-zinc-700 bg-zinc-900
                     px-3 py-2.5 text-sm" />
        {password && (
          <input type="password" value={confirm} placeholder="Confirm password"
            onChange={(e) => setConfirm(e.target.value)}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900
                       px-3 py-2.5 text-sm" />
        )}
        {mismatch && (
          <p className="text-sm text-amber-400">Those don’t match.</p>
        )}
        {password && (
          <label className="flex items-start gap-3 text-sm">
            <Toggle checked={security.public_live_view ?? false}
              label="Public live view"
              onChange={(public_live_view) => patch({ security: { public_live_view } })} />
            <span className="text-zinc-400">
              Let anyone on your network see the latest frame without the
              password. Settings and controls stay locked.
            </span>
          </label>
        )}
        <p className="text-sm text-zinc-500">
          If you forget it, you can clear it over SSH — it never needs a
          reflash. The camera is never exposed to the internet either way.
        </p>
      </div>
    </>
  )
}

/* -- 8. notifications ------------------------------------------------------ */

function Notifications({ draft, patch }) {
  const [topic, setTopic] = useState(null)
  const [sent, setSent] = useState('')
  const enabled = draft.notifications?.enabled ?? false

  const setup = async () => {
    const r = await fetch('/api/notify/generate-topic', { method: 'POST' })
      .catch(() => null)
    if (r?.ok) setTopic(await r.json())
  }

  const test = async () => {
    const r = await fetch('/api/notify/test', { method: 'POST' }).catch(() => null)
    const body = r?.ok ? await r.json() : null
    setSent(body?.sent ? 'Sent — check your phone.' : 'Couldn’t send it.')
  }

  return (
    <>
      <Heading title="Phone alerts?">
        Free, optional, and off unless you turn them on. Skylapse tells you when
        capture stops — and again when it recovers.
      </Heading>

      <div className="mt-6 space-y-4">
        <label className="flex items-center justify-between">
          <span className="text-sm text-zinc-300">Send me alerts</span>
          <Toggle checked={enabled} label="Enable alerts"
            onChange={(value) => {
              patch({ notifications: { enabled: value } })
              if (value && !topic) setup()
            }} />
        </label>

        {enabled && topic && (
          <div className="space-y-3">
            <p className="text-sm text-zinc-400">
              Install the free ntfy app, then scan this or subscribe to the
              topic by hand:
            </p>
            {topic.qr_svg && (
              <div className="flex justify-center rounded-lg bg-white p-4"
                dangerouslySetInnerHTML={{ __html: topic.qr_svg }} />
            )}
            <code className="block break-all rounded-lg bg-zinc-800/60 p-3
                             text-sm text-sky-400">
              {topic.topic}
            </code>
            <p className="text-xs text-zinc-500">
              That topic name is the only thing keeping your alerts private.
              Treat it like a password.
            </p>
            <Button onClick={test} className="w-full">Send a test</Button>
            {sent && <p className="text-sm text-zinc-400">{sent}</p>}
          </div>
        )}
      </div>
    </>
  )
}

/* -- 9. done --------------------------------------------------------------- */

function Done({ draft }) {
  const [status, setStatus] = useState(null)
  const summary = draft.summary ?? {}

  useEffect(() => {
    fetch('/api/status').then((r) => r.json()).then(setStatus).catch(() => {})
  }, [])

  const rows = [
    ['Camera', summary.camera || '—'],
    ['Location', formatCoords(summary.latitude, summary.longitude) || '—'],
    ['Schedule', summary.schedule === 'night_only' ? 'Night only' : '24/7'],
    ['RAW', summary.raw_mode === 'every_frame' ? 'Every frame' : 'Keepers only'],
    ['Network', summary.network_mode === 'standalone'
      ? `Its own (${summary.hotspot_ssid || 'Skylapse-Setup'})` : 'Wi-Fi'],
    ['Password', summary.protected ? 'Set' : 'Not set'],
  ]

  return (
    <>
      <Heading title="That’s it.">
        {startsWhen({ period: status?.daemon?.state === 'idle_day' ? 'day' : 'night',
                      sunset: status?.daemon?.dusk,
                      now: status?.server_time })}
      </Heading>
      {summary.network_mode === 'standalone' && (
        <p className="mt-4 rounded-lg bg-amber-950 p-3 text-sm text-amber-300">
          This camera serves its own network. If you lose this page, join{' '}
          {summary.hotspot_ssid || 'Skylapse-Setup'} from your Wi-Fi settings
          and open http://10.42.0.1
        </p>
      )}
      <Card className="mt-6">
        <dl className="divide-y divide-zinc-800">
          {rows.map(([label, value]) => (
            <div key={label} className="flex justify-between py-2.5 text-sm">
              <dt className="text-zinc-400">{label}</dt>
              <dd className="text-zinc-200">{value}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </>
  )
}

const SCREENS = {
  welcome: Welcome,
  network: Network,
  camera: Camera,
  location: Location,
  capture: Capture,
  security: Security,
  notifications: Notifications,
  done: Done,
}
