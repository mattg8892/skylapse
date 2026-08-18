// Dashboard: latest frame, live capture status, and the actions that only make
// sense while standing at the camera — keeper RAW, focus assist, re-render.
import { useEffect, useState } from 'react'
import SettingsScreen from './SettingsScreen.jsx'
import NightsScreen from './NightsScreen.jsx'
import FocusScreen from './FocusScreen.jsx'
import { Button, Card, Toast, useToast } from '../components/ui.jsx'
import {
  countdownFor, countdownText, idleDetail, pillFor, serverNow,
} from '../lib/capture.js'
import { networkBadge } from '../lib/network.js'

// One entry per navigable screen. Focus is deliberately absent: it is entered
// from the dashboard, not navigated to, and leaving it must stop the session.
const NAV = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'nights', label: 'Nights' },
  { id: 'settings', label: 'Settings' },
]

export default function Dashboard({ status }) {
  // A single source of truth for which screen is showing. The previous version
  // gave each nav button its own toggle, so a click could take you off the
  // current screen without landing on the target — hence the double-clicking.
  const [screen, setScreen] = useState('dashboard')
  const [toast, showToast] = useToast()
  const d = status.daemon ?? {}
  const current = status.current ?? {}
  // A camera serving its own access point looks identical whether someone
  // chose that or Wi-Fi failed, so the badge has to say which.
  const badge = networkBadge(status.network, status.server_time)

  return (
    <div className="mx-auto min-h-screen max-w-3xl px-4 py-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">Skylapse</h1>
          <LiveStatus status={status} />
        </div>
        <div className="flex shrink-0 items-center gap-3 text-sm">
          {badge && (
            <span title={badge.detail}
              className={`rounded-full px-3 py-1 ${badge.tone === 'red'
                ? 'bg-red-950 text-red-400' : 'bg-amber-950 text-amber-400'}`}>
              {badge.label}
            </span>
          )}
          <span className="text-zinc-400">
            {new Date(status.server_time * 1000).toLocaleTimeString()}
          </span>
        </div>
      </header>

      {/* Every destination is always present and one click always switches. */}
      <nav className="mt-4 flex gap-2 text-sm">
        {NAV.map((item) => (
          <button key={item.id} onClick={() => setScreen(item.id)}
            aria-current={screen === item.id ? 'page' : undefined}
            className={`rounded-lg border px-3 py-1.5 transition ${
              screen === item.id
                ? 'border-sky-500 bg-sky-600/20 text-sky-300'
                : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'}`}>
            {item.label}
          </button>
        ))}
      </nav>

      {/* Exactly one screen renders. */}
      {screen === 'settings' && (
        <SettingsScreen showToast={showToast} storage={status.storage} />
      )}

      {screen === 'nights' && (
        <NightsScreen cameraId={current.camera_id} showToast={showToast}
          onBack={() => setScreen('dashboard')} />
      )}

      {screen === 'focus' && (
        <FocusScreen showToast={showToast} onExit={() => setScreen('dashboard')} />
      )}

      {screen === 'dashboard' && (
        <main className="mt-6 flex flex-col gap-5">
          <LatestFrame daemon={d} status={status} showToast={showToast}
            onOpenFocus={() => setScreen('focus')} />
          <SafetyBanner daemon={d} showToast={showToast} />
          <Card title="Focus assist">
            <p className="mt-1 text-sm text-zinc-400">
              A live view with zoom, exposure and gain controls, and a sharpness
              score to chase. Nothing is written to the card while focusing, and
              the session exits by itself after 15 minutes.
            </p>
            <Button onClick={() => setScreen('focus')} className="mt-4 w-full">
              Start focus assist
            </Button>
          </Card>
          <StorageCard storage={status.storage} />
        </main>
      )}

      <Toast message={toast} />
    </div>
  )
}

/* -- live status pill ------------------------------------------------------ */

const PILL_CLASS = {
  green: 'bg-emerald-950 text-emerald-400',
  amber: 'bg-amber-950 text-amber-400',
  red: 'bg-rose-950 text-rose-400',
  blue: 'bg-indigo-950 text-indigo-300',
  sky: 'bg-sky-950 text-sky-300',
  zinc: 'bg-zinc-800 text-zinc-400',
}

