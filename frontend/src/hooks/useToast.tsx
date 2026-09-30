/** Toast notifications: one place for "saved", "failed", "queued offline". */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

export type ToastKind = 'success' | 'error' | 'warning' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  title: string
  body?: string
}

interface ToastContextValue {
  notify: (kind: ToastKind, title: string, body?: string) => void
  success: (title: string, body?: string) => void
  error: (title: string, body?: string) => void
  warning: (title: string, body?: string) => void
  info: (title: string, body?: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

let nextId = 1

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const notify = useCallback(
    (kind: ToastKind, title: string, body?: string) => {
      const toast: Toast = { id: nextId++, kind, title, body }
      setToasts((current) => [...current.slice(-4), toast])
      // Errors stay longer: they usually need reading, not glancing at.
      window.setTimeout(() => dismiss(toast.id), kind === 'error' ? 8000 : 4200)
    },
    [dismiss],
  )

  const value = useMemo<ToastContextValue>(
    () => ({
      notify,
      success: (title, body) => notify('success', title, body),
      error: (title, body) => notify('error', title, body),
      warning: (title, body) => notify('warning', title, body),
      info: (title, body) => notify('info', title, body),
    }),
    [notify],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast ${toast.kind}`} onClick={() => dismiss(toast.id)}>
            <div className="toast-title">{toast.title}</div>
            {toast.body ? <div className="toast-body">{toast.body}</div> : null}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside <ToastProvider>')
  return context
}
