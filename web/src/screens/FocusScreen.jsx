// Focus assist as a live view.
//
// Laid out for the situation it is actually used in: standing at the camera in
// the dark, holding a phone in portrait, one hand on the focus ring. The image
// takes whatever height is left, and every control sits in the bottom third
// where a thumb reaches — nothing here should ever require scrolling.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '../components/ui.jsx'

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
  const lastFrames = useRef(0)

  const pushControls = useCallback(async (ms, g) => {
    await fetch('/api/focus/controls', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exposure_ms: ms, gain: g }),
    }).catch(() => {})
  }, [])

  useEffect(() => {
    let alive = true
    const begin = async () => {
      await fetch('/api/focus/start', { method: 'POST' }).catch(() => {})
      await pushControls(exposureMs, gain)
      try {
        const cfg = await (await fetch('/api/config')).json()
        const cam = Object.values(cfg.cameras ?? {})[0]
        if (alive && cam?.night?.max_gain) {
          setMaxGain(Math.max(cam.night.max_gain, 600))
        }
      } catch { /* keep the default ceiling */ }
    }
    begin()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Leaving by ANY route stops the session: nav button, browser back, closed
  // tab, or the Stop button. Without this the daemon stays in focus mode until
  // its 15-minute timeout while the dashboard shows a frame that never updates,
  // which made the Stop button load-bearing. keepalive lets the request survive
  // the page going away.
  useEffect(() => {
    const leave = () =>
      fetch('/api/focus/stop', { method: 'POST', keepalive: true }).catch(() => {})
    window.addEventListener('pagehide', leave)
    return () => {
      window.removeEventListener('pagehide', leave)
      leave()
    }
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
          // The daemon starts a fresh session when exposure or gain changes, so
          // its frame counter restarts. That is the signal to drop the old
          // sparkline: those points were measured under different conditions.
          if ((d.frames ?? 0) < lastFrames.current) setHistory([])
          lastFrames.current = d.frames ?? 0
          setHistory((h) => [...h.slice(-(SPARK_POINTS - 1)), d.score ?? 0])
        } else if (Date.now() - startedAt > 6000) {
          showToast('Focus mode ended')
          onExit()
        }
      } catch { /* transient */ }
    }, REFRESH_MS)
    return () => { alive = false; clearInterval(id) }
  }, [startedAt, showToast, onExit])

  const stop = async () => {
    await fetch('/api/focus/stop', { method: 'POST' }).catch(() => {})
    showToast('Focus mode off')
    onExit()
  }

  // Pan: dragging moves the crop window opposite the gesture, so it feels like
  // pushing the image around. Distances divide by the zoom factor because a
  // screen pixel covers 1/zoom of the frame at that level.
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
    // dvh, not vh: mobile browser chrome would otherwise push the controls off.
    <div className="flex h-[100dvh] flex-col gap-3 pb-3 pt-3">
      <div className="flex shrink-0 items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <span className="text-3xl tabular-nums leading-none">
            {info?.score ?? '—'}
          </span>
          <span className="text-sm text-zinc-500">
            best {info?.best ?? '—'}
          </span>
          <span className={`text-sm ${TREND_CLASS[info?.trend] ?? 'text-zinc-400'}`}>
            {TREND_LABEL[info?.trend] ?? '—'}
          </span>
        </div>
        <span className="shrink-0 text-sm tabular-nums text-sky-400">{mm}:{ss}</span>
      </div>

      {/* min-h-0 lets this shrink inside the flex column instead of overflowing */}
      <div className="min-h-0 flex-1 overflow-hidden rounded-2xl border border-zinc-800 bg-black"
        onPointerDown={onPointerDown} onPointerMove={onPointerMove}
        onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
        style={{ cursor: zoom > 1 ? 'grab' : 'default', touchAction: 'none' }}>
        <img src={liveUrl} alt="Live focus view" draggable={false}
          className="h-full w-full select-none object-contain" />
      </div>

      <Sparkline values={history} best={info?.best} />

      {info?.rebaselined && (
        <p className="shrink-0 rounded-lg bg-amber-950/70 px-3 py-2 text-xs text-amber-300">
          Rebaselined — sharpness only compares at a fixed exposure and gain, so
          the peak resets when you move a slider.
        </p>
      )}

      {/* Everything below is thumb territory. */}
      <div className="shrink-0 space-y-3">
        <div className="flex gap-2">
          {ZOOMS.map((z) => (
            <button key={z}
              onClick={() => { setZoom(z); if (z === 1) setCenter({ x: .5, y: .5 }) }}
              className={`flex-1 rounded-lg border py-2 text-sm transition ${
                z === zoom ? 'border-sky-500 bg-sky-600/20 text-sky-300'
                           : 'border-zinc-700 text-zinc-400'}`}>
              {z}×
            </button>
          ))}
        </div>

        <Slider label="Exposure" value={exposureMs} min={50} max={2000} step={50}
          display={`${exposureMs} ms`}
          onChange={(v) => { setExposureMs(v); pushControls(v, gain) }} />
        <Slider label="Gain" value={gain} min={0} max={maxGain} step={5}
          display={String(gain)}
          onChange={(v) => { setGain(v); pushControls(exposureMs, v) }} />

        <Button tone="warn" onClick={stop} className="w-full">
          Stop focus assist
        </Button>
      </div>
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
      {/* h-6 gives the thumb a bigger hit area than the default track */}
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 h-6 w-full accent-sky-500" />
    </label>
  )
}

function Sparkline({ values, best }) {
  if (values.length < 2) {
    return <div className="h-12 shrink-0 rounded-xl bg-zinc-800/60" />
  }
  const max = Math.max(...values, best ?? 0) || 1
  const points = values
    .map((v, i) => `${(i / (values.length - 1)) * 100},${100 - (v / max) * 100}`)
    .join(' ')
  const bestY = best ? 100 - (best / max) * 100 : null

  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img"
      aria-label="Sharpness over time"
      className="h-12 w-full shrink-0 rounded-xl bg-zinc-800/60">
      {bestY != null && (
        <line x1="0" y1={bestY} x2="100" y2={bestY} stroke="#fbbf24"
          strokeWidth="1" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      )}
      <polyline points={points} fill="none" stroke="#38bdf8" strokeWidth="1.5"
        vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