function LiveStatus({ status }) {
  // Ticks locally every second so the countdown moves, and resyncs to the
  // server clock on every status poll — the browser's own clock is not
  // trustworthy relative to a Pi that steps its time at boot.
  const [now, setNow] = useState(() => serverNow(status))

  useEffect(() => {
    setNow(serverNow(status))          // resync on each new status
  }, [status])

  useEffect(() => {
    const id = setInterval(() => setNow((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  const pill = pillFor(status, now)
  const countdown = countdownText(countdownFor(status, now))
  const dusk = pill.key === 'idle_day' ? idleDetail(status) : null
  const frames = status?.capture?.frames_tonight

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
      <span className={`rounded-full px-2.5 py-0.5 ${PILL_CLASS[pill.tone]}`}>
        <span aria-hidden="true">●</span> {pill.label}{dusk ? ` at ${dusk}` : ''}
      </span>
      {status?.current?.camera_name && (
        <span className="text-zinc-500">{status.current.camera_name}</span>
      )}
      {countdown && <span className="tabular-nums text-zinc-500">{countdown}</span>}
      {frames > 0 && (
        <span className="text-zinc-500">
          {frames.toLocaleString()} frame{frames === 1 ? '' : 's'} tonight
        </span>
      )}
    </div>
  )
}

/* -- latest frame + live status ------------------------------------------- */

function LatestFrame({ daemon: d, status, showToast, onOpenFocus }) {
  // Defense in depth for the focus-escape bug. If the daemon is in focus mode —
  // another device, a closed tab, a race — the stored frame is stale and will
  // never update. Showing "waiting for first frame" there is a lie that looks
  // like a broken camera, so say what is actually happening and offer a way out.
  if (d.state === 'focusing') {
    return <FocusElsewhere showToast={showToast} onOpenFocus={onOpenFocus} />
  }

  const depth = status?.current?.keeper_depth ?? 3

  return (
    <section>
      {d.latest ? (
        <img src={`/api/latest?t=${d.updated}`} alt="Latest sky frame"
          className="w-full rounded-2xl border border-zinc-800" />
      ) : (
        <div className="grid h-64 place-items-center rounded-2xl border border-dashed
                        border-zinc-800 text-zinc-500">
          {d.state === 'no_camera' ? 'No camera detected' : 'Waiting for first frame…'}
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
        <StatusLine daemon={d} status={status} />
        <div className="shrink-0">
          <KeeperButton depth={depth} showToast={showToast} />
          <p className="mt-1 max-w-[16rem] text-xs text-zinc-500">
            Grabs the most recent frames as editable RAW — for when a meteor or
            something cool just happened.
          </p>
        </div>
      </div>
    </section>
  )
}

function FocusElsewhere({ showToast, onOpenFocus }) {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 2000)
    return () => clearInterval(id)
  }, [])

  const stop = async () => {
    await fetch('/api/focus/stop', { method: 'POST' }).catch(() => {})
    showToast('Focus mode stopped — capture resuming')
  }

  return (
    <Card title="Focus mode active">
      <p className="mt-1 text-sm text-zinc-400">
        This camera is in focus mode, so normal capture is paused and the latest
        frame won’t update until it ends. It stops by itself after 15 minutes.
      </p>
      <img src={`/api/focus/live?zoom=1&t=${tick}`} alt="Live focus view"
        className="mt-3 w-full rounded-xl border border-zinc-800 bg-black" />
      <div className="mt-3 flex gap-3">
        <Button tone="warn" className="flex-1" onClick={stop}>Stop focus mode</Button>
        <Button onClick={onOpenFocus}>Open focus screen</Button>
      </div>
    </Card>
  )
}

function StatusLine({ daemon: d }) {
  // A night_only camera sitting out the day looks identical to a broken one
  // unless we say why it is quiet and when it will wake up.
  if (d.state === 'idle_day') {
    const when = d.dusk
      ? new Date(d.dusk * 1000).toLocaleTimeString([], {
          hour: '2-digit', minute: '2-digit' })
      : null
    return (
      <p className="text-sm text-amber-300">
        Idle until dusk{when ? ` at ${when}` : ''} · night-only schedule
      </p>
    )
  }
  if (d.state !== 'capturing') {
    return <p className="text-sm text-zinc-500">{d.state ?? 'idle'}</p>
  }
  const bits = [
    d.period,
    `${(d.exposure_us / 1e6).toFixed(1)}s`,
    `gain ${d.gain}`,
    `brightness ${d.brightness}`,
  ]
  // stars is null in daylight by design; Kp only once the aurora poll has run.
  if (d.stars != null) bits.push(`${d.stars.toLocaleString()} stars`)
  if (d.kp != null) bits.push(`Kp ${d.kp}`)
  return (
    <>
      <p className="text-sm text-zinc-400">{bits.join(' · ')}</p>
      {/* Auto-exposure asking for more light and having none left to give.
          Not a fault, but it has to be visible: a whole night ran pinned at
          gain 22 on a module whose ceiling is 22, and the only symptom was a
          sky that looked darker than it should have. */}
      {d.ae_at_limits && (
        <p className="mt-1 text-sm text-amber-400">
          AE at limits — sky darker than target. Raise the longest exposure in
          Settings, or accept darker frames.
        </p>
      )}
    </>
  )
}

function KeeperButton({ depth, showToast }) {
  const [busy, setBusy] = useState(false)

  // POST /api/keeper only drops a command file; the daemon acts on it up to a
  // full capture gap later. So we watch for the result it writes and report the
  // real count, rather than claiming success the moment the POST returns.
  const save = async () => {
    setBusy(true)
    try {
      const before = (await (await fetch('/api/status')).json()).keeper?.at ?? 0
      await fetch('/api/keeper', { method: 'POST' })
      showToast('Save RAW queued — waiting for the next frame')

      const deadline = Date.now() + 120000
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500))
        const k = (await (await fetch('/api/status')).json()).keeper
        if (k?.at && k.at !== before) {
          showToast(k.saved
            ? `Saved ${k.saved} frame${k.saved === 1 ? '' : 's'} as DNG`
            : `No frames saved (${k.buffered} buffered) — check the log`)
          setBusy(false)
          return
        }
      }
      showToast('Save RAW timed out waiting for the daemon')
    } catch {
      showToast('Could not reach the camera')
    }
    setBusy(false)
  }

  // Label states the live configured depth, so it matches what pressing it
  // will actually produce rather than a hardcoded guess.
  return (
    <Button onClick={save} disabled={busy} className="shrink-0">
      {busy ? 'Saving…' : `Save last ${depth} RAW frame${depth === 1 ? '' : 's'}`}
    </Button>
  )
}

