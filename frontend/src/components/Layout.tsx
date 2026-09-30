/** Application shell: sidebar navigation, top bar, theme switch, sign out. */

import { NavLink, useLocation } from 'react-router-dom'
import { useState, type ReactNode } from 'react'

import { useAuth } from '@/hooks/useAuth'
import { errorMessage } from '@/utils/ui'
import type { ScannerState } from '@/types'

interface NavItem {
  to: string
  label: string
  icon: string
  permission?: string
}

const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: '▦' },
  { to: '/scanner', label: 'Scanner', icon: '⛶', permission: 'scan' },
  { to: '/students', label: 'Students', icon: '👤', permission: 'students:read' },
  { to: '/history', label: 'Scan History', icon: '🕘', permission: 'scans:read' },
  { to: '/transactions', label: 'Transactions', icon: '🧾', permission: 'transactions:read' },
  { to: '/reports', label: 'Reports', icon: '📊', permission: 'reports:read' },
  { to: '/settings', label: 'Settings', icon: '⚙', permission: 'settings:read' },
]

export function Layout({
  title,
  subtitle,
  actions,
  children,
  scannerState,
  applicationName,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
  scannerState?: ScannerState | null
  applicationName?: string
}) {
  const { user, signOut, can, theme, setTheme } = useAuth()
  const location = useLocation()
  const [signingOut, setSigningOut] = useState(false)

  const visible = NAV.filter((item) => !item.permission || can(item.permission))
  const scannerHealthy = scannerState?.camera_connected ?? false
  const statusTone = scannerState?.error_code
    ? 'danger'
    : scannerHealthy
      ? 'success'
      : 'neutral'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          <div className="brand-text">
            <strong>{applicationName ?? 'Student ID Scanner'}</strong>
            <span>Barcode System</span>
          </div>
        </div>

        <nav className="nav" aria-label="Main navigation">
          {visible.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => (isActive ? 'active' : '')}
            >
              <span className="icon" aria-hidden="true">
                {item.icon}
              </span>
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className={`chip ${statusTone}`}>
            <span className="dot" />
            {scannerState
              ? scannerState.state
              : scannerHealthy
                ? 'ready'
                : 'camera off'}
          </div>
          {user ? (
            <>
              <div>
                Signed in as <strong>{user.username}</strong>
                <br />
                <span className="dim">
                  {user.role}
                  {user.must_change_password ? ' · change your password' : ''}
                </span>
              </div>
              <button
                type="button"
                className="btn small block"
                onClick={async () => {
                  setSigningOut(true)
                  await signOut()
                  setSigningOut(false)
                }}
                disabled={signingOut}
              >
                {signingOut ? 'Signing out…' : 'Sign out'}
              </button>
            </>
          ) : null}
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="titles">
            <h1>{title}</h1>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <div className="topbar-actions">
            {actions}
            {scannerState?.error_code ? (
              <span className="chip danger" title={errorMessage(scannerState.error_code)}>
                <span className="dot" />
                {scannerState.error_code}
              </span>
            ) : null}
            <button
              type="button"
              className="btn small"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            >
              {theme === 'dark' ? '🌙 Dark' : '☀ Light'}
            </button>
          </div>
        </header>

        <main className="content" key={location.pathname}>
          {children}
        </main>
      </div>
    </div>
  )
}
