// Liveness and countdown logic for the dashboard.
//
// Pure on purpose. These decide whether the camera looks alive, which is the
// one thing on the dashboard someone checks from bed — and the interesting
// inputs (a stalled rig at 3am, a clock 4 hours out) are impossible to stage in
// a browser and trivial to pass in as arguments.

export const PILL = {
  capturing: { label: 'Capturing', tone: 'green' },
  waiting: { label: 'Waiting', tone: 'amber' },
  stalled: { label: 'Stalled', tone: 'red' },
  idle_day: { label: 'Idle until dusk', tone: 'blue' },
  focusing: { label: 'Focus mode', tone: 'sky' },
  paused_safety: { label: 'Paused (safety)', tone: 'amber' },
  no_camera: { label: 'No camera', tone: 'red' },
  starting: { label: 'Starting…', tone: 'zinc' },
  unknown: { label: 'Unknown', tone: 'zinc' },
}

// A frame is "fresh" within this multiple of the expected cadence. Beyond it
// the rig is late but not yet alarming; past the watchdog's own threshold it is
// the same condition that sends a notification.
export const FRESH_FACTOR = 1.5

/** Server-clock now, in seconds, corrected for the browser's own clock skew. */
export function serverNow(status, clientNowMs = Date.now()) {
  const skewMs = (status?.server_time ?? 0) * 1000 - clientNowMs
  return (clientNowMs + skewMs) / 1000
}

export function cadenceSeconds(capture) {
  const gap = capture?.gap_s ?? 0
  const exposure = (capture?.exposure_us ?? 0) / 1e6
  return Math.max(1, gap + exposure)
}

/**
 * Which pill to show.
 *
 * States where not capturing is correct (idle, focus, safety pause, no camera)
 * win outright — freshness is meaningless there and would wrongly read as a
 * fault. Otherwise the verdict comes from how old the newest frame is, NOT
 * from the daemon's own state string: "capturing" is exactly what a wedged
 * daemon reports right up until someone notices.
 */
export function pillFor(status, nowSeconds) {
  const state = status?.daemon?.state
  if (state && state !== 'capturing') {
    return { key: state in PILL ? state : 'unknown', ...(PILL[state] ?? PILL.unknown) }
  }

  const capture = status?.capture
  const frameAt = capture?.frame_at
  if (!state) return { key: 'unknown', ...PILL.unknown }
  if (!frameAt) return { key: 'starting', ...PILL.starting }

  const age = Math.max(0, nowSeconds - frameAt)
  const cadence = cadenceSeconds(capture)
  // Same number the watchdog uses, handed over by the API, so the pill and the
  // notification can never disagree about what counts as stalled.
  const threshold = capture?.stall_threshold_s ?? Math.max(60, 3 * cadence)

  if (age <= FRESH_FACTOR * cadence) return { key: 'capturing', ...PILL.capturing, age }
  if (age < threshold) return { key: 'waiting', ...PILL.waiting, age }
  return { key: 'stalled', ...PILL.stalled, age }
}

/**
 * Two-phase countdown to the next frame.
 *
 * The daemon's cycle from one capture's start is: expose, save, then sleep the
 * gap. So the next exposure begins at frame_at + exposure + gap, and runs for
 * another exposure. Saving and analysis are unmodelled, which is why the
 * estimate can run out before a frame lands — hence the "any moment" fallback
 * rather than a countdown that goes negative or sticks at zero.
 */
export function countdownFor(status, nowSeconds) {
  const capture = status?.capture
  const state = status?.daemon?.state
  if (state !== 'capturing' || !capture?.frame_at) return null

  const exposure = (capture.exposure_us ?? 0) / 1e6
  const gap = capture.gap_s ?? 0
  const nextStart = capture.frame_at + exposure + gap
  const nextEnd = nextStart + exposure

  if (nowSeconds < nextStart) {
    return { phase: 'gap', seconds: Math.ceil(nextStart - nowSeconds) }
  }
  if (nowSeconds < nextEnd) {
    return { phase: 'exposing', seconds: Math.ceil(nextEnd - nowSeconds) }
  }
  return { phase: 'due', seconds: 0 }
}

export function countdownText(countdown) {
  if (!countdown) return null
  if (countdown.phase === 'due') return 'Next frame any moment'
  const s = Math.max(0, countdown.seconds)
  return countdown.phase === 'exposing'
    ? `Exposing… ${s}s remaining`
    : `Next frame in ${s}s`
}

/** "Idle until dusk at 20:14" needs the dusk the daemon computed. */
export function idleDetail(status) {
  const dusk = status?.daemon?.dusk
  if (!dusk) return null
  return new Date(dusk * 1000)
    .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
