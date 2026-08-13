import { useEffect, useState } from 'react'

// Notifications: master switch gates everything; per-event toggles beneath it.
// Remote access: drives the Tailscale QR flow specced in DESIGN.md.

const EVENT_LABELS = {
  aurora: 'Aurora possible tonight',
  storage_low: 'Storage running low',
  camera_offline: 'Camera offline',
  timelapse_ready: 'Timelapse ready',
}

export default function SettingsScreen() {
  const [cfg, setCfg] = useState(null)
  const [topic, setTopic] = useState(null)
  const [remote, setRemote] = useState(null)
  const [testResult, setTestResult] = useState(null)

  useEffect(() => {
    fetch('/api/config').then((r) => r.json()).then(setCfg).catch(() => {})
    pollRemote()
  }, [])

  const pollRemote = () =>
    fetch('/api/remote/status').then((r) => r.json()).then(setRemote).catch(() => {})

  const saveNotifications = async (notifications) => {
    setCfg({ ...cfg, notifications })
    await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notifications }),
    }).catch(() => {})
  }

  const setupNtfy = async () => {
    const r = await fetch('/api/notify/generate-topic', { method: 'POST' })
    setTopic(await r.json())
  }

  const sendTest = async () => {
    const r = await fetch('/api/notify/test', { method: 'POST' })
    const { sent } = await r.json()
    setTestResult(sent ? 'Sent — check your phone' : 'Not sent (check switch + topic)')
  }

  const enableRemote = async () => {
    await fetch('/api/remote/enable', { method: 'POST' })
    const id = setInterval(async () => {
      const st = await (await fetch('/api/remote/status')).json()
      setRemote(st)
      if (st.connected) clearInterval(id)
    }, 3000)
  }

  if (!cfg) return null
  const n = cfg.notifications

  return (
    <div className="mx-auto flex max-w-md flex-col gap-5 px-4 py-6">
      {/* Notifications */}
      <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Notifications</h2>
          <button
            role="switch" aria-checked={n.enabled}
            onClick={() => saveNotifications({ ...n, enabled: !n.enabled })}
            className={`h-6 w-11 rounded-full transition ${
              n.enabled ? 'bg-sky-500' : 'bg-zinc-700'}`}>
            <span className={`block h-5 w-5 rounded-full bg-white transition ${
              n.enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </div>
        <p className="mt-1 text-sm text-zinc-400">
          Alerts on your phone via the free ntfy app. Off by default.
        </p>

        {n.enabled && (
          <div className="mt-4 space-y-3">
            {!n.ntfy_topic && !topic ? (
              <button onClick={setupNtfy}
                className="w-full rounded-lg border border-zinc-700 py-2.5 text-sm hover:bg-zinc-800">
                Set up phone alerts
              </button>
            ) : (
              <div className="rounded-lg bg-zinc-800/60 p-3 text-sm">
                <p className="text-zinc-400">
                  In the ntfy app, subscribe to topic:
                </p>
                <code className="text-sky-400">
                  {topic?.topic ?? n.ntfy_topic}
                </code>
              </div>
            )}

            <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
              {Object.entries(EVENT_LABELS).map(([key, label]) => (
                <li key={key} className="flex items-center justify-between px-3 py-2.5 text-sm">
                  <span>{label}</span>
                  <input type="checkbox" checked={n.events?.[key] ?? false}
                    onChange={(e) => saveNotifications({
                      ...n, events: { ...n.events, [key]: e.target.checked },
                    })} />
                </li>
              ))}
            </ul>

            <button onClick={sendTest}
              className="w-full rounded-lg border border-zinc-700 py-2.5 text-sm hover:bg-zinc-800">
              Send test notification
            </button>
            {testResult && <p className="text-sm text-zinc-400">{testResult}</p>}
          </div>
        )}
      </section>

      {/* Remote access */}
      <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
        <h2 className="font-medium">Remote access</h2>
        <p className="mt-1 text-sm text-zinc-400">
          View your camera from anywhere with your own free Tailscale account.
          Your camera is never exposed to the open internet.
        </p>

        {!remote?.installed ? (
          <p className="mt-3 text-sm text-zinc-500">
            Tailscale isn't installed on this device.
          </p>
        ) : remote.connected ? (
          <div className="mt-3 rounded-lg bg-emerald-950 p-3 text-sm">
            <p className="text-emerald-300">Remote access enabled</p>
            <a href={remote.url} className="text-sky-400 underline">{remote.url}</a>
          </div>
        ) : remote.auth_url ? (
          <div className="mt-3 text-sm">
            <p className="text-zinc-400">
              1. Install the free Tailscale app on your phone and sign in.<br />
              2. Scan this code to approve the camera:
            </p>
            <div className="mt-3 flex justify-center rounded-lg bg-zinc-800 p-4"
              dangerouslySetInnerHTML={{ __html: remote.qr_svg }} />
          </div>
        ) : (
          <button onClick={enableRemote}
            className="mt-3 w-full rounded-lg border border-zinc-700 py-2.5 text-sm hover:bg-zinc-800">
            Enable remote access
          </button>
        )}
      </section>
    </div>
  )
}
