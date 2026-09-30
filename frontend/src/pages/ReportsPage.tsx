/** Reports: daily summary, performance, unknown barcodes, exports. */

import { useState } from 'react'

import { BarChart, Card, DataTable, ErrorBanner, Stat, type Column } from '@/components/ui'
import { useApi } from '@/hooks/useApi'
import { useAuth } from '@/hooks/useAuth'
import { useToast } from '@/hooks/useToast'
import { ApiError, reports } from '@/services/api'
import { formatDateTime, formatMs, formatNumber, todayIso } from '@/utils/format'
import type { DailyRow, ExportFormat, PerformanceReport } from '@/types'

type Tab = 'summary' | 'performance' | 'unknown' | 'exports'

const TABS: Array<{ id: Tab; label: string; permission?: string }> = [
  { id: 'summary', label: 'Scan summary' },
  { id: 'performance', label: 'System performance' },
  { id: 'unknown', label: 'Unknown barcodes' },
  { id: 'exports', label: 'Export centre', permission: 'reports:export' },
]

export function ReportsPage() {
  const { can } = useAuth()
  const [tab, setTab] = useState<Tab>('summary')
  const [period, setPeriod] = useState<'daily' | 'weekly' | 'monthly'>('daily')
  const [date, setDate] = useState(todayIso())

  const daily = useApi(
    () => reports.daily({ period, date }),
    [period, date],
  )
  const performance = useApi(
    () => reports.performance({ period, date }),
    [period, date],
  )
  const unknown = useApi(() => reports.unknownBarcodes({ period }), [period], 60_000)

  const visibleTabs = TABS.filter((item) => !item.permission || can(item.permission))
  const totals = daily.data?.totals ?? {}

  return (
    <>
      <div className="toolbar" style={{ marginBottom: 4 }}>
        <div className="field">
          <label htmlFor="period">Period</label>
          <select id="period" value={period} onChange={(event) => setPeriod(event.target.value as typeof period)}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="anchor">Anchor date</label>
          <input id="anchor" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
        </div>
        <div className="field" style={{ flex: 1 }} />
        <div className="flex-row" role="tablist">
          {visibleTabs.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              className={`btn small ${tab === item.id ? 'primary' : ''}`}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'summary' ? <ErrorBanner error={daily.error} onRetry={daily.reload} /> : null}

      {tab === 'summary' && daily.data ? (
        <>
          <div className="grid cols-4">
            <Stat label="Total scans" value={formatNumber(Number(totals.total ?? 0))} hint={daily.data.label} tone="accent" />
            <Stat label="Verified" value={formatNumber(Number(totals.verified ?? 0))} tone="success" />
            <Stat label="Unknown ID" value={formatNumber(Number(totals.unknown_id ?? 0))} tone="warning" />
            <Stat label="Errors" value={formatNumber(Number(totals.errors ?? 0))} tone="danger" />
          </div>

          <Card title="Daily breakdown" subtitle={`${daily.data.start} → ${daily.data.end} (${daily.data.timezone})`}>
            <BarChart
              data={[...daily.data.days].reverse().map((day) => ({
                label: day.scan_date.slice(5),
                value: day.verified,
                secondary: day.total_scans - day.verified,
              }))}
            />
            <DailyTable days={daily.data.days} />
          </Card>

          <div className="grid cols-2">
            <Card title="Most frequent students">
              <DataTable
                columns={[
                  { key: 'id', header: 'Student ID', render: (row) => <span className="mono">{row.student_number}</span> },
                  { key: 'name', header: 'Name', render: (row) => row.full_name },
                  { key: 'scans', header: 'Scans', numeric: true, render: (row) => row.total_scans },
                  { key: 'last', header: 'Last seen', render: (row) => formatDateTime(row.last_scan_time) },
                ]}
                rows={daily.data.top_students}
                rowKey={(row) => row.student_number}
                empty="No scans in this period."
              />
            </Card>

            <Card title="Hourly profile" subtitle="When the cashier is busiest">
              <BarChart
                data={daily.data.hourly_profile.map((hour) => ({
                  label: `${hour.hour}:00`,
                  value: hour.verified,
                  secondary: hour.scans - hour.verified,
                }))}
              />
            </Card>
          </div>
        </>
      ) : null}

      {tab === 'performance' && performance.data ? (
        <PerformanceView report={performance.data} />
      ) : null}

      {tab === 'unknown' ? (
        <Card
          title="Unknown and invalid barcodes"
          subtitle="Cards that did not resolve to a student - a good check for a misprinted or borrowed ID"
        >
          <ErrorBanner error={unknown.error} onRetry={unknown.reload} />
          <DataTable
            columns={[
              { key: 'barcode', header: 'Barcode', render: (row) => <span className="mono">{row.barcode_value}</span> },
              { key: 'attempts', header: 'Attempts', numeric: true, render: (row) => row.attempts },
              { key: 'first', header: 'First seen', render: (row) => formatDateTime(row.first_seen) },
              { key: 'last', header: 'Last seen', render: (row) => formatDateTime(row.last_seen) },
            ]}
            rows={unknown.data?.rows ?? []}
            rowKey={(row) => row.barcode_value}
            loading={unknown.loading}
            empty="Every barcode in this period resolved to a student."
          />
        </Card>
      ) : null}

      {tab === 'exports' ? <ExportCentre date={date} /> : null}
    </>
  )
}

