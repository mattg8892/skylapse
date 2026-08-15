// Renders the dashboard's liveness pill and countdown for a set of states,
// straight from the real logic. Run: node web/src/lib/capture.test.mjs
//
// This exists because the states that matter — a rig stalled at 3am, a phone
// whose clock is four hours out — cannot be staged in a browser on demand, and
// because the pill is the one thing on the dashboard someone checks from bed.
import assert from 'node:assert'
import {
  countdownFor, countdownText, idleDetail, pillFor, serverNow,
} from './capture.js'

const NOW = 1_786_000_000            // fixed "server time" for reproducibility

/** A status payload shaped exactly like /api/status. */
function status({ state = 'capturing', frameAgo = 2, exposure_us = 1_000_000,
                  gap_s = 5, threshold = 60, frames = 42, dusk = null } = {}) {
  return {
    server_time: NOW,
    daemon: { state, ...(dusk ? { dusk } : {}) },
    capture: {
      frame_at: frameAgo === null ? null : NOW - frameAgo,
      exposure_us, gap_s, stall_threshold_s: threshold, frames_tonight: frames,
    },
  }
}

const cases = [
  ['fresh frame, mid-gap', status({ frameAgo: 2 })],
  ['long exposure, exposing phase', status({ frameAgo: 31, exposure_us: 25_000_000, gap_s: 5, threshold: 90 })],
  ['overdue but not yet stalled', status({ frameAgo: 30, threshold: 60 })],
  ['past the watchdog threshold', status({ frameAgo: 400, threshold: 60 })],
  ['estimate exhausted, no frame yet', status({ frameAgo: 6.5 })],
  ['night-only rig idling', status({ state: 'idle_day', dusk: NOW + 3600 })],
  ['focus mode', status({ state: 'focusing' })],
  ['safety pause', status({ state: 'paused_safety' })],
  ['camera unplugged', status({ state: 'no_camera' })],
  ['no frame captured yet', status({ frameAgo: null })],
]

console.log('  state                              pill        countdown')
console.log('  ' + '-'.repeat(72))
for (const [name, s] of cases) {
  const now = serverNow(s, NOW * 1000)
  const pill = pillFor(s, now)
  const text = countdownText(countdownFor(s, now)) ?? '—'
  const dusk = pill.key === 'idle_day' ? ` at ${idleDetail(s)}` : ''
  console.log(`  ${name.padEnd(34)} ${(pill.tone + ' ' + pill.label + dusk).padEnd(11)} ${text}`)
}

// -- assertions -------------------------------------------------------------

const fresh = status({ frameAgo: 2 })
assert.equal(pillFor(fresh, serverNow(fresh, NOW * 1000)).key, 'capturing')
assert.equal(pillFor(status({ frameAgo: 30 }), NOW).key, 'waiting')
assert.equal(pillFor(status({ frameAgo: 400 }), NOW).key, 'stalled')
assert.equal(pillFor(status({ state: 'idle_day' }), NOW).key, 'idle_day')

// Freshness must beat the daemon's self-report: a wedged daemon says
// "capturing" right up until someone notices.
assert.equal(pillFor(status({ state: 'capturing', frameAgo: 9999 }), NOW).key,
             'stalled', 'trusted the daemon over the frames')

// Countdown never goes negative and never sticks at a stale number.
for (const ago of [0, 3, 6, 6.5, 12, 500]) {
  const c = countdownFor(status({ frameAgo: ago }), NOW)
  assert.ok(c.seconds >= 0, `negative countdown at ago=${ago}`)
}
assert.equal(countdownText(countdownFor(status({ frameAgo: 500 }), NOW)),
             'Next frame any moment')

// Clock skew: a browser four hours fast must still read the frame as fresh.
const skewed = serverNow(fresh, (NOW + 4 * 3600) * 1000)
assert.equal(pillFor(fresh, skewed).key, 'capturing', 'clock skew broke the pill')

// Quiet states never show a countdown — there is no next frame coming.
for (const state of ['idle_day', 'focusing', 'paused_safety', 'no_camera']) {
  assert.equal(countdownFor(status({ state }), NOW), null, `${state} showed a countdown`)
}

console.log('\n  all assertions passed')
