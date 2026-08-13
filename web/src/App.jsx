import { useEffect, useState } from 'react'
import ConnectionScreen from './screens/ConnectionScreen.jsx'
import Dashboard from './screens/Dashboard.jsx'

const POLL_MS = 5000

export default function App() {
  const [status, setStatus] = useState(null)

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

  useEffect(() => {
    let alive = true
    const poll = () =>
      fetch('/api/status')
        .then((r) => r.json())
        .then((s) => alive && setStatus(s))
        .catch(() => {})
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (!status) {
    return (
      <div className="min-h-screen grid place-items-center text-zinc-400">
        Connecting to camera…
      </div>
    )
  }

  const netState = status.network?.state
  const showConnection = netState === 'hotspot' || netState === 'standalone'

  return showConnection && !status.network?.session_standalone_dismissed ? (
    <ConnectionScreen status={status} />
  ) : (
    <Dashboard status={status} />
  )
}
