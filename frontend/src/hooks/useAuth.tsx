/**
 * Authentication context.
 *
 * The session token is an HttpOnly cookie, so React only needs to know *who*
 * is signed in and *what they may do*.  `permissions` comes from the login
 * response and drives the navigation, so a cashier does not see buttons that
 * would return 403.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { ApiError, auth, settings } from '@/services/api'
import type { PublicSettings, Role, User } from '@/types'

interface AuthContextValue {
  user: User | null
  permissions: Set<string>
  loading: boolean
  requireLogin: boolean
  publicSettings: PublicSettings | null
  theme: 'dark' | 'light'
  signIn: (username: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  refresh: () => Promise<void>
  can: (...permissions: string[]) => boolean
  hasRole: (...roles: Role[]) => boolean
  setTheme: (theme: 'dark' | 'light') => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

const PERMISSIONS_BY_ROLE: Record<Role, string[]> = {
  admin: ['*'],
  cashier: [
    'scan',
    'verify',
    'transaction:create',
    'transaction:cancel',
    'students:read',
    'scans:read',
    'transactions:read',
    'reports:read',
    'reports:export',
    'settings:read',
  ],
  staff: ['scan', 'verify', 'students:read', 'scans:read', 'reports:read', 'settings:read'],
  operator: ['scan', 'verify', 'transaction:create', 'students:read', 'scans:read', 'reports:read'],
  viewer: ['students:read', 'scans:read', 'transactions:read', 'reports:read', 'settings:read'],
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [permissions, setPermissions] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [publicSettings, setPublicSettings] = useState<PublicSettings | null>(null)
  const [theme, setThemeState] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('sidb-theme') as 'dark' | 'light') ?? 'dark',
  )

  const setTheme = useCallback((next: 'dark' | 'light') => {
    setThemeState(next)
    localStorage.setItem('sidb-theme', next)
    document.documentElement.dataset.theme = next
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  // Load the public settings (application name, theme, whether login is
  // required) before anything else, so the login screen is not blank.
  useEffect(() => {
    let cancelled = false
    settings
      .public()
      .then((value) => {
        if (cancelled) return
        setPublicSettings(value)
        if (value.theme === 'light' || value.theme === 'dark') {
          setTheme(value.theme)
        }
        // A kiosk can run with `require_login: false`; in that mode the API
        // accepts the caller, so the UI skips the login screen entirely.
        if (!value.require_login) {
          setLoading(false)
        }
      })
      .catch(() => undefined)
      .finally(() => {
        void refreshUser(cancelled)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refreshUser = useCallback(async (cancelled = false) => {
    try {
      const current = await auth.me()
      if (cancelled) return
      setUser(current)
      setPermissions(new Set(PERMISSIONS_BY_ROLE[current.role] ?? []))
    } catch (error) {
      if (cancelled) return
      if (error instanceof ApiError && error.status === 0) {
        // The service is down; stay signed out and let the shell show the
        // "service unavailable" banner.
      }
      setUser(null)
      setPermissions(new Set())
    } finally {
      if (!cancelled) setLoading(false)
    }
  }, [])

  const signIn = useCallback(
    async (username: string, password: string) => {
      const response = await auth.login(username, password)
      setUser(response.user)
      setPermissions(new Set(response.permissions))
    },
    [],
  )

  const signOut = useCallback(async () => {
    try {
      await auth.logout()
    } finally {
      setUser(null)
      setPermissions(new Set())
    }
  }, [])

  const can = useCallback(
    (...required: string[]) => {
      if (!user) return false
      if (permissions.has('*')) return true
      return required.every((permission) => permissions.has(permission))
    },
    [permissions, user],
  )

  const hasRole = useCallback(
    (...roles: Role[]) => (user ? roles.includes(user.role) : false),
    [user],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      permissions,
      loading,
      requireLogin: publicSettings?.require_login ?? true,
      publicSettings,
      theme,
      signIn,
      signOut,
      refresh: () => refreshUser(),
      can,
      hasRole,
      setTheme,
    }),
    [user, permissions, loading, publicSettings, theme, signIn, signOut, refreshUser, can, hasRole, setTheme],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
