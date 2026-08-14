// Focus assist as a live view. The image is the centrepiece — you are standing
// at the camera turning a ring, so everything else is arranged around keeping
// that picture large and current.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card } from '../components/ui.jsx'

const ZOOMS = [1, 2, 4, 8, 10]
const REFRESH_MS = 1000
const TIMEOUT_MS = 15 * 60 * 1000      // mirrors focus.TIMEOUT_S
const SPARK_POINTS = 60

const TREND_LABEL = { improving: '▲ sharper', worsening: '▼ softer', flat: '— steady' }
const TREND_CLASS = {
  improving: 'text-emerald-400', worsening: 'text-rose-400', flat: 'text-zinc-400',
}

export default function FocusScreen({ showToast, onExit }) {
  const [info, setInfo] = useState(null)
  const [zoom, setZoom] = useState(1)
  const [center, setCenter] = useState({ x: 0.5, y: 0.5 })
  const [exposureMs, setExposureMs] = useState(500)
  const [gain, setGain] = useState(250)
  const [maxGain, setMaxGain] = useState(600)
  const [startedAt] = useState(() => Date.now())
  const [now, setNow] = useState(() => Date.now())
  const [tick, setTick] = useState(0)
  const [history, setHistory] = useState([])
  const drag = useRef(null)

  // Start the session, then push the initial control values so the daemon is
  // using the same numbers the sliders are showing.
  useEffect(() => {
    let alive = true
    const begin = async () => {
      await fetch('/api/focus/start', { method: 'POST' }).catch(() => {})
      await pushControls(exposureMs, gain)
      const cfg = await (await fetch('/api/config')).json().catch(() => null)
      if (alive && cfg) {
        const cam = Object.values(cfg.cameras ?? {})[0]
        if (cam?.night?.max_gain) setMaxGain(Math.max(cam.night.max_gain, 600))
      }
    }
    begin()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    let alive = true
    const id = setInterval(async () => {
      if (!alive) return
      setNow(Date.now())
      setTick((t) => t + 1)
      try {
        const s = await (await fetch('/api/status')).json()
        if (!alive) return
        const d = s.daemon ?? {}
        if (d.state === 'focusing') {
          setInfo(d)
          setHistory((h) => [...h.slice(-(SPARK_POINTS - 1)), d.score ?? 0])
        } else if (Date.now() - startedAt > 6000) {
          showToast('Focus mode ended')
          onExit()
        }
      } catch { /* transient */ }
    }, REFRESH_MS)
    return () => { alive = false; clearInterval(id) }
  }, [startedAt, showToast, onExit])

  const pushControls = useCallback(async (ms, g) => {
    await fetch('/api/focus/controls', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exposure_ms: ms, gain: g }),
    }).catch(() => {})
  }, [])

  const stop = async () => {
    await fetch('/api/focus/stop', { method: 'POST' }).catch(() => {})
    showToast('Focus mode off')
    onExit()
  }

  // Pan: dragging moves the crop window opposite the gesture, so it feels like
  // pushing the image around. Only meaningful once zoomed in. Distances are
  // divided by the zoom factor because a screen pixel covers 1/zoom of the
  // frame at that level.
  const clamp01 = (v) => Math.min(1, Math.max(0, v))

  const onPointerDown = (e) => {
    if (zoom === 1) return
    drag.current = {
      pointerX: e.clientX, pointerY: e.clientY,
      originX: center.x, originY: center.y,
    }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }

  const onPointerMove = (e) => {
    if (!drag.current) return
    const rect = e.currentTarget.getBoundingClientRect()
    const dx = (e.clientX - drag.current.pointerX) / rect.width / zoom
    const dy = (e.clientY - drag.current.pointerY) / rect.height / zoom
    setCenter({
      x: clamp01(drag.current.originX - dx),
      y: clamp01(drag.current.originY - dy),
    })
  }

  const onPointerUp = () => { drag.current = null }

  const remaining = Math.max(0, TIMEOUT_MS - (now - startedAt))
  const mm = Math.floor(remaining / 60000)
  const ss = String(Math.floor((remaining % 60000) / 1000)).padStart(2, '0')

  const liveUrl = `/api/focus/live?zoom=${zoom}`
    + `&cx=${center.x.toFixed(3)}&cy=${center.y.toFixed(3)}&t=${tick}`

  return (
    <div className="mt-6 flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">Focus assist</h2>
        <span className="text-sm tabular-nums text-sky-400">
          auto-exit in {mm}:{ss}
        </span>
      </div>

      <div className="overflow-hidden rounded-2xl border border-zinc-800 bg-black"
        onPointerDown={onPointerDown} onPointerMove={onPointerMove}
        onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
        style={{ cursor: zoom > 1 ? 'grab' : 'default', touchAction: 'none' }}>
        <img src={liveUrl} alt="Live focus view" draggable={false}
          className="w-full select-none" />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {ZOOMS.map((z) => (
          <button key={z} onClick={() => { setZoom(z); if (z === 1) setCenter({ x: .5, y: .5 }) }}
            className={`rounded-lg border px-3 py-1.5 text-sm transition ${
              z === zoom ? 'border-sky-500 bg-sky-600/20 text-sky-300'
                         : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'}`}>
            {z}×
          </button>
        ))}
        {zoom > 1 && (
          <span className="ml-1 text-xs text-zinc-500">drag the image to pan</span>
        )}
      </div>

      <Card title="Sharpness"
        right={<span className={`text-sm ${TREND_CLASS[info?.trend] ?? 'text-zinc-400'}`}>
          {TREND_LABEL[info?.trend] ?? '—'}
        </span>}>
        <div className="mt-3 flex items-end gap-6">
          <div>
            <p className="text-xs text-zinc-500">Score</p>
            <p className="text-3xl tabular-nums">{info?.score ?? '—'}</p>
          </div>
          <div>
            <p className="text-xs text-zinc-500">Best</p>
            <p className="text-2xl tabular-nums text-zinc-400">{info?.best ?? '—'}</p>
          </div>
        </div>
        <Sparkline values={history} best={info?.best} />
        <p className="mt-2 text-xs text-zinc-500">
          Smoothed over the last 3 frames — raw scores jitter on noise alone.
          Turn the ring until the line peaks and the arrow flips.
        </p>
      </Card>

      <Card title="Exposure">
        <div className="mt-3 space-y-4">
          <Slider label="Exposure" value={exposureMs} min={50} max={2000} step={50}
            display={`${exposureMs} ms`}
            onChange={(v) => { setExposureMs(v); pushControls(v, gain) }} />
          <Slider label="Gain" value={gain} min={0} max={maxGain} step={5}
            display={String(gain)}
            onChange={(v) => { setGain(v); pushControls(exposureMs, v) }} />
          {info && (
            <p className="text-xs text-zinc-500">
              Camera reported {(info.exposure_us / 1000).toFixed(0)} ms at gain{' '}
              {info.gain} on the last frame.
            </p>
          )}
        </div>
      </Card>

      <Button tone="warn" onClick={stop} className="w-full">Stop focus assist</Button>
    </div>
  )
}

function Slider({ label, value, min, max, step, display, onChange }) {
  return (
    <label className="block text-sm">
      <span className="flex justify-between">
        <span className="text-zinc-400">{label}</span>
        <span className="tabular-nums text-zinc-300">{display}</span>
      </span>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-2 w-full accent-sky-500" />
    </label>
  )
}

function Sparkline({ values, best }) {
  if (values.length < 2) {
    return <div className="mt-4 h-20 rounded-xl bg-zinc-800/60" />
  }
  const max = Math.max(...values, best ?? 0) || 1
  const points = values
    .map((v, i) => `${(i / (values.length - 1)) * 100},${100 - (v / max) * 100}`)
    .join(' ')
  const bestY = best ? 100 - (best / max) * 100 : null

  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img"
      aria-label="Sharpness over time"
      className="mt-4 h-20 w-full rounded-xl bg-zinc-800/60">
      {bestY != null && (
        <line x1="0" y1={bestY} x2="100" y2={bestY} stroke="#fbbf24"
          strokeWidth="1" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      )}
      <polyline points={points} fill="none" stroke="#38bdf8" strokeWidth="1.5"
        vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
