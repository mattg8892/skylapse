import { useState } from 'react'

// Implements the flow from DESIGN.md: saved networks with reasons, three
// actions (Try again / Connect new / Access point), "always" checkbox,
// and the time-sync card that appears only when the camera's
// clock source is browser-only.

export default function ConnectionScreen({ status }) {
  const [showStandalone, setShowStandalone] = useState(false)
  const [alwaysStandalone, setAlwaysStandalone] = useState(false)
  const [synced, setSynced] = useState(false)
  const [busy, setBusy] = useState(null)

  const authFailures = status.network?.auth_failures ?? {}
  const cameraTime = status.server_time ? new Date(status.server_time * 1000) : null
  const drift = cameraTime ? Math.abs(Date.now() - cameraTime.getTime()) / 1000 : 0

  const tryAgain = async () => {
    if (!window.confirm(
      'This disconnects your phone from the camera for up to 90 seconds while ' +
      'it tries your Wi-Fi. If it fails, rejoin the Skylapse-Setup access point.')) return
    setBusy('retry')
    await fetch('/api/network/retry', { method: 'POST' })
      .catch(() => setBusy(null))
  }

  const pickStandalone = async () => {
    setBusy('standalone')
    await fetch(`/api/network/standalone?always=${alwaysStandalone}`, {
      method: 'POST',
    }).catch(() => setBusy(null))
    setShowStandalone(true)
    // Busy stays set on success: netwatch moves to 'standalone' on its next
    // poll and App stops rendering this screen, so there is nothing to clear
    // it for. Clearing it here just makes the button look idle for the few
    // seconds before anything visibly happens.
  }

  const syncTime = async () => {
    await fetch('/api/time/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        epoch_ms: Date.now(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }),
    }).catch(() => {})
    setSynced(true)
  }

  return (
    <div className="min-h-screen flex flex-col items-center gap-5 px-4 py-10">
      <div className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
        <h1 className="font-medium">No Wi-Fi connection</h1>
        <p className="mt-1 text-sm text-zinc-400">
          Skylapse couldn't find a known network. Capture is still running.
        </p>

        <ul className="mt-4 divide-y divide-zinc-800 rounded-lg border border-zinc-800 text-sm">
          {(status.network?.known_networks ?? []).map((ssid) => (
            <li key={ssid} className="flex justify-between px-3 py-2">
              <span>{ssid}</span>
              <span className="text-zinc-500">
                {authFailures[ssid] ? 'wrong password?' : 'out of range'}
              </span>
            </li>
          ))}
        </ul>

        <div className="mt-4 flex flex-col gap-2">
          <button onClick={tryAgain} disabled={busy}
            className="rounded-lg border border-zinc-700 py-2.5 text-sm hover:bg-zinc-800">
            {busy === 'retry' ? 'Trying (up to 90s)…' : 'Try again'}
          </button>
          <button disabled title="Not built yet — see the setup wizard in DESIGN.md"
            className="rounded-lg border border-zinc-800 py-2.5 text-sm
                       text-zinc-600 cursor-not-allowed">
            Connect to a new network (not built yet)
          </button>
          <button onClick={pickStandalone} disabled={busy}
            className="rounded-lg border-2 border-sky-600 py-2.5 text-sm hover:bg-zinc-800">
            {busy === 'standalone' ? 'Switching…' : 'Use in access point mode'}
          </button>
          <label className="mt-1 flex items-center gap-2 text-sm text-zinc-400">
            <input type="checkbox" checked={alwaysStandalone}
              onChange={(e) => setAlwaysStandalone(e.target.checked)} />
            Always use access point mode
          </label>
        </div>
      </div>

      {showStandalone && drift > 5 && (
        <div className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
          <h2 className="font-medium">Access point mode</h2>
          <p className="mt-1 text-sm text-zinc-400">
            Without internet, the camera can't set its own clock. Timestamps and
            day/night switching depend on it.
          </p>
          <dl className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between rounded-lg bg-zinc-800/60 px-3 py-2">
              <dt className="text-zinc-400">Camera time</dt>
              <dd className="text-red-400">{cameraTime?.toLocaleString()}</dd>
            </div>
            <div className="flex justify-between rounded-lg bg-zinc-800/60 px-3 py-2">
              <dt className="text-zinc-400">Your phone</dt>
              <dd>{new Date().toLocaleString()}</dd>
            </div>
          </dl>
          <button onClick={syncTime} disabled={synced}
            className={`mt-4 w-full rounded-lg py-2.5 text-sm ${
              synced ? 'bg-emerald-900 text-emerald-300'
                     : 'bg-zinc-100 text-zinc-900 hover:bg-white'}`}>
            {synced ? 'Time synced' : 'Sync to phone time'}
          </button>
        </div>
      )}
    </div>
  )
}
