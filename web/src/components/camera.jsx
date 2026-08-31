/**
 * Choosing, testing and adding a camera.
 *
 * Shared by the first-run wizard and the settings screen on purpose. Setup had
 * a real flow — see what is detected, take a test shot, declare a sensor the Pi
 * cannot see, wait out the reboot — and settings had none of it: plug something
 * in and hope the daemon notices. Anything you can do to a camera at first run
 * you can do again later, from the same code, or the second camera is a
 * second-class citizen forever.
 *
 * Raspberry Pi camera modules are the primary target and the HQ/IMX477 is the
 * one this is developed against. ZWO is offered second and honestly: its SDK is
 * a vendor binary fetched on demand, verified against one model, and it may
 * simply not work with yours.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Select } from './ui.jsx'
import { errorText } from '../lib/errors'

/** Poll /api/setup/camera, and expose a refresh for after anything changes. */
export function useCameras() {
  const [info, setInfo] = useState(null)

  const refresh = useCallback(async () => {
    const r = await fetch('/api/setup/camera', { cache: 'no-store' })
      .catch(() => null)
    if (r?.ok) setInfo(await r.json())
  }, [])

  useEffect(() => { refresh() }, [refresh])
  return [info, refresh]
}

/**
 * The whole camera surface: what is attached, a real frame from it, and the
 * two ways to add one the Pi cannot find by itself.
 *
 * `selected`/`onSelect` are the caller's, because the wizard is editing a draft
 * and settings is writing config — the only difference between the two uses.
 */
export function CameraPanel({ selected, onSelect, info, refresh }) {
  const [shotAt, setShotAt] = useState(null)
  const [waiting, setWaiting] = useState(false)
  const polls = useRef(0)

  const cameras = info?.cameras ?? []
  const found = cameras.length > 0 || info?.detected
  const active = selected || info?.active || cameras[0]?.camera_id || ''

  const takeShot = async () => {
    setWaiting(true)
    polls.current = 0
    await fetch('/api/setup/camera/test', { method: 'POST' }).catch(() => {})
    // The daemon picks the request up between frames, so this can take a gap.
    const timer = setInterval(async () => {
      polls.current += 1
      const r = await fetch('/api/setup/camera').catch(() => null)
      const next = r?.ok ? await r.json() : null
      if (next?.shot_at && next.shot_at !== info?.shot_at) {
        setShotAt(next.shot_at)
        setWaiting(false)
        clearInterval(timer)
      } else if (polls.current > 40) {         // ~2 minutes
        setWaiting(false)
        clearInterval(timer)
      }
    }, 3000)
  }

  return (
    <div className="space-y-4">
      {found ? (
        <>
          {cameras.length > 1 ? (
            <Select label="Which camera is pointed at the sky?" value={active}
              onChange={onSelect}
              options={cameras.map((c) => ({ value: c.camera_id, label: c.label }))} />
          ) : (
            <p className="rounded-xl border border-zinc-800 bg-zinc-900 p-4
                          text-emerald-300">
              {cameras[0]?.label || info.detected}
            </p>
          )}

          {shotAt && (
            <figure>
              <img src={`/api/setup/camera/shot?t=${shotAt}`} alt="Test shot"
                className="w-full rounded-lg bg-zinc-800" />
              <figcaption className="mt-1 text-xs text-zinc-500">
                A real frame, taken just now. It isn’t saved to your night.
              </figcaption>
            </figure>
          )}

          <Button onClick={takeShot} disabled={waiting} className="w-full">
            {waiting ? 'Taking a shot…' : shotAt ? 'Retake' : 'Take a test shot'}
          </Button>
        </>
      ) : (
        <div className="space-y-3 text-sm text-zinc-400">
          <p>No camera detected. Worth checking, in this order:</p>
          <ul className="list-disc space-y-2 pl-5">
            <li><b>Pull the power for 15 seconds.</b> Not a reboot — a full cold
              start, plug out. A warm reboot does not drain the sensor’s
              regulator and some modules only come up after one. This is first
              because it is the cheapest thing to try and, on this project’s own
              hardware, the thing that turned out to be wrong twice.</li>
            <li><b>Ribbon cable:</b> contacts the right way round at
              <i> both</i> ends. It is easy to get right at one and wrong at the
              other.</li>
            <li><b>USB camera:</b> seated firmly, and on the official power
              supply. Underpowering a USB3 camera looks like a missing camera.</li>
          </ul>
          <Button onClick={refresh} className="w-full">Scan again</Button>
        </div>
      )}

      <AddCamera found={!!found} onChanged={refresh} />
    </div>
  )
}

