// Dashboard: latest frame, live capture status, and the actions that only make
// sense while standing at the camera — keeper RAW, focus assist, re-render.
import { useEffect, useState } from 'react'
import SettingsScreen from './SettingsScreen.jsx'
import NightsScreen from './NightsScreen.jsx'
import { Button, Card, Toast, useToast } from '../components/ui.jsx'

// Mirrors focus.TIMEOUT_S. Duplicated rather than fetched: it only drives a
// countdown label, and the daemon enforces the real deadline regardless.
const FOCUS_TIMEOUT_MS = 15 * 60 * 1000

export default function Dashboard({ status }) {
  const [view, setView] = useState('dashboard')   // dashboard | nights | settings
  const [toast, showToast] = useToast()
  const d = status.daemon ?? {}
  const current = status.current ?? {}
  const standalone =
    status.network?.mode === 'standalone' || status.network?.session_standalone

  return (
    <div className="mx-auto min-h-screen max-w-3xl px-4 py-6">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-medium">Skylapse</h1>
        <div className="flex items-center gap-3 text-sm">
          {standalone && (
            <span className="rounded-full bg-amber-950 px-3 py-1 text-amber-400">
              Network: Standalone
            </span>
          )}
          <span className="text-zinc-400">
            {new Date(status.server_time * 1000).toLocaleTimeString()}
          </span>
          <button onClick={() => setView(view === 'nights' ? 'dashboard' : 'nights')}
            className="rounded-lg border border-zinc-700 px-3 py-1 hover:bg-zinc-800">
            Nights
          </button>
          <button onClick={() => setView(view === 'settings' ? 'dashboard' : 'settings')}
            className="rounded-lg border border-zinc-700 px-3 py-1 hover:bg-zinc-800">
            {view === 'settings' ? 'Dashboard' : 'Settings'}
          </button>
        </div>
      </header>

      {view === 'settings' && (
        <SettingsScreen showToast={showToast} storage={status.storage} />
      )}

      {view === 'nights' && (
        <NightsScreen cameraId={current.camera_id} showToast={showToast}
          onBack={() => setView('dashboard')} />
      )}

      {view === 'dashboard' && (
        <main className="mt-6 flex flex-col gap-5">
          <LatestFrame daemon={d} showToast={showToast} />
          <SafetyBanner daemon={d} showToast={showToast} />
          <FocusCard showToast={showToast} />
          <StorageCard storage={status.storage} />
        </main>
      )}

      <Toast message={toast} />
    </div>
  )
}

/* -- latest frame + live status ------------------------------------------- */

function LatestFrame({ daemon: d, showToast }) {
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

      <div className="mt-3 flex items-center justify-between gap-4">
        <StatusLine daemon={d} />
        <KeeperButton showToast={showToast} />
      </div>
    </section>
  )
}

function StatusLine({ daemon: d }) {
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
  return <p className="text-sm text-zinc-400">{bits.join(' · ')}</p>
}

function KeeperButton({ showToast }) {
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

  return (
    <Button onClick={save} disabled={busy} className="shrink-0">
      {busy ? 'Saving…' : 'Save RAW'}
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

/* -- focus assist ---------------------------------------------------------- */

const TREND_LABEL = { improving: '▲ sharper', worsening: '▼ softer', flat: '— steady' }
const TREND_CLASS = {
  improving: 'text-emerald-400', worsening: 'text-rose-400', flat: 'text-zinc-400',
}

function FocusCard({ showToast }) {
  const [active, setActive] = useState(false)
  const [info, setInfo] = useState(null)
  const [startedAt, setStartedAt] = useState(0)
  const [now, setNow] = useState(Date.now())

  // Focus needs far tighter feedback than the app's 5s status poll — you are
  // turning a ring and watching the number move. Poll fast, but only while on.
  useEffect(() => {
    if (!active) return
    let alive = true
    const id = setInterval(async () => {
      if (!alive) return
      setNow(Date.now())
      try {
        const s = await (await fetch('/api/status')).json()
        if (!alive) return
        const d = s.daemon ?? {}
        if (d.state === 'focusing') {
          setInfo(d)
        } else if (Date.now() - startedAt > 6000) {
          // Daemon dropped out of focus mode on its own: the 15-minute auto-exit.
          setActive(false)
          setInfo(null)
          showToast('Focus mode ended')
        }
      } catch { /* transient; next tick retries */ }
    }, 1000)
    return () => { alive = false; clearInterval(id) }
  }, [active, startedAt, showToast])

  const start = async () => {
    await fetch('/api/focus/start', { method: 'POST' }).catch(() => {})
    setStartedAt(Date.now())
    setNow(Date.now())
    setInfo(null)
    setActive(true)
    showToast('Focus mode on — turn the ring until the score peaks')
  }

  const stop = async () => {
    await fetch('/api/focus/stop', { method: 'POST' }).catch(() => {})
    setActive(false)
    setInfo(null)
    showToast('Focus mode off')
  }

  const remaining = Math.max(0, FOCUS_TIMEOUT_MS - (now - startedAt))
  const mm = Math.floor(remaining / 60000)
  const ss = String(Math.floor((remaining % 60000) / 1000)).padStart(2, '0')

  return (
    <Card
      title="Focus assist"
      right={active && (
        <span className="text-sm tabular-nums text-sky-400">auto-exit in {mm}:{ss}</span>
      )}>
      <p className="mt-1 text-sm text-zinc-400">
        Rapid throwaway frames with a live sharpness score — turn the focus ring
        until the number peaks. Nothing is written to the card while focusing,
        and the session exits by itself after 15 minutes so a forgotten session
        can’t cost you the night.
      </p>

      {active && (
        <div className="mt-4 grid grid-cols-3 gap-3 rounded-xl bg-zinc-800/60 p-4 text-center">
          <div>
            <p className="text-xs text-zinc-500">Score</p>
            <p className="text-2xl tabular-nums">{info?.score ?? '—'}</p>
          </div>
          <div>
            <p className="text-xs text-zinc-500">Best</p>
            <p className="text-2xl tabular-nums text-zinc-300">{info?.best ?? '—'}</p>
          </div>
          <div>
            <p className="text-xs text-zinc-500">Trend</p>
            <p className={`pt-1.5 text-lg ${TREND_CLASS[info?.trend] ?? 'text-zinc-400'}`}>
              {TREND_LABEL[info?.trend] ?? '—'}
            </p>
          </div>
        </div>
      )}

      <Button onClick={active ? stop : start} tone={active ? 'warn' : 'default'}
        className="mt-4 w-full">
        {active ? 'Stop focus assist' : 'Start focus assist'}
      </Button>
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
