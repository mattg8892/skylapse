/**
 * Turning whatever the server said into something safe to render.
 *
 * FastAPI has two error shapes. A raised HTTPException gives `detail` as a
 * string, which is what every call site here assumed. A *validation* error
 * gives `detail` as an array of objects -- {type, loc, msg, input} -- and
 * putting one of those into JSX throws "Objects are not valid as a React
 * child" during render. React then unmounts the whole tree, so the page goes
 * black and the only trace is in the browser console.
 *
 * That is not a hypothetical either: the dew heater's test button posted no
 * body to an endpoint that required one, got a 422, and took the settings page
 * down with it. Nine call sites had the same latent bug, all one bad request
 * away from the same black screen.
 *
 * So: never let a server error reach the DOM unflattened.
 */

/** A short, human-readable message for any error body. Always a string. */
export function errorText(body, fallback = 'That did not work') {
  const detail = body?.detail ?? body
  const text = flatten(detail)
  return text || fallback
}

function flatten(detail) {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail.trim()
  if (typeof detail === 'number' || typeof detail === 'boolean') return String(detail)

  if (Array.isArray(detail)) {
    return detail.map(flatten).filter(Boolean).join('; ')
  }

  if (typeof detail === 'object') {
    // A pydantic validation entry. `loc` is like ["body", "seconds"]; the
    // leading "body"/"query" is noise to anyone reading a toast.
    if (typeof detail.msg === 'string') {
      const field = Array.isArray(detail.loc)
        ? detail.loc.filter((p) => typeof p === 'string'
                                && !['body', 'query', 'path'].includes(p)).join('.')
        : ''
      return field ? `${field}: ${detail.msg}` : detail.msg
    }
    if (typeof detail.detail === 'string') return detail.detail
    if (typeof detail.error === 'string') return detail.error
    if (typeof detail.message === 'string') return detail.message
    return ''      // an object with nothing readable is worse than the fallback
  }

  return ''
}
