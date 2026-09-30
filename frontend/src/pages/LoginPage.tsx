import { useState } from 'react'

import { useAuth } from '@/hooks/useAuth'
import { ApiError } from '@/services/api'

export function LoginPage() {
  const { signIn, publicSettings, requireLogin } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (!requireLogin) {
    // Documented kiosk mode: the API accepts the caller, so the UI skips the form.
    return null
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(username.trim(), password)
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : 'Could not sign in. Check that the service is running.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="brand">
          <img src="/favicon.svg" alt="" width={44} height={44} />
          <div className="brand-text" style={{ textAlign: 'left' }}>
            <strong>{publicSettings?.application_name ?? 'Student ID Barcode Detection System'}</strong>
            <span>Operator sign-in</span>
          </div>
        </div>

        <div className="field">
          <label htmlFor="username">Username</label>
          <input
            id="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </div>

        {error ? (
          <div className="banner danger" role="alert">
            {error}
          </div>
        ) : null}

        <button type="submit" className="btn primary block" disabled={busy || !username || !password}>
          {busy ? <span className="spinner" /> : null}
          Sign in
        </button>

        <p className="login-hint">
          Demo accounts (database/seed.sql): <span className="mono">admin</span>,{' '}
          <span className="mono">cashier</span>, <span className="mono">staff</span> with password{' '}
          <span className="mono">DemoPass!2026</span>.
          <br />
          Change the administrator password before any real use.
        </p>
      </form>
    </div>
  )
}
