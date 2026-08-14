// Dashboard: latest frame, live capture status, and the actions that only make
// sense while standing at the camera — keeper RAW, focus assist, re-render.
import { useEffect, useState } from 'react'
import SettingsScreen from './SettingsScreen.jsx'
import { Button, Card, Select, Toast, useToast } from '../components/ui.jsx'

const QUALITY_OPTIONS = [
  { value: 'standard', label: 'Standard' },
  { value: 'high', label: 'High' },
  { value: 'max', label: 'Max' },
]

// Mirrors focus.TIMEOUT_S. Duplicated rather than fetched: it only drives a
// countdown label, and the daemon enforces the real deadline regardless.
const FOCUS_TIMEOUT_MS = 15 * 60 * 1000

export default function Dashboard({ status }) {
  const [showSettings, setShowSettings] = useState(false)
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
          <button onClick={() => setShowSettings(!showSettings)}
            className="rounded-lg border border-zinc-700 px-3 py-1 hover:bg-zinc-800">
            {showSettings ? 'Dashboard' : 'Settings'}
          </button>
        </div>
      </header>

      {showSettings ? (
        <SettingsScreen showToast={showToast} />
      ) : (
        <main className="mt-6 flex flex-col gap-5">
          <LatestFrame daemon={d} showToast={showToast} />
          <SafetyBanner daemon={d} showToast={showToast} />
          <FocusCard showToast={showToast} />
          <TimelapseCard current={current} showToast={showToast} />
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
  const save = async () => {
    setBusy(true)
    try {
      const r = await fetch('/api/keeper', { method: 'POST' })
      const body = await r.json()
      showToast(body.note ?? 'Buffered frames will be saved as DNG')
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

/* -- timelapse ------------------------------------------------------------- */

function TimelapseCard({ current, showToast }) {
  const [clipSeconds, setClipSeconds] = useState(30)
  const [quality, setQuality] = useState('high')
  const [busy, setBusy] = useState(false)
  const [version, setVersion] = useState(0)   // cache-bust <video> after a re-render

  if (!current.camera_id || !current.night) return null

  const rerender = async () => {
    setBusy(true)
    try {
      const url = `/api/timelapse/render/${current.camera_id}/${current.night}`
        + `?force=true&clip_seconds=${clipSeconds}&quality=${quality}`
      const r = await fetch(url, { method: 'POST' })
      const body = await r.json()
      if (r.ok) {
        showToast(`Rendered ${body.file}`)
        setVersion((v) => v + 1)
      } else {
        showToast(body.detail ?? 'Render failed')
      }
    } catch {
      showToast('Render failed')
    }
    setBusy(false)
  }

  return (
    <Card title="Timelapse"
      right={<span className="text-sm text-zinc-500">{current.night}</span>}>
      {current.timelapse || version > 0 ? (
        <video
          key={version} controls playsInline preload="metadata"
          src={`/api/timelapse/${current.camera_id}/${current.night}?v=${version}`}
          className="mt-3 w-full rounded-xl border border-zinc-800 bg-black" />
      ) : (
        <p className="mt-3 rounded-xl border border-dashed border-zinc-800 p-6
                      text-center text-sm text-zinc-500">
          No timelapse for this night yet — one renders automatically at dawn.
        </p>
      )}

      <div className="mt-4 space-y-3">
        <label className="block text-sm">
          <span className="flex justify-between">
            <span className="text-zinc-400">Clip length</span>
            <span className="tabular-nums text-zinc-300">{clipSeconds}s</span>
          </span>
          <input
            type="range" min="5" max="120" step="5" value={clipSeconds}
            onChange={(e) => setClipSeconds(Number(e.target.value))}
            className="mt-2 w-full accent-sky-500" />
        </label>

        <Select label="Quality" value={quality} onChange={setQuality}
          options={QUALITY_OPTIONS} />

        <Button onClick={rerender} disabled={busy} tone="accent" className="w-full">
          {busy ? 'Rendering…' : 'Re-render this night'}
        </Button>
        <p className="text-xs text-zinc-500">
          A one-off override for this render — your saved timelapse settings are
          left alone.
        </p>
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

      <p className="mt-3 text-xs text-zinc-500">
        Below the floor the oldest nights are trimmed automatically — frames
        first, timelapses last, and never the night in progress.
      </p>
    </Card>
  )
}