/**
 * The two reasons a camera does not show up by itself, in the order they
 * happen: a Pi module with no EEPROM, then a ZWO with no SDK.
 *
 * Collapsed by default and offered whether or not a camera was found — adding a
 * second camera is the same job as rescuing the first, and settings previously
 * had no way to do either.
 */
function AddCamera({ found, onChanged }) {
  const [open, setOpen] = useState(!found)

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        className="w-full text-sm text-sky-400 underline">
        {found ? 'Add another camera' : 'My camera isn’t being detected'}
      </button>
    )
  }

  return (
    <div className="space-y-4">
      <DeclareSensor onChanged={onChanged} />
      <ZwoSupport onChanged={onChanged} />
    </div>
  )
}

/**
 * Declare a sensor by hand when auto-detection cannot see it.
 *
 * Raspberry Pi OS detects cameras by reading an EEPROM, and third-party boards
 * — the very common HQ/IMX477 clones among them — do not carry one. On a stock
 * image such a camera is simply invisible, and the only fix was editing
 * /boot/firmware/config.txt over SSH: the exact thing an appliance image exists
 * to avoid. Measured on this project's own hardware, which is how it was found.
 */
export function DeclareSensor({ onChanged }) {
  const [sensors, setSensors] = useState([])
  const [sensor, setSensor] = useState('imx477')
  const [state, setState] = useState('')

  useEffect(() => {
    fetch('/api/setup/camera/sensors').then((r) => r.json())
      .then((b) => setSensors(b.sensors ?? [])).catch(() => {})
  }, [])

  const enable = async () => {
    setState('working')
    const r = await fetch('/api/setup/camera/overlay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sensor }),
    }).catch(() => null)
    setState(r?.ok ? 'rebooting' : 'failed')
  }

  // onChanged is what makes the test-shot button appear. Without it this
  // screen announced "Camera found — take a test shot" while the button to do
  // that sat behind a stale camera list, so it only turned up if you reloaded
  // the page by hand. Reported from a real setup, and the instruction was
  // right there on screen telling you to do something you could not do.
  if (state === 'rebooting') {
    return <WaitingForReboot sensor={sensor} onFound={onChanged} />
  }

  return (
    <div className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900 p-4">
      <p className="text-sm text-zinc-300">Raspberry Pi camera module</p>
      <p className="text-sm text-zinc-400">
        Some cameras — most third-party HQ/IMX477 boards among them — can’t be
        detected automatically, because that relies on a chip they don’t carry.
        Pick yours and Skylapse will tell the Pi about it directly.
      </p>
      <Select label="Sensor" value={sensor} onChange={setSensor}
        options={sensors.length ? sensors
          : [{ value: 'imx477', label: 'HQ Camera / IMX477' }]} />
      <p className="rounded-lg bg-zinc-800/60 p-3 text-xs text-zinc-400">
        The camera restarts to load the driver, so your phone will drop off its
        Wi-Fi for a minute. Don’t close this page — it picks up on its own.
        <b className="mt-1 block text-zinc-300">
          Have the power lead handy.
        </b>
        On most boards the restart is not enough on its own and you will need to
        pull the power once — the next screen says when.
      </p>
      <Button tone="accent" className="w-full" disabled={state === 'working'}
        onClick={enable}>
        {state === 'working' ? 'Saving…' : 'Enable and restart'}
      </Button>
      {state === 'failed' && (
        <p className="text-sm text-red-400">
          Couldn’t write the boot configuration.
        </p>
      )}
      <p className="text-xs text-zinc-500">
        Harmless to get wrong — pick another and try again. The original
        configuration is kept as config.txt.skylapse-backup.
      </p>
    </div>
  )
}

