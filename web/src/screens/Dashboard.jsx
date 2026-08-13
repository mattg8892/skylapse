// Dashboard stub: latest frame + capture status + standalone badge.
// Full build-out (tonight strip, storage gauge) is issue #3.
import { useState } from 'react'
import SettingsScreen from './SettingsScreen.jsx'

export default function Dashboard({ status }) {
  const [showSettings, setShowSettings] = useState(false)
  const d = status.daemon ?? {}
  const standalone =
    status.network?.mode === 'standalone' || status.network?.session_standalone

  return (
    <div className="min-h-screen px-4 py-6 max-w-3xl mx-auto">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-medium">Skylapse</h1>
        <div className="flex items-center gap-3 text-sm">
          {standalone && (
            <span className="rounded-full bg-amber-950 px-3 py-1 text-amber-400">
              Network: Standalone
            </span>
          )}
          <span className="text-zinc-400">
            {new Date(status.server_time * 1000).toLocaleTimeString()}
          </span>
          <button onClick={() => setShowSettings(!showSettings)}
            className="rounded-lg border border-zinc-700 px-3 py-1 hover:bg-zinc-800">
            {showSettings ? 'Dashboard' : 'Settings'}
          </button>
        </div>
      </header>

      {showSettings && <SettingsScreen />}
      {showSettings ? null : (<></>)}

      <main className="mt-6">
        {d.latest ? (
          <img src={`/api/latest?t=${d.updated}`} alt="Latest sky frame"
            className="w-full rounded-2xl border border-zinc-800" />
        ) : (
          <div className="grid h-64 place-items-center rounded-2xl border border-dashed border-zinc-800 text-zinc-500">
            {d.state === 'no_camera' ? 'No camera detected' : 'Waiting for first frame…'}
          </div>
        )}
        {d.state === 'capturing' && (
          <p className="mt-3 text-sm text-zinc-400">
            {d.period} · {(d.exposure_us / 1e6).toFixed(1)}s · gain {d.gain} ·
            brightness {d.brightness}
          </p>
        )}
      </main>
    </div>
  )
}
