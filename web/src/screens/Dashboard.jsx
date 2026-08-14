// Dashboard: latest frame, live capture status, and the actions that only make
// sense while standing at the camera — keeper RAW, focus assist, re-render.
import { useEffect, useState } from 'react'
import SettingsScreen from './SettingsScreen.jsx'
import NightsScreen from './NightsScreen.jsx'
import FocusScreen from './FocusScreen.jsx'
import { Button, Card, Toast, useToast } from '../components/ui.jsx'

export default function Dashboard({ status }) {
  const [view, setView] = useState('dashboard')   // dashboard | nights | settings | focus
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

      {view === 'focus' && (
        <FocusScreen showToast={showToast} onExit={() => setView('dashboard')} />
      )}

      {view === 'dashboard' && (
        <main className="mt-6 flex flex-col gap-5">
          <LatestFrame daemon={d} showToast={showToast} />
          <SafetyBanner daemon={d} showToast={showToast} />
          <Card title="Focus assist">
            <p className="mt-1 text-sm text-zinc-400">
              A live view with zoom, exposure and gain controls, and a sharpness
              score to chase. Nothing is written to the card while focusing, and
              the session exits by itself after 15 minutes.
            </p>
            <Button onClick={() => setView('focus')} className="mt-4 w-full">
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