/**
 * Install ZWO's SDK on demand.
 *
 * Their licence forbids redistributing it and their download portal is
 * browser-only, so it cannot ship in the image — which until now made a ZWO rig
 * the one setup that still needed SSH, in a product whose whole point is not
 * needing one. Fetching it here on request, with their terms accepted, closes
 * that without redistributing anything.
 *
 * The warning above the button is not boilerplate. Skylapse is developed
 * against Pi camera modules; the ZWO path is verified on a single ASI676MC and
 * is genuinely likely to fail on other models. Someone about to buy a camera
 * for this should read that before they spend the money, not after.
 */
export function ZwoSupport({ onChanged }) {
  const [zwo, setZwo] = useState(null)
  const [accepted, setAccepted] = useState(false)
  const [state, setState] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    fetch('/api/setup/zwo').then((r) => r.json()).then(setZwo).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const install = async () => {
    setState('working')
    setError('')
    const r = await fetch('/api/setup/zwo/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accept_terms: true }),
    }).catch(() => null)
    if (r?.ok) {
      setState('done')
      load()
      onChanged?.()
    } else {
      const detail = await r?.json().catch(() => null)
      setError(errorText(detail, 'Couldn’t install it. Check the camera’s '
        + 'internet connection and try again.'))
      setState('')
    }
  }

  if (!zwo) return null

  return (
    <div className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900 p-4">
      <p className="text-sm text-zinc-300">ZWO ASI camera (USB)</p>

      <p className="rounded-lg border border-amber-700/60 bg-amber-950/40 p-3
                    text-xs text-amber-200/90">
        <b className="text-amber-300">Best effort, and not guaranteed.</b>{' '}
        Skylapse is built and tested for Raspberry Pi camera modules — the HQ
        camera above all. ZWO support is second: it needs a vendor library that
        can’t be shipped with Skylapse, it has only been verified on one model
        (ASI676MC), and it may not work with your camera at all. If you are
        choosing a camera for this, choose a Pi one.
      </p>

      {!zwo.supported ? (
        <p className="text-sm text-zinc-400">
          Not available on this system ({zwo.machine}) — it needs 64-bit
          Raspberry Pi OS.
        </p>
      ) : zwo.installed ? (
        <>
          <p className="text-sm text-emerald-300">
            ZWO support installed (SDK {zwo.version}).
          </p>
          <p className="text-xs text-zinc-500">
            Plug the camera in and use “Scan again” above. If it still isn’t
            found, a full power cycle is the usual fix — a USB3 camera on an
            underpowered supply looks exactly like a missing one.
          </p>
        </>
      ) : state === 'done' ? (
        <p className="text-sm text-emerald-300">
          Installed. Plug the camera in and scan again.
        </p>
      ) : (
        <>
          <p className="text-sm text-zinc-400">
            Skylapse can download ZWO’s camera library for you — about 4 MB, so
            the camera needs to be on a network with internet access. No
            terminal, no reboot.
          </p>
          <label className="flex items-start gap-3 text-sm text-zinc-400">
            <input type="checkbox" className="mt-1" checked={accepted}
              onChange={(e) => setAccepted(e.target.checked)} />
            <span>
              I accept{' '}
              <a href={zwo.license_url} target="_blank" rel="noreferrer"
                className="text-sky-400 underline">ZWO’s licence</a>{' '}
              for their SDK.
            </span>
          </label>
          <Button tone="accent" className="w-full"
            disabled={!accepted || state === 'working'} onClick={install}>
            {state === 'working' ? 'Downloading…' : 'Install ZWO support'}
          </Button>
          {!zwo.bindings && (
            <p className="text-xs text-amber-400">
              The Python bindings are missing from this install too, so the
              driver won’t load even once the library is in place. Reinstall
              with the <code>zwo</code> extra.
            </p>
          )}
        </>
      )}

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  )
}

/**
 * The screen you stare at while the camera restarts.
 *
 * Reported from the rig: "you just kinda get thrown around on an offline copy
 * of the page and can't tell if it's working." Which is exactly right — the
 * reboot takes the access point down with it, so the page loses its network
 * mid-action and has nothing to say.
 *
 * The tab's JavaScript keeps running through all of that, though, as long as
 * nobody reloads. So this keeps quietly retrying, says plainly what is
 * happening and what to do about the dropped Wi-Fi, and moves on by itself the
 * moment the camera answers again. The one instruction that matters is "do not
 * reload" — a reload during the outage is what produces the blank cached page.
 */