function DailyTable({ days }: { days: DailyRow[] }) {
  const columns: Column<DailyRow>[] = [
    { key: 'date', header: 'Date', render: (row) => row.scan_date },
    { key: 'total', header: 'Total', numeric: true, render: (row) => row.total_scans },
    { key: 'verified', header: 'Verified', numeric: true, render: (row) => row.verified },
    { key: 'unknown', header: 'Unknown', numeric: true, render: (row) => row.unknown_id },
    { key: 'invalid', header: 'Invalid', numeric: true, render: (row) => row.invalid_barcode },
    { key: 'dup', header: 'Duplicates', numeric: true, render: (row) => row.duplicate_scan },
    { key: 'errors', header: 'Errors', numeric: true, render: (row) => row.errors },
    { key: 'avg', header: 'Avg ms', numeric: true, render: (row) => formatMs(row.avg_processing_ms) },
  ]
  return (
    <div style={{ marginTop: 16 }}>
      <DataTable columns={columns} rows={days} rowKey={(row) => row.scan_date} empty="No data." />
    </div>
  )
}

function PerformanceView({ report }: { report: PerformanceReport }) {
  return (
    <>
      <div className="banner info">
        Every figure below is computed from the recorded <span className="mono">scan_logs</span> rows
        for this period. Nothing here is estimated — an empty period reports zeros, not guesses.
      </div>
      <div className="grid cols-4">
        <Stat label="Scans logged" value={formatNumber(report.scans)} hint={`${report.frames_observed} rows scanned`} />
        <Stat
          label="Verified"
          value={formatNumber(report.verified)}
          hint={report.scans ? `${((report.verified / report.scans) * 100).toFixed(1)}%` : '—'}
          tone="success"
        />
        <Stat
          label="Avg frame time"
          value={formatMs(report.avg_processing_ms)}
          hint={`p95 ${formatMs(report.p95_processing_ms)}`}
          tone="accent"
        />
        <Stat
          label="Avg DB lookup"
          value={formatMs(report.avg_database_ms)}
          hint={`budget: under 100 ms`}
        />
      </div>

      <div className="grid cols-2">
        <Card title="Timing breakdown" subtitle="Where the milliseconds go">
          <dl className="detail-list">
            <dt>Average frame processing</dt>
            <dd>{formatMs(report.avg_processing_ms)}</dd>
            <dt>Minimum</dt>
            <dd>{formatMs(report.min_processing_ms)}</dd>
            <dt>Maximum</dt>
            <dd>{formatMs(report.max_processing_ms)}</dd>
            <dt>95th percentile</dt>
            <dd>{formatMs(report.p95_processing_ms)}</dd>
            <dt>Average detection + decode</dt>
            <dd>{formatMs(report.avg_detection_ms)}</dd>
            <dt>Average database lookup</dt>
            <dd>{formatMs(report.avg_database_ms)}</dd>
            <dt>Average decoder confidence</dt>
            <dd>{report.avg_confidence ?? 'n/a'}</dd>
          </dl>
          <p className="small dim" style={{ marginTop: 12, marginBottom: 0 }}>
            ZXing-C++ validates every symbol it returns but does not expose a numeric score, so
            confidence is null rather than invented (ADR-011).
          </p>
        </Card>

        <Card title="By device">
          <DataTable
            columns={[
              { key: 'device', header: 'Device', render: (row) => row.device_name },
              { key: 'scans', header: 'Scans', numeric: true, render: (row) => row.scans },
              { key: 'verified', header: 'Verified', numeric: true, render: (row) => row.verified },
              { key: 'avg', header: 'Avg ms', numeric: true, render: (row) => formatMs(row.avg_processing_ms) },
            ]}
            rows={report.by_device}
            rowKey={(row) => row.device_name}
            empty="No scans recorded in this period."
          />
        </Card>
      </div>

      <Card title="Slowest scans" subtitle="Useful for spotting a camera or driver that needs attention">
        <DataTable
          columns={[
            { key: 'time', header: 'Time', render: (row) => formatDateTime(String(row.scan_time)) },
            { key: 'barcode', header: 'Barcode', render: (row) => <span className="mono">{row.barcode_value}</span> },
            { key: 'total', header: 'Total ms', numeric: true, render: (row) => formatMs(Number(row.processing_time_ms)) },
            { key: 'detect', header: 'Detect ms', numeric: true, render: (row) => formatMs(Number(row.detection_time_ms)) },
            { key: 'db', header: 'DB ms', numeric: true, render: (row) => formatMs(Number(row.database_time_ms)) },
            { key: 'device', header: 'Device', render: (row) => String(row.device_name ?? '—') },
          ]}
          rows={report.slowest}
          rowKey={(row) => String(row.id)}
          empty="No verified scans in this period."
        />
      </Card>
    </>
  )
}

