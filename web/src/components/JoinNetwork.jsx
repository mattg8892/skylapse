// Pick a Wi-Fi network and hand it credentials.
//
// Shared by the setup wizard and the fallback "no connection" screen, because
// they are the same job in two contexts. The difference between them is the
// stakes: from the fallback screen you are reading this over the camera's own
// access point, and a successful join takes that network away for up to ninety
// seconds. So `warnAboutDisconnect` makes the screen say so before doing it,
// rather than leaving someone staring at a dead page wondering what they broke.
import { useEffect, useState } from 'react'
import { Button, Select } from './ui.jsx'

export default function JoinNetwork({ warnAboutDisconnect = false, onJoined }) {
  const [networks, setNetworks] = useState(null)
  const [ssid, setSsid] = useState('')
  const [password, setPassword] = useState('')
  const [state, setState] = useState('')          // '', 'joining', or an error

  const scan = async () => {
    setNetworks(null)
    const r = await fetch('/api/network/scan').catch(() => null)
    setNetworks(r?.ok ? await r.json() : [])
  }

  useEffect(() => { scan() }, [])

  const chosen = networks?.find((n) => n.ssid === ssid)

  const join = async () => {
    setState('joining')
    const r = await fetch('/api/network/join', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid, password }),
    }).catch(() => null)
    if (!r?.ok) {
      // netwatch owns the radio and may not be running — it ships disabled on
      // installs that have not verified it. Say which, rather than leaving a
      // button that appears to do nothing.
      return setState('The network service isn’t responding, so the camera '
                      + 'couldn’t be given those details. You can set Wi-Fi up '
                      + 'later in Settings.')
    }
    onJoined?.(ssid)
    // Deliberately stays in 'joining'. The camera is about to change networks;
    // if this page is being served over the one that is going away, the honest
    // final state is "we asked, now go and look", not a success tick this page
    // will never live long enough to earn.
  }

  if (state === 'joining') {
    return (
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-4 text-sm">
        <p className="text-sky-300">Connecting to {ssid}…</p>
        <p className="mt-2 text-zinc-400">
          This takes up to 90 seconds.{' '}
          {warnAboutDisconnect
            ? 'If this page stops responding, that is the camera moving to your '
              + 'network — find it at its new address. If it does not work, the '
              + 'camera comes back on its own network and you can try again.'
            : 'The page will catch up on its own.'}
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {networks === null && <p className="text-sm text-zinc-500">Scanning…</p>}
      {networks?.length === 0 && (
        <p className="text-sm text-zinc-500">
          No networks found.{' '}
          <button onClick={scan} className="text-sky-400 underline">Scan again</button>
        </p>
      )}

      {networks?.length > 0 && (
        <Select label="Network" value={ssid} onChange={setSsid}
          options={[{ value: '', label: 'Choose a network…' },
                    ...networks.map((n) => ({
                      value: n.ssid,
                      label: `${n.ssid}${n.secured ? '' : '  (open)'}`,
                    }))]} />
      )}

      {ssid && chosen?.secured !== false && (
        <input type="password" value={password} placeholder="Wi-Fi password"
          autoComplete="off" onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded-lg border border-zinc-700 bg-zinc-900
                     px-3 py-2.5 text-sm" />
      )}

      {ssid && warnAboutDisconnect && (
        <p className="rounded-lg bg-amber-950 p-3 text-sm text-amber-300">
          You are reading this over the camera’s own network. Joining {ssid}
          {' '}takes that away for up to 90 seconds, and afterwards the camera
          will be at a new address on your network.
        </p>
      )}

      {ssid && (
        <Button tone="accent" className="w-full" onClick={join}>
          Join {ssid}
        </Button>
      )}

      {state && state !== 'joining' && (
        <p className="rounded-lg bg-red-950 p-3 text-sm text-red-300">{state}</p>
      )}
    </div>
  )
}