export function WaitingForReboot({ sensor, onFound }) {
  const [seconds, setSeconds] = useState(0)
  const [found, setFound] = useState(null)

  useEffect(() => {
    const tick = setInterval(() => setSeconds((s) => s + 1), 1000)
    const poll = setInterval(async () => {
      // no-store, because the whole failure mode here is a cached answer from
      // before the reboot looking like a live one.
      const r = await fetch('/api/setup/camera', { cache: 'no-store' })
        .catch(() => null)
      if (!r?.ok) return
      const info = await r.json().catch(() => null)
      if (info?.detected || info?.cameras?.length) {
        setFound(info.cameras?.[0]?.label || info.detected)
        // Tell whoever owns the camera list, or the button this screen is
        // about to tell them to press will not be on the page.
        onFound?.()
        clearInterval(poll)
        clearInterval(tick)
      }
    }, 3000)
    return () => { clearInterval(poll); clearInterval(tick) }
  }, [onFound])

  if (found) {
    return (
      <div className="rounded-xl border border-emerald-800 bg-emerald-950 p-4">
        <p className="text-emerald-300">Camera found: {found}</p>
        <p className="mt-2 text-sm text-zinc-400">
          Take a test shot to make sure it is really working, not just detected.
          The button is just below.
        </p>
      </div>
    )
  }

  // Reported from the rig, reproducibly: declaring a sensor and restarting is
  // not enough on its own — the camera appears only after the power is pulled.
  // Which follows, since a warm reboot never drains the sensor's regulator, so
  // the module comes back up in exactly the dead state it went down in.
  //
  // So this is not filed under troubleshooting. It is the next step of the
  // procedure, shown once the restart has plainly not done it by itself, and
  // not before — because on boards where the reboot *is* enough, telling
  // someone to go and unplug their camera would be wrong.
  const coldStart = seconds > 45
  // Past about two minutes a Pi has either come back or is not going to, and
  // continuing to show a hopeful spinner would be its own kind of lie.
  const slow = seconds > 150
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex items-center gap-3">
        <span className="h-4 w-4 shrink-0 animate-spin rounded-full
                         border-2 border-sky-500 border-t-transparent" />
        <p className="text-sky-300">
          Restarting with {sensor} enabled… {seconds}s
        </p>
      </div>

      <ol className="mt-4 space-y-2 text-sm text-zinc-400">
        <li>
          <b className="text-zinc-300">Don’t reload this page.</b> It is still
          working, and reloading now is what shows you a blank one.
        </li>
        <li>
          Your phone will drop off the camera’s Wi-Fi while it restarts.
          <b className="text-zinc-300"> Rejoin it</b> when it comes back and
          this page carries on by itself.
        </li>
        <li>Usually about a minute. Nothing you answered is lost.</li>
      </ol>

      {coldStart && (
        <div className="mt-4 rounded-lg border border-sky-800 bg-sky-950 p-3">
          <p className="text-sm text-sky-200">
            <b className="text-sky-100">Now pull the power for 15 seconds</b>,
            then plug it back in.
          </p>
          <p className="mt-2 text-xs text-sky-200/80">
            This is normal and it is usually the step that works. Restarting
            applies the setting, but it does not cut power to the sensor, and
            most camera boards will not come up until it has actually been off.
            Unplug at the wall or at the Pi — either is fine.
          </p>
          <p className="mt-2 text-xs text-sky-200/80">
            Leave this page open while you do it. It keeps checking, and moves
            on by itself the moment the camera answers.
          </p>
        </div>
      )}

      {slow && (
        <p className="mt-4 rounded-lg bg-amber-950 p-3 text-sm text-amber-300">
          Still nothing. If you have already power-cycled it, check your phone is
          back on the camera’s network, then reload this page — nothing is lost
          either way, since your answers are kept on the camera rather than here.
          A camera that never appears after a cold start is usually the ribbon
          cable, or the wrong sensor picked, and both are safe to try again.
        </p>
      )}
    </div>
  )
}
