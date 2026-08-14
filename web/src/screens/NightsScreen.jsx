// Nights browser: pick a night, then scrub it. The star chart is the "find the
// clear part of the night" tool — click a peak and the filmstrip seeks there.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Card, Select } from '../components/ui.jsx'

const QUALITY_OPTIONS = [
  { value: 'standard', label: 'Standard' },
  { value: 'high', label: 'High' },
  { value: 'max', label: 'Max' },
]

const PAGE = 2000                 // matches the API's per-request ceiling
const STRIP_RADIUS = 15           // thumbnails rendered either side of the cursor

const gb = (bytes) => (bytes / 1e9).toFixed(bytes < 1e9 ? 2 : 1)
const clockOf = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

export default function NightsScreen({ cameraId, showToast, onBack }) {
  const [nights, setNights] = useState(null)
  const [selected, setSelected] = useState(null)

  useEffect(() => {
    if (!cameraId) return
    fetch(`/api/nights/${cameraId}`)
      .then((r) => r.json()).then(setNights).catch(() => setNights([]))
  }, [cameraId])

  if (selected) {
    return (
      <NightView cameraId={cameraId} night={selected} showToast={showToast}
        onBack={() => setSelected(null)} />
    )
  }

  return (
    <div className="mt-6 flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">Nights</h2>
        <Button onClick={onBack}>Back to dashboard</Button>
      </div>

      {nights === null && <p className="text-sm text-zinc-500">Loading…</p>}
      {nights?.length === 0 && (
        <Card><p className="text-sm text-zinc-500">No nights captured yet.</p></Card>
      )}

      <ul className="flex flex-col gap-3">
        {nights?.map((n) => (
          <li key={n.night}>
            <button onClick={() => setSelected(n.night)}
              className="w-full rounded-2xl border border-zinc-800 bg-zinc-900 p-4
                         text-left transition hover:border-zinc-700 hover:bg-zinc-800/60">
              <div className="flex items-center justify-between gap-3">
                <span className="font-medium">{n.night}</span>
                {n.has_timelapse && (
                  <span className="rounded-full bg-sky-950 px-2.5 py-0.5 text-xs text-sky-400">
                    timelapse
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-zinc-400">
                {n.frames.toLocaleString()} frames · {gb(n.bytes)} GB ·{' '}
                {clockOf(n.first)}–{clockOf(n.last)}
              </p>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/* -- one night ------------------------------------------------------------- */

function NightView({ cameraId, night, showToast, onBack }) {
  const [frames, setFrames] = useState([])
  const [total, setTotal] = useState(0)
  const [index, setIndex] = useState(0)
  const [stars, setStars] = useState([])
  const [fullscreen, setFullscreen] = useState(false)
  const [hasTimelapse, setHasTimelapse] = useState(false)

  // Pull the whole index in pages. It is small (~100 bytes/frame) and the
  // scrubber needs the full timeline; the images themselves stay lazy.
  useEffect(() => {
    let alive = true
    const all = []
    const pull = async (offset) => {
      const r = await fetch(
        `/api/nights/${cameraId}/${night}/frames?offset=${offset}&limit=${PAGE}`)
      const body = await r.json()
      all.push(...body.frames)
      if (!alive) return
      setTotal(body.total)
      if (all.length < body.total && body.frames.length) return pull(all.length)
      setFrames([...all])
      setIndex((i) => Math.min(i, Math.max(0, all.length - 1)))
    }
    pull(0).catch(() => {})
    fetch(`/api/stars/${cameraId}/${night}`)
      .then((r) => r.json()).then((s) => alive && setStars(s)).catch(() => {})
    fetch(`/api/nights/${cameraId}`).then((r) => r.json())
      .then((ns) => alive && setHasTimelapse(
        !!ns.find((n) => n.night === night)?.has_timelapse))
      .catch(() => {})
    return () => { alive = false }
  }, [cameraId, night])

  const frame = frames[index]
  const frameUrl = frame
    ? `/api/nights/${cameraId}/${night}/frame/${frame.name}` : null
  const dngUrl = frame?.has_dng
    ? `/api/nights/${cameraId}/${night}/raw/${frame.name.replace(/\.jpg$/, '.dng')}`
    : null

  // Chart click -> nearest frame in time. This is the whole point of the chart:
  // spot the clear stretch, jump straight to it.
  const seekToTime = useCallback((ts) => {
    if (!frames.length) return
    let best = 0, bestGap = Infinity
    frames.forEach((f, i) => {
      const gap = Math.abs(f.timestamp - ts)
      if (gap < bestGap) { bestGap = gap; best = i }
    })
    setIndex(best)
  }, [frames])

  return (
    <div className="mt-6 flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">{night}</h2>
        <Button onClick={onBack}>All nights</Button>
      </div>

      <TimelapsePanel cameraId={cameraId} night={night} present={hasTimelapse}
        showToast={showToast} onRendered={() => setHasTimelapse(true)} />

      <Card title="Frames"
        right={<span className="text-sm text-zinc-500">
          {total ? `${index + 1} / ${total.toLocaleString()}` : '—'}
        </span>}>
        {frame ? (
          <>
            <button onClick={() => setFullscreen(true)}
              className="mt-3 block w-full" title="Tap for full screen">
              <img src={frameUrl} alt={`Frame at ${clockOf(frame.timestamp)}`}
                className="w-full rounded-xl border border-zinc-800" />
            </button>

            <div className="mt-2 flex items-center justify-between text-sm">
              <span className="text-zinc-400">
                {clockOf(frame.timestamp)}
                {frame.exposure_us != null &&
                  ` · ${(frame.exposure_us / 1e6).toFixed(1)}s`}
                {frame.gain != null && ` · gain ${frame.gain}`}
                {frame.stars != null && ` · ${frame.stars.toLocaleString()} stars`}
              </span>
              {frame.has_dng && (
                <span className="rounded-full bg-emerald-950 px-2.5 py-0.5 text-xs
                                 text-emerald-400">DNG</span>
              )}
            </div>

            <input
              type="range" min={0} max={Math.max(0, frames.length - 1)} value={index}
              onChange={(e) => setIndex(Number(e.target.value))}
              className="mt-4 w-full accent-sky-500"
              aria-label="Scrub through the night" />

            <Filmstrip cameraId={cameraId} night={night} frames={frames}
              index={index} onPick={setIndex} />
          </>
        ) : (
          <p className="mt-3 text-sm text-zinc-500">Loading frames…</p>
        )}
      </Card>

      <StarChart stars={stars} current={frame?.timestamp} onSeek={seekToTime} />

      {fullscreen && frame && (
        <Fullscreen frameUrl={frameUrl} dngUrl={dngUrl} name={frame.name}
          onClose={() => setFullscreen(false)} />
      )}
    </div>
  )
}

/* -- filmstrip ------------------------------------------------------------- */

function Filmstrip({ cameraId, night, frames, index, onPick }) {
  const ref = useRef(null)
  // Only a window around the cursor is mounted: 1200 <img> tags would be a
  // punishing amount of DOM and traffic for a phone, even with lazy loading.
  const from = Math.max(0, index - STRIP_RADIUS)
  const window_ = frames.slice(from, index + STRIP_RADIUS + 1)

  useEffect(() => {
    const el = ref.current?.querySelector('[data-active="true"]')
    el?.scrollIntoView({ block: 'nearest', inline: 'center' })
  }, [index])

  return (
    <div ref={ref} className="mt-3 flex gap-1.5 overflow-x-auto pb-1">
      {window_.map((f, i) => {
        const real = from + i
        return (
          <button key={f.name} data-active={real === index}
            onClick={() => onPick(real)}
            className={`relative shrink-0 overflow-hidden rounded-md border transition ${
              real === index ? 'border-sky-500' : 'border-zinc-800 hover:border-zinc-600'}`}>
            <img
              src={`/api/nights/${cameraId}/${night}/frame/${f.name}?thumb=true`}
              alt="" loading="lazy" className="h-14 w-14 object-cover" />
            {f.has_dng && (
              <span className="absolute bottom-0 right-0 bg-emerald-500/90 px-1
                               text-[9px] font-medium text-black">R</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/* -- star chart ------------------------------------------------------------ */

function StarChart({ stars, current, onSeek }) {
  const svgRef = useRef(null)
  const { points, t0, t1, max } = useMemo(() => {
    if (!stars.length) return { points: '', t0: 0, t1: 0, max: 0 }
    const t0 = stars[0].t, t1 = stars[stars.length - 1].t
    const max = Math.max(...stars.map((s) => s.stars)) || 1
    const span = Math.max(1, t1 - t0)
    const points = stars.map((s) =>
      `${((s.t - t0) / span) * 100},${100 - (s.stars / max) * 100}`).join(' ')
    return { points, t0, t1, max }
  }, [stars])

  if (!stars.length) {
    return (
      <Card title="Sky quality">
        <p className="mt-1 text-sm text-zinc-500">
          No star counts for this night — they’re only measured after dark.
        </p>
      </Card>
    )
  }

  const seek = (e) => {
    const rect = svgRef.current.getBoundingClientRect()
    const x = ((e.clientX ?? e.touches?.[0]?.clientX) - rect.left) / rect.width
    onSeek(t0 + Math.min(1, Math.max(0, x)) * (t1 - t0))
  }

  const cursorPct = current != null && t1 > t0
    ? Math.min(100, Math.max(0, ((current - t0) / (t1 - t0)) * 100)) : null

  return (
    <Card title="Sky quality"
      right={<span className="text-sm text-zinc-500">peak {max.toLocaleString()}</span>}>
      <p className="mt-1 text-sm text-zinc-400">
        Stars counted per frame. A collapse is cloud — click to jump there.
      </p>
      <svg ref={svgRef} viewBox="0 0 100 100" preserveAspectRatio="none"
        onClick={seek} role="img" aria-label="Star count across the night"
        className="mt-3 h-32 w-full cursor-pointer rounded-xl bg-zinc-800/60">
        <polyline points={points} fill="none" stroke="#38bdf8" strokeWidth="1"
          vectorEffect="non-scaling-stroke" />
        {cursorPct != null && (
          <line x1={cursorPct} y1="0" x2={cursorPct} y2="100" stroke="#fbbf24"
            strokeWidth="1" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
      <div className="mt-1 flex justify-between text-xs text-zinc-500">
        <span>{clockOf(t0)}</span><span>{clockOf(t1)}</span>
      </div>
    </Card>
  )
}

/* -- fullscreen ------------------------------------------------------------ */

function Fullscreen({ frameUrl, dngUrl, name, onClose }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/95">
      <div className="flex items-center justify-between gap-3 p-4">
        <span className="truncate text-sm text-zinc-400">{name}</span>
        <Button onClick={onClose}>Close</Button>
      </div>
      <div className="flex flex-1 items-center justify-center overflow-auto px-4">
        <img src={frameUrl} alt={name} className="max-h-full max-w-full object-contain" />
      </div>
      <div className="flex flex-wrap justify-center gap-3 p-4">
        <a href={frameUrl} download={name}
          className="rounded-lg border border-zinc-700 px-3 py-2.5 text-sm hover:bg-zinc-800">
          Download JPEG
        </a>
        {dngUrl ? (
          <a href={dngUrl}
            className="rounded-lg border border-sky-600 bg-sky-600/15 px-3 py-2.5
                       text-sm text-sky-300 hover:bg-sky-600/25">
            Download DNG
          </a>
        ) : (
          <span className="rounded-lg border border-zinc-800 px-3 py-2.5 text-sm text-zinc-600">
            No DNG for this frame
          </span>
        )}
      </div>
    </div>
  )
}

/* -- timelapse (relocated here from the dashboard) ------------------------- */

export function TimelapsePanel({ cameraId, night, present, showToast, onRendered }) {
  const [clipSeconds, setClipSeconds] = useState(30)
  const [quality, setQuality] = useState('high')
  const [busy, setBusy] = useState(false)
  const [version, setVersion] = useState(0)

  const rerender = async () => {
    setBusy(true)
    try {
      const url = `/api/timelapse/render/${cameraId}/${night}`
        + `?force=true&clip_seconds=${clipSeconds}&quality=${quality}`
      const r = await fetch(url, { method: 'POST' })
      const body = await r.json()
      if (r.ok) {
        showToast(`Rendered ${body.file}`)
        setVersion((v) => v + 1)
        onRendered?.()
      } else {
        showToast(body.detail ?? 'Render failed')
      }
    } catch {
      showToast('Render failed')
    }
    setBusy(false)
  }

  return (
    <Card title="Timelapse">
      {present || version > 0 ? (
        <video key={version} controls playsInline preload="metadata"
          src={`/api/timelapse/${cameraId}/${night}?v=${version}`}
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
          <input type="range" min="5" max="120" step="5" value={clipSeconds}
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
          left alone. Frame rate is clamped to 12–60 fps, so a short night may
          produce a shorter clip than requested.
        </p>
      </div>
    </Card>
  )
}
