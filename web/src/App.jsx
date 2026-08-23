import { useCallback, useEffect, useState } from 'react'
import ConnectionScreen from './screens/ConnectionScreen.jsx'
import Dashboard from './screens/Dashboard.jsx'
import LoginScreen from './screens/LoginScreen.jsx'
import WizardScreen from './screens/WizardScreen.jsx'
import { showsConnectionScreen } from './lib/network.js'

const POLL_MS = 5000

/**
 * ?setup=preview walks the wizard on a camera that is already configured,
 * without touching its config. Setup runs exactly once per camera, so without
 * this the only way to look at the front door is to break the house.
 */
function previewingSetup() {
  return new URLSearchParams(window.location.search).get('setup') === 'preview'
}

export default function App() {
  const [status, setStatus] = useState(null)
  const [gate, setGate] = useState(null)        // auth status, or null until known
  const [preview, setPreview] = useState(previewingSetup)

  // Offer the browser's clock on every session start. The backend applies it
  // only when it has no better source (NTP > RTC > browser) and drift > 5s.
  useEffect(() => {
    fetch('/api/time/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        epoch_ms: Date.now(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }),
    }).catch(() => {})
  }, [])

  const checkAuth = useCallback(() => {
    fetch('/api/auth/status').then((r) => r.json()).then(setGate)
      .catch(() => setGate({ password_set: false, authenticated: true }))
  }, [])

  useEffect(() => { checkAuth() }, [checkAuth])

  useEffect(() => {
    let alive = true
    const poll = () =>
      fetch('/api/status')
        .then((r) => (r.status === 401 ? null : r.json()))
        .then((s) => alive && s && setStatus(s))
        .catch(() => {})
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [gate])

  // The login screen comes before everything, including the wizard: a camera
  // someone has already protected must not hand its setup flow to the next
  // person on the network.
  if (gate && gate.password_set && !gate.authenticated) {
    return <LoginScreen onIn={checkAuth} />
  }

  if (!status) {
    return (
      <div className="min-h-screen grid place-items-center text-zinc-400">
        Connecting to camera…
      </div>
    )
  }

  // Setup owns the screen until it is finished. It is the front door, and a
  // half-configured camera has nothing useful to show behind it.
  if (preview || status.setup_complete === false) {
    return (
      <WizardScreen preview={preview}
        onDone={() => {
          setPreview(false)
          if (previewingSetup()) window.location.search = ''
          fetch('/api/status').then((r) => r.json()).then(setStatus).catch(() => {})
        }} />
    )
  }

  return showsConnectionScreen(status.network) ? (
    <ConnectionScreen status={status} />
  ) : (
    <Dashboard status={status} />
  )
}
