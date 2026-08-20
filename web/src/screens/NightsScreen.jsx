// Nights browser: pick a night, then scrub it. The star chart is the "find the
// clear part of the night" tool — click a peak and the filmstrip seeks there.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Card, ConfirmDialog, Select } from '../components/ui.jsx'

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

      {!!nights?.length && (
        <ExportCard cameraId={cameraId} nights={nights} showToast={showToast} />
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

/* -- USB export ------------------------------------------------------------ */

const CONTENT_LABELS = {
  timelapse: 'Timelapse video',
  jpegs: 'JPEG frames (+ sidecars)',
  raws: 'RAW / DNG files',
}

function ExportCard({ cameraId, nights, showToast }) {
  // Always present, drive or no drive. Hiding the card until a stick was
  // plugged in meant the feature only existed for people who already knew it
  // existed — and gave no way to check whether the Pi had even seen the drive.
  const [open, setOpen] = useState(false)
  const [drives, setDrives] = useState([])
  const [device, setDevice] = useState('')
  const [picked, setPicked] = useState(() => new Set())
  const [content, setContent] = useState({ timelapse: true, jpegs: false, raws: false })
  const [progress, setProgress] = useState(null)
  const [busy, setBusy] = useState(false)

  const refreshDrives = useCallback(async () => {
    try {
      const list = await (await fetch('/api/export/drives')).json()
      setDrives(list)
      setDevice((d) => (list.find((x) => x.device === d) ? d : list[0]?.device ?? ''))
    } catch { setDrives([]) }
  }, [])

  // Poll for drives continuously so the button's indicator is honest even when
  // the panel is shut, and briskly while it is open — someone standing at the
  // Pi with this on their phone should watch the drive appear a moment after
  // they push it in, without reaching for a refresh.
  useEffect(() => {
    refreshDrives()
    const id = setInterval(refreshDrives, open ? 3000 : 15000)
    return () => clearInterval(id)
  }, [refreshDrives, open])

  // Poll while a copy is running so the bar actually moves.
  useEffect(() => {
    const id = setInterval(async () => {
      try {
        const s = await (await fetch('/api/export/status')).json()
        setProgress(s.state === 'idle' ? null : s)
      } catch { /* transient */ }
    }, 1000)
    return () => clearInterval(id)
  }, [])

  const drive = drives.find((d) => d.device === device)
  const selectedBytes = nights
    .filter((n) => picked.has(n.night))
    .reduce((sum, n) => sum + n.bytes, 0)

  const toggleNight = (night) => setPicked((prev) => {
    const next = new Set(prev)
    next.has(night) ? next.delete(night) : next.add(night)
    return next
  })

  const start = async () => {
    setBusy(true)
    try {
      if (!drive?.mountpoint) {
        const m = await (await fetch(
          `/api/export/mount?device=${encodeURIComponent(device)}`,
          { method: 'POST' })).json()
        if (!m.mountpoint) {
          showToast(m.needs_sudo
            ? 'Drive needs mounting by hand — see the hint in the API response'
            : 'Could not mount the drive')
          setBusy(false)
          return
        }
        await refreshDrives()
      }
      const r = await fetch('/api/export/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device, camera_id: cameraId, nights: [...picked], content,
        }),
      })
      const body = await r.json()
      if (!r.ok) {
        const d = body.detail ?? {}
        showToast(d.required_bytes
          ? `Not enough space: needs ${gb(d.required_bytes)} GB, `
            + `${gb(d.free_bytes)} GB free`
          : d.error ?? 'Export could not start')
      } else {
        showToast(`Exporting ${body.files_total} files (${gb(body.bytes_total)} GB)`)
      }
    } catch { showToast('Export failed to start') }
    setBusy(false)
  }

  const eject = async () => {
    const r = await (await fetch(
      `/api/export/eject?device=${encodeURIComponent(device)}`,
      { method: 'POST' })).json()
    showToast(r.ok ? 'Safe to remove the drive' : r.error ?? 'Could not eject')
    refreshDrives()
  }

  const pct = progress?.bytes_total
    ? Math.min(100, Math.round((progress.bytes_done / progress.bytes_total) * 100))
    : 0
  const connected = drives.length > 0
  const running = progress?.state === 'running'

  return (
    <Card>
      <button onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex w-full items-center gap-3 text-left">
        <span className="flex-1 font-medium">Export to USB</span>
        {connected && (
          <span className="flex items-center gap-1.5 rounded-full bg-emerald-950
                           px-2.5 py-0.5 text-xs text-emerald-400">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            {drives.length > 1 ? `${drives.length} drives` : 'drive connected'}
          </span>
        )}
        {running && (
          <span className="text-xs text-sky-400 tabular-nums">{pct}%</span>
        )}
        <span className="text-xs text-zinc-500">{open ? 'Hide' : 'Open'}</span>
      </button>

      {open && !connected && (
        <div className="mt-4 rounded-xl border border-dashed border-zinc-800 p-6
                        text-center">
          <p className="text-sm text-zinc-400">
            Plug a USB drive into the Pi to export nights, images, and RAWs.
          </p>
          <p className="mt-2 text-xs text-zinc-500">
            Watching for one now — it’ll appear here a moment after you insert
            it. The Pi’s own SD card is never offered as a destination.
          </p>
          <Button onClick={refreshDrives} className="mt-4">Check again</Button>
        </div>
      )}

      {open && connected && (
      <div className="mt-4 space-y-4">
        <div className="flex justify-end">
          <Button onClick={refreshDrives} className="!py-1">Rescan</Button>
        </div>
        <Select label="Drive" value={device} onChange={setDevice}
          options={drives.map((d) => ({
            value: d.device,
            label: `${d.label} · ${gb(d.size_bytes)} GB · ${d.fstype}`,
          }))} />

        <div>
          <p className="text-sm text-zinc-400">Nights</p>
          <ul className="mt-2 max-h-44 divide-y divide-zinc-800 overflow-y-auto
                         rounded-lg border border-zinc-800">
            {nights.map((n) => (
              <li key={n.night}
                className="flex items-center justify-between px-3 py-2 text-sm">
                <label className="flex flex-1 items-center gap-2">
                  <input type="checkbox" checked={picked.has(n.night)}
                    onChange={() => toggleNight(n.night)} />
                  <span>{n.night}</span>
                </label>
                <span className="tabular-nums text-zinc-500">{gb(n.bytes)} GB</span>
              </li>
            ))}
          </ul>
        </div>

        <div>
          <p className="text-sm text-zinc-400">Include</p>
          <ul className="mt-2 divide-y divide-zinc-800 rounded-lg border border-zinc-800">
            {Object.entries(CONTENT_LABELS).map(([key, label]) => (
              <li key={key} className="flex items-center justify-between px-3 py-2 text-sm">
                <span>{label}</span>
                <input type="checkbox" checked={content[key]}
                  onChange={(e) =>
                    setContent({ ...content, [key]: e.target.checked })} />
              </li>
            ))}
          </ul>
        </div>

        {picked.size > 0 && (
          <p className="text-xs text-zinc-500">
            {picked.size} night{picked.size === 1 ? '' : 's'} selected — up to{' '}
            {gb(selectedBytes)} GB before filtering by content type. Nothing is
            removed from the camera.
          </p>
        )}

        {progress && (
          <div className="rounded-xl bg-zinc-800/60 p-4">
            <div className="flex justify-between text-sm">
              <span className={progress.state === 'error'
                ? 'text-rose-400' : 'text-zinc-300'}>
                {progress.state === 'running' ? 'Copying…'
                  : progress.state === 'done' ? 'Export complete'
                  : progress.error ?? progress.state}
              </span>
              <span className="tabular-nums text-zinc-400">{pct}%</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-zinc-700">
              <div className={`h-full ${
                progress.state === 'error' ? 'bg-rose-500' : 'bg-sky-500'}`}
                style={{ width: `${pct}%` }} />
            </div>
            <p className="mt-2 truncate text-xs text-zinc-500">
              {progress.files_done ?? 0}/{progress.files_total ?? 0} files
              {progress.current_file ? ` · ${progress.current_file}` : ''}
            </p>
          </div>
        )}

        <div className="flex gap-3">
          <Button tone="accent" className="flex-1" onClick={start}
            disabled={busy || !picked.size || progress?.state === 'running'}>
            {progress?.state === 'running' ? 'Exporting…' : 'Start export'}
          </Button>
          <Button onClick={eject} disabled={progress?.state === 'running'}>
            Eject
          </Button>
        </div>
      </div>
      )}
    </Card>
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
  // Bumped after a delete so the index is pulled again — the frames on screen
  // are gone and showing them would offer a fullscreen view of a 404.
  const [reloadKey, setReloadKey] = useState(0)

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
  }, [cameraId, night, reloadKey])

  // Keeper-saved frames are the ones worth finding again, and a badge alone
  // does not help when they are three needles in 1200 frames.
  const [dngOnly, setDngOnly] = useState(false)
  const dngCount = frames.reduce((n, f) => n + (f.has_dng ? 1 : 0), 0)
  const shown = dngOnly ? frames.filter((f) => f.has_dng) : frames

  const frame = shown[index]
  const frameUrl = frame
    ? `/api/nights/${cameraId}/${night}/frame/${frame.name}` : null
  const dngUrl = frame?.has_dng
    ? `/api/nights/${cameraId}/${night}/raw/${frame.name.replace(/\.jpg$/, '.dng')}`
    : null

  const nearestTo = useCallback((list, ts) => {
    let best = 0, bestGap = Infinity
    list.forEach((f, i) => {
      const gap = Math.abs(f.timestamp - ts)
      if (gap < bestGap) { bestGap = gap; best = i }
    })
    return best
  }, [])

  // Toggling the filter keeps you at the same moment in the night rather than
  // dumping you back at the start.
  const toggleDngOnly = () => {
    const ts = shown[index]?.timestamp
    const next = !dngOnly
    const list = next ? frames.filter((f) => f.has_dng) : frames
    setDngOnly(next)
    setIndex(list.length && ts != null ? nearestTo(list, ts) : 0)
  }

  const jumpDng = (dir) => {
    if (dngOnly) {
      setIndex((i) => Math.min(shown.length - 1, Math.max(0, i + dir)))
      return
    }
    const from = index + dir
    const step = dir > 0 ? 1 : -1
    for (let i = from; i >= 0 && i < shown.length; i += step) {
      if (shown[i].has_dng) { setIndex(i); return }
    }
  }

  // Chart click -> nearest frame in time. This is the whole point of the chart:
  // spot the clear stretch, jump straight to it.
  const seekToTime = useCallback((ts) => {
    if (shown.length) setIndex(nearestTo(shown, ts))
  }, [shown, nearestTo])

  return (
    <div className="mt-6 flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">{night}</h2>
        <Button onClick={onBack}>All nights</Button>
      </div>

      <TimelapsePanel cameraId={cameraId} night={night} present={hasTimelapse}
        showToast={showToast} onRendered={() => setHasTimelapse(true)} />

      <TidyUp cameraId={cameraId} night={night} frames={frames}
        current={frame} showToast={showToast} onBack={onBack}
        onDeleted={() => { setIndex(0); setReloadKey((k) => k + 1) }} />

      <Card title="Frames"
        right={<span className="text-sm text-zinc-500">
          {shown.length ? `${index + 1} / ${shown.length.toLocaleString()}` : '—'}
          {dngOnly && ' with RAW'}
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
              type="range" min={0} max={Math.max(0, shown.length - 1)} value={index}
              onChange={(e) => setIndex(Number(e.target.value))}
              className="mt-4 w-full accent-sky-500"
              aria-label="Scrub through the night" />

            <Filmstrip cameraId={cameraId} night={night} frames={shown}
              index={index} onPick={setIndex} />

            {dngCount > 0 && (
              <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
                <button onClick={toggleDngOnly}
                  aria-pressed={dngOnly}
                  className={`rounded-lg border px-3 py-1.5 transition ${
                    dngOnly ? 'border-emerald-500 bg-emerald-600/20 text-emerald-300'
                            : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'}`}>
                  DNG frames ({dngCount})
                </button>
                <button onClick={() => jumpDng(-1)}
                  className="rounded-lg border border-zinc-700 px-3 py-1.5
                             text-zinc-400 hover:bg-zinc-800">
                  ‹ prev
                </button>
                <button onClick={() => jumpDng(1)}
                  className="rounded-lg border border-zinc-700 px-3 py-1.5
                             text-zinc-400 hover:bg-zinc-800">
                  next ›
                </button>
                <span className="text-xs text-zinc-500">
                  {dngOnly ? 'Showing only frames with RAW' : 'Jump between RAW frames'}
                </span>
              </div>
            )}
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


/**
 * Getting rid of frames, which nothing could do before this.
 *
 * The only thing that deleted anything was the low-space cleanup, and that
 * takes the OLDEST night first — precisely backwards for the case that actually
 * turns up, which is an evening of setup junk in the folder you are still
 * filling. Hence the middle option: clear what is there now and keep the night
 * running, without touching what arrives next.
 */
function TidyUp({ cameraId, night, frames, current, showToast, onDeleted, onBack }) {
  const [pending, setPending] = useState(null)
  const [busy, setBusy] = useState(false)

  const run = async (url, done, after) => {
    setBusy(true)
    const r = await fetch(url, { method: 'DELETE' }).catch(() => null)
    setBusy(false)
    setPending(null)
    if (!r?.ok) return showToast?.('Could not delete that')
    const body = await r.json().catch(() => ({}))
    showToast?.(done(body))
    after?.()
  }

  return (
    <Card title="Tidy up">
      <p className="mt-1 text-sm text-zinc-400">
        Setting a camera up leaves a lot of frames that are of nothing. Deleting
        them is permanent — there is no undo, and nothing is copied anywhere
        first.
      </p>

      <div className="mt-4 space-y-3">
        <Button className="w-full" disabled={busy || !current}
          onClick={() => setPending({ kind: 'frame' })}>
          Delete this frame
        </Button>
        <Button className="w-full" disabled={busy || !frames.length}
          onClick={() => setPending({ kind: 'sofar' })}>
          Delete everything up to now
        </Button>
        <Button tone="warn" className="w-full" disabled={busy || !frames.length}
          onClick={() => setPending({ kind: 'night' })}>
          Delete this whole night
        </Button>
      </div>

      <ConfirmDialog
        open={!!pending}
        title={pending?.kind === 'frame' ? 'Delete this frame?'
          : pending?.kind === 'sofar' ? 'Delete everything so far?'
            : 'Delete the whole night?'}
        consequence={pending?.kind === 'frame'
          ? `${current?.name} and its RAW file, if it has one. Permanent.`
          : pending?.kind === 'sofar'
            ? `Every frame captured before right now — about `
              + `${frames.length.toLocaleString()} of them — is deleted, and `
              + `the camera carries on filling this night with whatever comes `
              + `next. The timelapse, if there is one, is left alone. Permanent.`
            : `All ${frames.length.toLocaleString()} frames and the timelapse `
              + `are deleted and the night disappears from this list. `
              + `Permanent.`}
        confirmLabel="Delete"
        tone="warn"
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (pending.kind === 'frame') {
            return run(`/api/nights/${cameraId}/${night}/frame/${current.name}`,
                       () => 'Frame deleted', onDeleted)
          }
          if (pending.kind === 'sofar') {
            return run(
              `/api/nights/${cameraId}/${night}?before=${Date.now() / 1000}`,
              (b) => `${b.frames_deleted.toLocaleString()} frames deleted`,
              onDeleted)
          }
          return run(`/api/nights/${cameraId}/${night}`,
                     (b) => `Night deleted (${b.frames_deleted.toLocaleString()} frames)`,
                     onBack)
        }} />
    </Card>
  )
}

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