function ExportCentre({ date }: { date: string }) {
  const toast = useToast()
  const [busy, setBusy] = useState<string | null>(null)

  const run = async (
    report: 'daily' | 'scans' | 'transactions' | 'unknown_barcodes' | 'performance',
    format: ExportFormat,
  ) => {
    const key = `${report}-${format}`
    setBusy(key)
    try {
      const descriptor = await reports.export({
        report,
        format,
        start: `${date}T00:00:00.000Z`,
        end: `${date}T23:59:59.999Z`,
        limit: 50_000,
      })
      const link = document.createElement('a')
      link.href = descriptor.download_url
      link.download = descriptor.filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      toast.success(
        `Exported ${descriptor.rows} rows to ${descriptor.filename}`,
        'The file is in the exports/ folder as well.',
      )
    } catch (cause) {
      toast.error('Export failed', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setBusy(null)
    }
  }

  const reportsList: Array<{ id: 'daily' | 'scans' | 'transactions' | 'unknown_barcodes' | 'performance'; label: string; description: string }> = [
    { id: 'daily', label: 'Daily scan report', description: 'Per-day counters, verification ratio and average processing time.' },
    { id: 'scans', label: 'Scan history', description: 'Every scan with its barcode, student, result and timings.' },
    { id: 'transactions', label: 'Transactions', description: 'Cashier transactions with amounts, references and status.' },
    { id: 'unknown_barcodes', label: 'Unknown barcodes', description: 'Cards that did not resolve, for registrar follow-up.' },
    { id: 'performance', label: 'System performance', description: 'Per-device timing statistics from the recorded scans.' },
  ]

  return (
    <Card title="Export centre" subtitle={`Exports cover ${date}`}>
      <div className="stack">
        {reportsList.map((item) => (
          <div className="flex-row" key={item.id}>
            <div style={{ flex: 1, minWidth: 240 }}>
              <strong>{item.label}</strong>
              <div className="small muted">{item.description}</div>
            </div>
            {(['csv', 'xlsx', 'pdf', 'json'] as ExportFormat[]).map((format) => (
              <button
                key={format}
                type="button"
                className={`btn small ${format === 'xlsx' ? 'primary' : ''}`}
                disabled={busy !== null}
                onClick={() => run(item.id, format)}
              >
                {busy === `${item.id}-${format}` ? <span className="spinner" /> : null}
                {format.toUpperCase()}
              </button>
            ))}
          </div>
        ))}
        <p className="small dim" style={{ margin: 0 }}>
          Exports are written to the server's <span className="mono">exports/</span> folder and are
          downloadable from the browser. PDF output is a text table rendered by Pillow; use XLSX for
          analysis.
        </p>
      </div>
    </Card>
  )
}
