// Run: node web/src/lib/network.test.mjs
//
// The distinction being defended here is the one a user actually cares about:
// "I put it in access-point mode" versus "your Wi-Fi is broken". Both leave the
// camera serving the same SSID, and if the badge conflates them, a fallback
// looks like a setting and nobody investigates.
import assert from 'node:assert'
import { networkBadge, remainingText } from './network.js'

const NOW = 1_786_000_000

const cases = [
  ['connected', { state: 'connected' }],
  ['manual, sticky', { state: 'standalone', hotspot_until: 0 }],
  ['manual, 2h left', { state: 'standalone', hotspot_until: NOW + 7200 }],
  ['manual, 4m left', { state: 'standalone', hotspot_until: NOW + 240 }],
  ['manual, expired', { state: 'standalone', hotspot_until: NOW - 5 }],
  ['automatic fallback', { state: 'hotspot' }],
]

for (const [name, net] of cases) {
  const badge = networkBadge(net, NOW)
  console.log(`  ${name.padEnd(20)} ${badge ? `[${badge.label}]` : '(no badge)'}`)
}

assert.equal(networkBadge({ state: 'connected' }, NOW), null)
assert.equal(networkBadge(null, NOW), null)

// A fallback must never be mistaken for a setting someone chose.
assert.equal(networkBadge({ state: 'hotspot' }, NOW).key, 'fallback')
assert.equal(networkBadge({ state: 'standalone' }, NOW).key, 'sticky')
assert.notEqual(networkBadge({ state: 'hotspot' }, NOW).tone,
                networkBadge({ state: 'standalone' }, NOW).tone)

// Sticky mode has nothing to count down; "0 left" would read as expiring.
assert.ok(!networkBadge({ state: 'standalone', hotspot_until: 0 }, NOW)
  .label.includes('left'))

assert.equal(networkBadge({ state: 'standalone', hotspot_until: NOW + 7200 }, NOW)
  .label, 'Access point · 2h left')

// Past its deadline the badge clamps at zero rather than counting up: netwatch
// clears the mode within a poll, and a negative countdown in that window would
// be the only thing on screen that looked broken.
assert.equal(networkBadge({ state: 'standalone', hotspot_until: NOW - 500 }, NOW)
  .label, 'Access point · 0s left')

assert.equal(remainingText(0), '0s')
assert.equal(remainingText(59), '59s')
assert.equal(remainingText(60), '1m')
assert.equal(remainingText(3600), '1h')
assert.equal(remainingText(7080), '1h 58m')
assert.equal(remainingText(3599), '59m')
assert.equal(remainingText(7199), '2h', 'rounded into a bogus "1h 60m"')
assert.equal(remainingText(null), null)

console.log('\n  all assertions passed')
