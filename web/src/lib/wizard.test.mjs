// Run: node web/src/lib/wizard.test.mjs
//
// The wizard is the front door: it is the only screen most people will judge
// the product by, and it runs exactly once per camera, in a shed, on a phone,
// often with no internet. The states worth asserting are the ones that are
// hardest to stage on purpose — a declined permission prompt, an offline
// camera, a location nobody set.
import assert from 'node:assert'
import {
  STEPS, canContinue, canGoBack, formatCoords, nextStep, prevStep,
  resolveLocation, startsWhen, stepIndex,
} from './wizard.js'

const NOW = 1_786_000_000

// -- navigation --------------------------------------------------------------

assert.equal(nextStep('welcome'), 'network')
assert.equal(prevStep('network'), 'welcome')
assert.equal(stepIndex('nonsense'), 0, 'an unknown step must not break the dots')

// The ends are walls, not wraps: Back on the first screen must not land on the
// summary, and Continue on the last must not loop to the beginning.
assert.equal(prevStep('welcome'), 'welcome')
assert.equal(nextStep('done'), 'done')
assert.equal(canGoBack('welcome'), false)
assert.equal(canGoBack('done'), true, 'Back works on every screen but the first')

for (const step of STEPS) {
  assert.ok(STEPS.includes(nextStep(step)) && STEPS.includes(prevStep(step)))
}

// -- what gates Continue -----------------------------------------------------
//
// Continue-through-without-touching has to yield a good config, so almost
// nothing is gated. Location is the exception: it cannot be guessed, and a
// camera at 0,0 schedules its night for the Gulf of Guinea.

for (const step of ['welcome', 'network', 'camera', 'capture', 'security',
                    'notifications', 'done']) {
  assert.equal(canContinue(step, {}), true, `${step} should not block`)
}
assert.equal(canContinue('location', {}), false)
assert.equal(canContinue('location', { location: { latitude: 42.73, longitude: -87.78 } }), true)
assert.equal(canContinue('location', { location: { latitude: 0, longitude: 0 } }), false,
             'null island accepted as a real location')
assert.equal(canContinue('location', { location: { latitude: 95, longitude: 0 } }), false)
assert.equal(canContinue('location', { location: { latitude: '42.7', longitude: '-87' } }), false,
             'strings from an input box are not coordinates')

// A real place that happens to be near zero on one axis is still a real place.
assert.equal(canContinue('location', { location: { latitude: 51.5, longitude: 0.1 } }), true,
             'Greenwich rejected')

// -- the location cascade ----------------------------------------------------

const position = { coords: { latitude: 42.7310299, longitude: -87.7834501 } }
const estimate = { latitude: 42.73, longitude: -87.78, timezone: 'America/Chicago',
                   place: 'Racine, Wisconsin' }

const granted = await resolveLocation({
  browser: async () => position,
  ip: async () => { throw new Error('should not be reached') },
})
assert.equal(granted.source, 'browser')
assert.equal(granted.approximate, false)
assert.equal(granted.latitude, 42.73103, 'coordinates should be rounded, not truncated')

const denied = await resolveLocation({
  browser: async () => { throw { code: 1 } },
  ip: async () => estimate,
})
assert.equal(denied.source, 'ip')
assert.equal(denied.approximate, true, 'an IP estimate must be labelled approximate')
assert.equal(denied.place, 'Racine, Wisconsin')

// Both rungs gone — the normal case for this device, and not a failure state.
const offline = await resolveLocation({
  browser: async () => { throw { code: 1 } },
  ip: async () => { throw new Error('503') },
})
assert.equal(offline.source, 'manual')
assert.match(offline.reason, /declined/, 'a declined prompt should say so')

const noSignal = await resolveLocation({
  browser: async () => { throw { code: 2 } },
  ip: async () => { throw new Error('503') },
})
assert.equal(noSignal.source, 'manual')
assert.doesNotMatch(noSignal.reason, /declined/,
                    'blamed the user for a permission they never denied')

// -- the summary line --------------------------------------------------------

assert.match(startsWhen({ period: 'night' }), /capturing now/,
             'told someone finishing at 11pm to wait for dusk')
assert.match(startsWhen({ period: 'day', sunset: null }), /at dusk\.$/,
             'invented a time inside the polar circle')
assert.match(startsWhen({ period: 'day', sunset: NOW + 3600, now: NOW }), /tonight/)

// -- coordinates -------------------------------------------------------------

assert.equal(formatCoords(42.73, -87.78), '42.73000, -87.78000')
assert.equal(formatCoords(undefined, undefined), '')

console.log('  all assertions passed')
