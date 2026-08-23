// Run: node web/src/lib/errors.test.mjs
//
// The case that matters is the array-of-objects one: it is the shape FastAPI
// returns for every validation error, and rendering it blanks the page.
import assert from 'node:assert'
import { errorText } from './errors.js'

// -- the shape that caused the black screen ---------------------------------

const VALIDATION_422 = {
  detail: [{ type: 'missing', loc: ['body'], msg: 'Field required', input: null }],
}
assert.equal(typeof errorText(VALIDATION_422), 'string',
  'a 422 body must flatten to a string, never an object')
assert.equal(errorText(VALIDATION_422), 'Field required')

// The real one from the dew heater test button, with a named field.
assert.equal(
  errorText({ detail: [{ loc: ['body', 'seconds'], msg: 'Input should be a valid number' }] }),
  'seconds: Input should be a valid number',
  'the field should be named, but not as "body.seconds"')

// Several at once.
assert.equal(
  errorText({ detail: [
    { loc: ['body', 'a'], msg: 'Field required' },
    { loc: ['body', 'b'], msg: 'Field required' },
  ] }),
  'a: Field required; b: Field required')

// -- the ordinary shape -----------------------------------------------------

assert.equal(errorText({ detail: 'Not enough frames or ffmpeg unavailable' }),
  'Not enough frames or ffmpeg unavailable')
assert.equal(errorText('plain string'), 'plain string')

// -- nothing useful ---------------------------------------------------------

assert.equal(errorText(null, 'fallback'), 'fallback')
assert.equal(errorText(undefined, 'fallback'), 'fallback')
assert.equal(errorText({}, 'fallback'), 'fallback')
assert.equal(errorText({ detail: [] }, 'fallback'), 'fallback')
assert.equal(errorText({ detail: {} }, 'fallback'), 'fallback',
  'an unreadable object must fall back, not render as [object Object]')
assert.equal(errorText({ detail: '   ' }, 'fallback'), 'fallback',
  'whitespace is not a message')

// -- other shapes the API uses ----------------------------------------------

assert.equal(errorText({ error: 'could not drive the pin' }), 'could not drive the pin')
assert.equal(errorText({ message: 'nope' }), 'nope')

// -- the invariant, stated once ---------------------------------------------

for (const body of [
  VALIDATION_422, null, undefined, {}, [], 'x', 42, { detail: { a: { b: 1 } } },
  { detail: [null, undefined, {}] },
]) {
  assert.equal(typeof errorText(body), 'string',
    `errorText must always return a string, got ${typeof errorText(body)}`)
}

console.log('errors.test.mjs: all assertions passed')
