// What the network badge says. Pure, so the states that matter — a timed
// access-point session with four minutes left, a sticky one that is not
// counting down at all — can be asserted instead of stumbled into.

/** Human duration for a countdown: "1h 58m", "9m", "40s". */
export function remainingText(seconds) {
  if (seconds === null || seconds === undefined) return null
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  // 119.6 minutes rounding to "1h 60m" reads as broken, so carry it.
  if (h && m === 60) return `${h + 1}h`
  return h ? (m ? `${h}h ${m}m` : `${h}h`) : `${Math.floor(s / 60)}m`
}

/**
 * The dashboard badge, or null when the camera is on Wi-Fi and there is
 * nothing worth saying.
 *
 * `net` is the `network` block of /api/status (netwatch's own status file),
 * `now` is server time in seconds.
 */
export function networkBadge(net, now) {
  if (!net) return null
  const ap = net.state === 'hotspot' || net.state === 'standalone'
  if (!ap) return null

  const ssid = net.hotspot_ssid || 'Skylapse-Setup'
  const until = net.hotspot_until || 0
  // Manual access-point mode is the user's own choice, so it is stated
  // plainly. The automatic fallback means Wi-Fi is not working, which is a
  // problem the user has not been told about yet — those must not look alike.
  const manual = net.state === 'standalone'

  if (manual && until) {
    const left = remainingText(Math.max(0, until - now))
    return { key: 'timed', tone: 'amber', label: `Access point · ${left} left`,
             detail: `Joinable as ${ssid}. Returns to Wi-Fi when the time is up.` }
  }
  if (manual) {
    return { key: 'sticky', tone: 'amber', label: 'Access point',
             detail: `Joinable as ${ssid}. Stays here until you switch it back.` }
  }
  return { key: 'fallback', tone: 'red', label: 'Wi-Fi unavailable',
           detail: `Couldn't reach your network, so the camera is serving ${ssid}.` }
}


/**
 * Whether to replace the whole app with the "No Wi-Fi connection" screen.
 *
 * Only the automatic fallback earns it. Reaching the camera through its own
 * access point is not by itself a problem to report: 'standalone' means
 * somebody chose this, and answering their choice with a failure screen is
 * both wrong and alarming.
 *
 * It is also what made that screen's own "Use in standalone mode" button look
 * dead: choosing standalone moves the state underneath, and the screen used to
 * stay up regardless, so nothing appeared to happen.
 */
export function showsConnectionScreen(net) {
  return net?.state === 'hotspot'
}