/* -- safety pause ---------------------------------------------------------- */

function SafetyBanner({ daemon: d, showToast }) {
  if (d.state !== 'paused_safety') return null
  const resume = async () => {
    await fetch('/api/capture/resume', { method: 'POST' }).catch(() => {})
    showToast('Resuming capture')
  }
  return (
    <Card>
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-medium text-amber-300">Capture paused — safety stop</p>
          <p className="mt-1 text-sm text-zinc-400">
            {d.reason === 'daylight'
              ? 'Daylight arrived while the camera was in manual exposure.'
              : 'Several frames came back near saturation.'}{' '}
            Capture will not resume into the condition that tripped it — it lifts
            at the next dusk, or now if you resume manually.
          </p>
        </div>
        <Button tone="warn" onClick={resume} className="shrink-0">Resume</Button>
      </div>
    </Card>
  )
}


/* -- storage --------------------------------------------------------------- */

function StorageCard({ storage }) {
  if (!storage || storage.free_gb == null) return null
  const usedPct = storage.total_gb
    ? Math.min(100, Math.round(((storage.total_gb - storage.free_gb) / storage.total_gb) * 100))
    : 0
  // Same threshold the daemon warns at: twice the cleanup floor.
  const tight = storage.free_gb < storage.cleanup_free_gb * 2

  return (
    <Card title="Storage"
      right={<span className="text-sm text-zinc-500">{usedPct}% used</span>}>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full ${tight ? 'bg-amber-500' : 'bg-sky-500'}`}
          style={{ width: `${usedPct}%` }} />
      </div>

      <dl className="mt-4 grid grid-cols-3 gap-3 text-sm">
        <div>
          <dt className="text-zinc-500">Free</dt>
          <dd className={`tabular-nums ${tight ? 'text-amber-400' : 'text-zinc-200'}`}>
            {storage.free_gb} GB
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">Nights on card</dt>
          <dd className="tabular-nums text-zinc-200">{storage.nights}</dd>
        </div>
        <div>
          <dt className="text-zinc-500">Cleanup floor</dt>
          <dd className="tabular-nums text-zinc-200">{storage.cleanup_free_gb} GB</dd>
        </div>
      </dl>

      {storage.nights_remaining != null && (
        <p className="mt-3 rounded-lg bg-zinc-800/60 p-3 text-sm">
          <span className="text-zinc-200">
            ~{storage.nights_remaining} more night{storage.nights_remaining === 1 ? '' : 's'}
          </span>
          <span className="text-zinc-500">
            {' '}at {storage.per_night_gb} GB/night
            {storage.basis === 'in_progress' && ' (tonight so far)'}
          </span>
        </p>
      )}

      <p className="mt-3 text-xs text-zinc-500">
        Below the floor the oldest nights are trimmed automatically — frames
        first, timelapses last, and never the night in progress.
      </p>
    </Card>
  )
}
