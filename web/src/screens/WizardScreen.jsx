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
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card, Select, Toggle } from '../components/ui.jsx'
import {
  canContinue, canGoBack, formatCoords, nextStep, prevStep, resolveLocation,
  startsWhen, STEPS, stepIndex,
} from '../lib/wizard.js'

const SCHEDULE_OPTIONS = [
  { value: 'always', label: '24/7 — day and night' },
  { value: 'night_only', label: 'Night only — idle while the sun is up' },
]

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

function Network({ setError }) {
  const [net, setNet] = useState(null)
  const [scan, setScan] = useState(null)
  const [chosen, setChosen] = useState('')
  const [password, setPassword] = useState('')
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    fetch('/api/network').then((r) => r.json()).then(setNet).catch(() => setNet({}))
  }, [])

  const openScan = async () => {
    setExpanded(true)
    const r = await fetch('/api/network/scan').catch(() => null)
    setScan(r?.ok ? await r.json() : [])
  }

  const join = async () => {
    const r = await fetch('/api/network/join', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid: chosen, password }),
    }).catch(() => null)
    // netwatch may not be running — it is disabled on installs that have not
    // verified it. Say so plainly rather than leaving a button that hangs.
    setError(r?.ok
      ? ''
      : 'Couldn’t hand that to the network service. It may not be running on '
        + 'this camera — you can set Wi-Fi up later in Settings.')
  }

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

      {!expanded ? (
        <button onClick={openScan}
          className="mt-4 text-sm text-sky-400 underline">
          Use a different network
        </button>
      ) : (
        <div className="mt-4 space-y-3">
          {scan === null && <p className="text-sm text-zinc-500">Scanning…</p>}
          {scan?.length === 0 && (
            <p className="text-sm text-zinc-500">No networks found.</p>
          )}
          {scan?.length > 0 && (
            <Select label="Network" value={chosen} onChange={setChosen}
              options={[{ value: '', label: 'Choose a network…' },
                        ...scan.map((n) => ({ value: n.ssid, label: n.ssid }))]} />
          )}
          {chosen && (
            <input type="password" value={password} placeholder="Wi-Fi password"
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900
                         px-3 py-2.5 text-sm" />
          )}
          {chosen && (
            <Button onClick={join} className="w-full">Join {chosen}</Button>
          )}
        </div>
      )}

      <p className="mt-6 text-sm text-zinc-500">
        You can also run it with no network at all. Capture, storage and the
        timelapse all work; you just reach the camera by joining its own Wi-Fi
        instead of yours.
      </p>
    </>
  )
}

/* -- 4. camera ------------------------------------------------------------- */

function Camera({ draft, patch }) {
  const [info, setInfo] = useState(null)
  const [shotAt, setShotAt] = useState(null)
  const [waiting, setWaiting] = useState(false)
  const polls = useRef(0)

  const refresh = useCallback(async () => {
    const r = await fetch('/api/setup/camera').catch(() => null)
    if (r?.ok) setInfo(await r.json())
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const takeShot = async () => {
    setWaiting(true)
    polls.current = 0
    await fetch('/api/setup/camera/test', { method: 'POST' }).catch(() => {})
    // The daemon picks the request up between frames, so this can take a gap.
    const timer = setInterval(async () => {
      polls.current += 1
      const r = await fetch('/api/setup/camera').catch(() => null)
      const next = r?.ok ? await r.json() : null
      if (next?.shot_at && next.shot_at !== info?.shot_at) {
        setShotAt(next.shot_at)
        setWaiting(false)
        clearInterval(timer)
      } else if (polls.current > 40) {         // ~2 minutes
        setWaiting(false)
        clearInterval(timer)
      }
    }, 3000)
  }

  const cameras = info?.cameras ?? []
  const found = cameras.length > 0 || info?.detected

  return (
    <>
      <Heading title="Camera">
        {found ? 'Here’s what Skylapse can see.' : 'No camera detected yet.'}
      </Heading>

      {found ? (
        <div className="mt-6 space-y-4">
          {cameras.length > 1 ? (
            <Select label="Which camera is pointed at the sky?"
              value={draft.camera?.camera_id || info.active || cameras[0].camera_id}
              onChange={(camera_id) => patch({ camera: { camera_id } })}
              options={cameras.map((c) => ({ value: c.camera_id, label: c.label }))} />
          ) : (
            <p className="rounded-xl border border-zinc-800 bg-zinc-900 p-4
                          text-emerald-300">
              {cameras[0]?.label || info.detected}
            </p>
          )}

          {shotAt && (
            <figure>
              <img src={`/api/setup/camera/shot?t=${shotAt}`} alt="Test shot"
                className="w-full rounded-lg bg-zinc-800" />
              <figcaption className="mt-1 text-xs text-zinc-500">
                A real frame, taken just now. It isn’t saved to your night.
              </figcaption>
            </figure>
          )}

          <Button onClick={takeShot} disabled={waiting} className="w-full">
            {waiting ? 'Taking a shot…' : shotAt ? 'Retake' : 'Take a test shot'}
          </Button>
        </div>
      ) : (
        <div className="mt-6 space-y-3 text-sm text-zinc-400">
          <p>Worth checking, in this order:</p>
          <ul className="list-disc space-y-2 pl-5">
            <li><b>USB camera:</b> seated firmly, and on the official power
              supply. Underpowering a USB3 camera looks like a missing camera.</li>
            <li><b>Ribbon cable:</b> contacts the right way round at
              <i> both</i> ends. It is easy to get right at one and wrong at the
              other.</li>
            <li><b>Pull the power for 15 seconds.</b> Not a reboot — a full cold
              start. Some modules only come up after one, and this is the step
              most often mistaken for a broken cable.</li>
          </ul>
          <Button onClick={refresh} className="w-full">Scan again</Button>
        </div>
      )}
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
    ['Password', summary.protected ? 'Set' : 'Not set'],
  ]

  return (
    <>
      <Heading title="That’s it.">
        {startsWhen({ period: status?.daemon?.state === 'idle_day' ? 'day' : 'night',
                      sunset: status?.daemon?.dusk,
                      now: status?.server_time })}
      </Heading>
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
