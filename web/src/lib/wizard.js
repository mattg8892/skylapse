// Wizard navigation and the location cascade. Pure, so the parts that are
// awkward to reach in a browser — a denied geolocation prompt, a camera with
// no internet, a phone that never answers — can be asserted instead of
// stumbled into on someone's first five minutes with the product.

// Camera setup is deliberately not here. It lives in Settings → Cameras, which
// does everything this screen did and more, and which is where you end up
// anyway the first time a camera is unplugged, replaced or added to. A wizard
// step that can reboot the Pi halfway through first-run setup is also the worst
// possible place to discover a camera the Pi cannot auto-detect.
export const STEPS = [
  'welcome', 'network', 'location', 'capture',
  'security', 'notifications', 'done',
]

export function stepIndex(step) {
  const i = STEPS.indexOf(step)
  return i < 0 ? 0 : i
}

export function nextStep(step) {
  return STEPS[Math.min(stepIndex(step) + 1, STEPS.length - 1)]
}

export function prevStep(step) {
  return STEPS[Math.max(stepIndex(step) - 1, 0)]
}

/** Back exists everywhere except the first screen. */
export function canGoBack(step) {
  return stepIndex(step) > 0
}

/**
 * Whether Continue should be enabled.
 *
 * Deliberately permissive: every screen except location can be walked through
 * without touching anything and still leave a sane camera. Location is the one
 * that cannot be guessed — a camera at 0,0 schedules its night for the Gulf of
 * Guinea — so it is the only gate.
 */
export function canContinue(step, draft) {
  if (step !== 'location') return true
  const { latitude, longitude } = draft?.location ?? {}
  if (typeof latitude !== 'number' || typeof longitude !== 'number') return false
  if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return false
  return !(Math.abs(latitude) < 0.5 && Math.abs(longitude) < 0.5)
}

/**
 * The location cascade from DESIGN.md, as a sequence of attempts.
 *
 * Browser geolocation first: it is one tap, it is exact, and it brings the
 * timezone with it. On denial or failure, a city-level estimate from the
 * camera's public IP — good enough to put sunset within minutes, which is all
 * the scheduler needs. Manual entry is always available and is the only rung
 * that works with no internet at all, which is the normal case here.
 *
 * `browser` and `ip` are injected so the chain can be tested without a browser
 * or a network.
 */
export async function resolveLocation({ browser, ip }) {
  try {
    const position = await browser()
    return {
      latitude: round5(position.coords.latitude),
      longitude: round5(position.coords.longitude),
      timezone: browserTimezone(),
      source: 'browser',
      approximate: false,
    }
  } catch (browserError) {
    try {
      const estimate = await ip()
      return {
        latitude: round5(estimate.latitude),
        longitude: round5(estimate.longitude),
        // The camera's timezone guess is worth less than the phone's, which is
        // the device the person is actually standing in the timezone of.
        timezone: browserTimezone() || estimate.timezone,
        place: estimate.place,
        source: 'ip',
        approximate: true,
      }
    } catch {
      // Both rungs gone. Manual entry is not a failure state, it is the
      // documented offline path — so this reports why, not that nothing works.
      return {
        source: 'manual',
        reason: browserError?.code === 1
          ? 'Location permission was declined.'
          : 'Couldn’t determine location automatically.',
      }
    }
  }
}

function round5(value) {
  return Math.round(Number(value) * 1e5) / 1e5
}

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
}

/** "42.73000, -87.78000" — fixed width so it stops jumping as sliders move. */
export function formatCoords(latitude, longitude) {
  if (typeof latitude !== 'number' || typeof longitude !== 'number') return ''
  return `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`
}

/**
 * What the done screen says about when capture starts.
 *
 * "starts at dusk tonight" is wrong if it is already dark — someone finishing
 * setup at 11pm should be told it is running now, because it is.
 */
export function startsWhen({ period, sunset, now }) {
  if (period && period !== 'day') return 'Skylapse is capturing now.'
  if (!sunset) return 'Skylapse starts capturing at dusk.'
  const when = new Date(sunset * 1000)
  const sameDay = now && new Date(now * 1000).getDate() === when.getDate()
  const time = when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return `Skylapse starts capturing at dusk ${sameDay ? 'tonight' : ''}, ${time}.`
    .replace(' ,', ',')
}
