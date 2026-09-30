/** Small, reusable presentational components. */

import { useEffect, useRef, type ReactNode } from 'react'

import type { Tone } from '@/utils/ui'

/* ------------------------------------------------------------------- chip */
export function Chip({
  tone = 'neutral',
  children,
  pulse = false,
  title,
}: {
  tone?: Tone
  children: ReactNode
  pulse?: boolean
  title?: string
}) {
  return (
    <span className={`chip ${tone}`} title={title}>
      <span className={`dot ${pulse ? 'pulse' : ''}`} />
      {children}
    </span>
  )
}

/* --------------------------------------------------------------- stat tile */
export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'success' | 'danger' | 'warning' | 'accent'
}) {
  return (
    <div className={`stat ${tone ?? ''}`}>
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {hint ? <span className="hint">{hint}</span> : null}
    </div>
  )
}

/* ------------------------------------------------------------------- card */
export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`card ${className ?? ''}`}>
      {title || actions ? (
        <header className="card-header">
          <div>
            {title ? <h2>{title}</h2> : null}
            {subtitle ? <p className="subtitle">{subtitle}</p> : null}
          </div>
          {actions ? <div className="flex-row">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  )
}

/* ------------------------------------------------------------------ modal */
export function Modal({
  title,
  children,
  footer,
  onClose,
  wide = false,
}: {
  title: ReactNode
  children: ReactNode
  footer?: ReactNode
  onClose: () => void
  wide?: boolean
}) {
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" style={wide ? { width: 'min(960px, 100%)' } : undefined}>
        <header className="modal-head">
          <h2>{title}</h2>
          <button ref={closeRef} type="button" className="btn ghost small" onClick={onClose}>
            Close
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer className="modal-foot">{footer}</footer> : null}
      </div>
    </div>
  )
}

/* -------------------------------------------------------------- data table */
export interface Column<T> {
  key: string
  header: ReactNode
  render: (row: T) => ReactNode
  numeric?: boolean
  width?: string
}

export function DataTable<T>({
  columns,
  rows,
  empty = 'No records to show.',
  loading = false,
  onRowClick,
  rowKey,
}: {
  columns: Column<T>[]
  rows: T[]
  empty?: ReactNode
  loading?: boolean
  onRowClick?: (row: T) => void
  rowKey: (row: T) => string | number
}) {
  if (loading) {
    return (
      <div className="loading-row">
        <span className="spinner" /> Loading…
      </div>
    )
  }
  if (rows.length === 0) {
    return <div className="empty">{empty}</div>
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                className={column.numeric ? 'numeric' : undefined}
                style={column.width ? { width: column.width } : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              style={onRowClick ? { cursor: 'pointer' } : undefined}
            >
              {columns.map((column) => (
                <td key={column.key} className={column.numeric ? 'numeric' : undefined}>
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* -------------------------------------------------------------- bar chart */
export interface ChartBar {
  label: string
  value: number
  secondary?: number
}

/**
 * A stacked bar chart drawn with plain divs - no charting dependency.
 * `value` is the successful portion, `secondary` the remainder, so a day with
 * 40 scans of which 36 verified shows the ratio at a glance.
 */
export function BarChart({ data, unit = '' }: { data: ChartBar[]; unit?: string }) {
  const max = data.reduce((highest, bar) => Math.max(highest, bar.value + (bar.secondary ?? 0)), 0)
  if (data.length === 0 || max === 0) {
    return <div className="empty">No data for this period yet.</div>
  }
  return (
    <div>
      <div className="chart">
        {data.map((bar) => {
          const total = bar.value + (bar.secondary ?? 0)
          const heightPercent = (total / max) * 100
          const verifiedPercent = total === 0 ? 0 : (bar.value / total) * 100
          return (
            <div className="bar-group" key={bar.label} title={`${bar.label}: ${total}${unit}`}>
              <div className="bar-stack" style={{ height: `${heightPercent}%` }}>
                <div className="bar other" style={{ height: `${100 - verifiedPercent}%` }} />
                <div className="bar verified" style={{ height: `${verifiedPercent}%` }} />
              </div>
              <span className="bar-label">{bar.label}</span>
            </div>
          )
        })}
      </div>
      <div className="legend">
        <span>
          <span className="swatch" style={{ background: 'var(--success)' }} />
          Verified
        </span>
        <span>
          <span className="swatch" style={{ background: 'var(--danger)' }} />
          Other results
        </span>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ error banner */
export function ErrorBanner({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  if (!error) return null
  return (
    <div className="banner danger" role="alert">
      <div className="flex-row">
        <span style={{ flex: 1 }}>{error}</span>
        {onRetry ? (
          <button type="button" className="btn small" onClick={onRetry}>
            Retry
          </button>
        ) : null}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- empty state */
export function EmptyState({ icon = '📋', title, body, action }: { icon?: string; title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <div style={{ fontSize: '2rem' }}>{icon}</div>
      <h3 style={{ marginTop: 8 }}>{title}</h3>
      {body ? <p className="muted small">{body}</p> : null}
      {action ? <div style={{ marginTop: 12 }}>{action}</div> : null}
    </div>
  )
}
