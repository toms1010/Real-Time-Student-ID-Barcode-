/** Scan history with filters, paging and CSV/XLSX export. */

import { useMemo, useState } from 'react'

import { Card, DataTable, ErrorBanner, type Column } from '@/components/ui'
import { useApi, useLocalState } from '@/hooks/useApi'
import { useAuth } from '@/hooks/useAuth'
import { useToast } from '@/hooks/useToast'
import { ApiError, reports, scans } from '@/services/api'
import { endOfDayIso, formatMs, formatTime, startOfDayIso, todayIso } from '@/utils/format'
import { resultLabel, resultTone } from '@/utils/ui'
import type { ExportFormat, Scan, ScanResult } from '@/types'

const RESULTS: ScanResult[] = [
  'VERIFIED',
  'UNKNOWN_ID',
  'INVALID_BARCODE',
  'DUPLICATE_SCAN',
  'UNSUPPORTED_FORMAT',
  'ERROR',
]

const PAGE_SIZE = 50

export function ScanHistoryPage() {
  const { can } = useAuth()
  const toast = useToast()
  const [date, setDate] = useState(todayIso())
  const [useRange, setUseRange] = useState(false)
  const [from, setFrom] = useState(todayIso())
  const [to, setTo] = useState(todayIso())
  const [result, setResult] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [exporting, setExporting] = useState<ExportFormat | null>(null)
  const [pageSize, setPageSize] = useLocalState('sidb-history-page-size', PAGE_SIZE)

  const query = useMemo(
    () => ({
      start: useRange ? startOfDayIso(new Date(from)) : startOfDayIso(new Date(date)),
      end: useRange ? endOfDayIso(new Date(to)) : endOfDayIso(new Date(date)),
      result: result || undefined,
      search: search || undefined,
      limit: pageSize,
      offset: page * pageSize,
      order: 'desc',
    }),
    [date, from, to, useRange, result, search, page, pageSize],
  )

  const list = useApi(() => scans.list(query), [query])
  const summary = useApi(() => scans.summary({ start: query.start, end: query.end }), [
    query.start,
    query.end,
  ])

  const rows = list.data?.items ?? []
  const total = list.data?.page.total ?? 0

  const doExport = async (format: ExportFormat) => {
    setExporting(format)
    try {
      const descriptor = await reports.export({
        report: 'scans',
        format,
        start: query.start,
        end: query.end,
        result: (result || null) as ScanResult | null,
        limit: 50_000,
      })
      // Two-step export: the service wrote the file, now download it.
      const link = document.createElement('a')
      link.href = descriptor.download_url
      link.download = descriptor.filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      toast.success(`Exported ${descriptor.rows} rows`, descriptor.filename)
    } catch (cause) {
      toast.error('Export failed', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setExporting(null)
    }
  }

  const columns: Column<Scan>[] = [
    { key: 'time', header: 'Time', render: (row) => formatTime(row.scan_time) },
    {
      key: 'student',
      header: 'Student ID',
      render: (row) => <span className="mono">{row.student_number ?? '—'}</span>,
    },
    {
      key: 'name',
      header: 'Student',
      render: (row) => row.student?.name ?? <span className="dim">Unknown</span>,
    },
    {
      key: 'barcode',
      header: 'Barcode',
      render: (row) => (
        <>
          <span className="mono">{row.barcode_value}</span>
          {row.barcode_type ? <span className="dim small"> {row.barcode_type}</span> : null}
        </>
      ),
    },
    {
      key: 'result',
      header: 'Result',
      render: (row) => (
        <span className={`chip ${resultTone(row.result)}`}>
          <span className="dot" />
          {resultLabel(row.result)}
        </span>
      ),
    },
    {
      key: 'timing',
      header: 'Timing',
      numeric: true,
      render: (row) => (
        <span className="small mono">
          {row.detection_time_ms !== null ? `${row.detection_time_ms.toFixed(1)}ms` : '—'}
          {' / '}
          {row.database_time_ms !== null ? `${row.database_time_ms.toFixed(2)}ms` : '—'}
        </span>
      ),
    },
    { key: 'device', header: 'Device', render: (row) => <span className="small dim">{row.device_name ?? row.source}</span> },
  ]

  const counters = summary.data ?? {}

  return (
    <>
      <ErrorBanner error={list.error} onRetry={list.reload} />

      <div className="grid cols-4">
        <Counter label="Matching scans" value={counters.total} tone="accent" />
        <Counter label="Verified" value={counters.verified} tone="success" />
        <Counter label="Unknown ID" value={counters.unknown_id} tone="warning" />
        <Counter
          label="Duplicates"
          value={counters.duplicate_scan}
          hint="suppressed by the cooldown"
        />
      </div>

      <Card
        title="Scan history"
        subtitle="Every scan attempt is logged, successful or not"
        actions={
          can('reports:export') ? (
            <>
              <button
                type="button"
                className="btn small"
                onClick={() => doExport('csv')}
                disabled={exporting !== null}
              >
                {exporting === 'csv' ? <span className="spinner" /> : null}
                Export CSV
              </button>
              <button
                type="button"
                className="btn small primary"
                onClick={() => doExport('xlsx')}
                disabled={exporting !== null}
              >
                {exporting === 'xlsx' ? <span className="spinner" /> : null}
                Export Excel
              </button>
              <button type="button" className="btn small" onClick={() => window.print()}>
                Print
              </button>
            </>
          ) : null
        }
      >
        <div className="toolbar" style={{ marginBottom: 16 }}>
          {!useRange ? (
            <div className="field">
              <label htmlFor="date">Date</label>
              <input
                id="date"
                type="date"
                value={date}
                onChange={(event) => {
                  setDate(event.target.value)
                  setPage(0)
                }}
              />
            </div>
          ) : (
            <>
              <div className="field">
                <label htmlFor="from">From</label>
                <input
                  id="from"
                  type="date"
                  value={from}
                  onChange={(event) => {
                    setFrom(event.target.value)
                    setPage(0)
                  }}
                />
              </div>
              <div className="field">
                <label htmlFor="to">To</label>
                <input
                  id="to"
                  type="date"
                  value={to}
                  onChange={(event) => {
                    setTo(event.target.value)
                    setPage(0)
                  }}
                />
              </div>
            </>
          )}
          <label className="inline-check" style={{ marginBottom: 8 }}>
            <input
              type="checkbox"
              checked={useRange}
              onChange={(event) => {
                setUseRange(event.target.checked)
                setPage(0)
              }}
            />
            Date range
          </label>

          <div className="field">
            <label htmlFor="result">Result</label>
            <select
              id="result"
              value={result}
              onChange={(event) => {
                setResult(event.target.value)
                setPage(0)
              }}
            >
              <option value="">All</option>
              {RESULTS.map((value) => (
                <option key={value} value={value}>
                  {resultLabel(value)}
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ flex: 1, minWidth: 200 }}>
            <label htmlFor="search">Search</label>
            <input
              id="search"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value)
                setPage(0)
              }}
              placeholder="Barcode, student ID or name…"
            />
          </div>

          <div className="field">
            <label htmlFor="page_size">Per page</label>
            <select
              id="page_size"
              value={pageSize}
              onChange={(event) => {
                setPageSize(Number(event.target.value))
                setPage(0)
              }}
            >
              {[25, 50, 100, 200].map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </div>
        </div>

        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={list.loading}
          empty="No scans match these filters."
        />

        <div className="table-footer">
          <span>
            Showing {rows.length === 0 ? 0 : page * pageSize + 1}–{page * pageSize + rows.length} of{' '}
            {total}
            {counters.avg_processing_ms ? ` · avg ${formatMs(Number(counters.avg_processing_ms))}` : ''}
          </span>
          <div className="flex-row">
            <button
              type="button"
              className="btn small"
              onClick={() => setPage((value) => Math.max(0, value - 1))}
              disabled={page === 0}
            >
              Previous
            </button>
            <button
              type="button"
              className="btn small"
              onClick={() => setPage((value) => value + 1)}
              disabled={!list.data?.page.has_more}
            >
              Next
            </button>
          </div>
        </div>
      </Card>
    </>
  )
}

function Counter({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: number | null | undefined
  tone?: 'success' | 'warning' | 'accent'
  hint?: string
}) {
  return (
    <div className={`stat ${tone ?? ''}`}>
      <span className="label">{label}</span>
      <span className="value">{(value ?? 0).toLocaleString()}</span>
      {hint ? <span className="hint">{hint}</span> : null}
    </div>
  )
}
