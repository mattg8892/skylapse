// Shared shell pieces. Extracted once the dashboard grew past a single card,
// so every panel keeps the same dark aesthetic: rounded-2xl zinc panel, sky
// accent, muted zinc-400 body copy.
import { useCallback, useState } from 'react'

export function Card({ title, right, children }) {
  return (
    <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      {(title || right) && (
        <div className="flex items-center justify-between gap-3">
          {title ? <h2 className="font-medium">{title}</h2> : <span />}
          {right}
        </div>
      )}
      {children}
    </section>
  )
}

export function Toggle({ checked, onChange, label }) {
  return (
    <button
      role="switch" aria-checked={checked} aria-label={label}
      onClick={() => onChange(!checked)}
      className={`h-6 w-11 shrink-0 rounded-full transition ${
        checked ? 'bg-sky-500' : 'bg-zinc-700'}`}>
      <span className={`block h-5 w-5 rounded-full bg-white transition ${
        checked ? 'translate-x-5' : 'translate-x-0.5'}`} />
    </button>
  )
}

const TONES = {
  default: 'border-zinc-700 hover:bg-zinc-800',
  accent: 'border-sky-600 bg-sky-600/15 text-sky-300 hover:bg-sky-600/25',
  warn: 'border-amber-600 bg-amber-600/15 text-amber-300 hover:bg-amber-600/25',
}

export function Button({ children, onClick, disabled, tone = 'default', className = '' }) {
  return (
    <button
      onClick={onClick} disabled={disabled}
      className={`rounded-lg border px-3 py-2.5 text-sm transition
        disabled:cursor-not-allowed disabled:opacity-40 ${TONES[tone]} ${className}`}>
      {children}
    </button>
  )
}

/** Segmented control — used for Auto/Manual exposure and Night/Day profile. */
export function Segmented({ value, onChange, options }) {
  return (
    <div className="flex rounded-lg border border-zinc-700 p-0.5 text-sm">
      {options.map((o) => (
        <button
          key={o.value} onClick={() => onChange(o.value)}
          className={`flex-1 rounded-md px-3 py-1.5 transition ${
            value === o.value ? 'bg-zinc-700 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function NumberField({ label, value, onChange, min, max, step = 1, suffix }) {
  return (
    <label className="flex items-center justify-between gap-3 text-sm">
      <span className="text-zinc-400">{label}</span>
      <span className="flex items-center gap-2">
        <input
          type="number" value={value} min={min} max={max} step={step}
          onChange={(e) => e.target.value !== '' && onChange(Number(e.target.value))}
          className="w-24 rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-right
                     tabular-nums outline-none focus:border-sky-600" />
        {suffix && <span className="w-6 text-zinc-500">{suffix}</span>}
      </span>
    </label>
  )
}

export function Select({ label, value, onChange, options }) {
  return (
    <label className="flex items-center justify-between gap-3 text-sm">
      <span className="text-zinc-400">{label}</span>
      <select
        value={value} onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 outline-none
                   focus:border-sky-600">
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  )
}

export function Slider({ label, value, min, max, step, display, onChange }) {
  return (
    <label className="block text-sm">
      <span className="flex justify-between">
        <span className="text-zinc-400">{label}</span>
        <span className="tabular-nums text-zinc-300">{display}</span>
      </span>
      {/* h-6 gives the thumb a bigger hit area than the default track */}
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 h-6 w-full accent-sky-500" />
    </label>
  )
}

/** Transient confirmation for fire-and-forget actions (keeper, render, resume). */
export function useToast() {
  const [message, setMessage] = useState(null)
  const show = useCallback((text) => {
    setMessage(text)
    setTimeout(() => setMessage((m) => (m === text ? null : m)), 3500)
  }, [])
  return [message, show]
}

export function Toast({ message }) {
  if (!message) return null
  return (
    <div role="status" aria-live="polite"
      className="pointer-events-none fixed inset-x-0 bottom-6 z-50 flex justify-center px-4">
      <div className="rounded-full border border-zinc-700 bg-zinc-800/95 px-4 py-2 text-sm shadow-lg">
        {message}
      </div>
    </div>
  )
}

/**
 * Blocking confirmation for actions that cost the user their own connection.
 *
 * Switching to the access point disconnects whoever is looking at this page,
 * which is not something a toast can take back. `consequence` is stated in the
 * dialog rather than trusted to the button label, because the person who most
 * needs to read it is the one tapping quickly on a phone.
 */
export function ConfirmDialog({ open, title, consequence, confirmLabel,
                               tone = 'warn', onConfirm, onCancel }) {
  if (!open) return null
  return (
    <div role="dialog" aria-modal="true" aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-sm rounded-2xl border border-zinc-700 bg-zinc-900 p-5">
        <h3 className="font-medium">{title}</h3>
        <p className="mt-2 text-sm text-zinc-400">{consequence}</p>
        <div className="mt-5 flex gap-2">
          <Button onClick={onCancel} className="flex-1">Cancel</Button>
          <Button onClick={onConfirm} tone={tone} className="flex-1">
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  )
}
