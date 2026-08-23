// The login screen. Deliberately plain: one field, one button, no branding
// games and no "forgot password" link that leads nowhere.
//
// The recovery path is physical — clear the entry over SSH — because that is
// the honest one for a device with no email and no account. Saying so here is
// better than letting someone hunt for a reset flow that does not exist.
import { useState } from 'react'
import { Button } from '../components/ui.jsx'

export default function LoginScreen({ onIn }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }).catch(() => null)
    setBusy(false)
    if (!r?.ok) return setError('That password didn’t work.')
    onIn?.()
  }

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm">
        <p className="text-sm uppercase tracking-widest text-sky-400">Skylapse</p>
        <h1 className="mt-1 text-2xl font-medium">This camera is protected.</h1>

        <input
          type="password" value={password} autoFocus
          autoComplete="current-password" placeholder="Password"
          onChange={(e) => setPassword(e.target.value)}
          className="mt-6 w-full rounded-lg border border-zinc-700 bg-zinc-900
                     px-3 py-3 text-base" />

        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}

        <Button tone="accent" className="mt-4 w-full" disabled={busy || !password}>
          {busy ? 'Checking…' : 'Unlock'}
        </Button>

        <p className="mt-6 text-sm text-zinc-500">
          Forgotten it? Clear the password entry in
          {' '}<code className="text-zinc-400">/etc/skylapse/config.yaml</code>{' '}
          over SSH and restart the service. It never needs a reflash.
        </p>
      </form>
    </div>
  )
}
