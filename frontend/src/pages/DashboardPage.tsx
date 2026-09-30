/** Dashboard: counters, 7-day trend, most recent scans, pipeline health. */

import { Link } from 'react-router-dom'

import { Card, Chip, DataTable, ErrorBanner, Stat, type Column } from '@/components/ui'
import { useApi } from '@/hooks/useApi'
import { reports, scans, system } from '@/services/api'
import { formatCurrency, formatMs, formatNumber, formatRelative, formatTime } from '@/utils/format'
import { resultLabel, resultTone } from '@/utils/ui'
import type { Scan } from '@/types'

export function DashboardPage() {
  const summary = useApi(() => reports.summary(), [], 15_000)
  const recent = useApi(() => scans.recent(8), [], 10_000)
  const state = useApi(() => system.scannerState(), [], 5_000)

  const data = summary.data

  const columns: Column<Scan>[] = [
    { key: 'time', header: 'Time', render: (row) => formatTime(row.scan_time) },
    {
      key: 'student',
      header: 'Student',
      render: (row) => (
        <>
          <div>{row.student?.name ?? <span className="dim">—</span>}</div>
          <div className="mono dim small">{row.student_number ?? row.barcode_value}</div>
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
    { key: 'time_ms', header: 'Total', numeric: true, render: (row) =>
      formatMs(
        row.detection_time_ms !== null && row.database_time_ms !== null
          ? row.detection_time_ms + row.database_time_ms
          : row.processing_time_ms,
      ),
    },
  ]

  return (
    <>
      <ErrorBanner error={summary.error} onRetry={summary.reload} />

      <div className="grid cols-4">
        <Stat
          label="Students"
          value={formatNumber(data?.students_total)}
          hint={`${formatNumber(data?.students_active)} active`}
          tone="accent"
        />
        <Stat
          label="Scans today"
          value={formatNumber(data?.scans_today)}
          hint={`${formatNumber(data?.scans_total)} all time`}
        />
        <Stat
          label="Verified today"
          value={formatNumber(data?.verified_today)}
          hint={
            data && data.scans_today > 0
              ? `${((data.verified_today / data.scans_today) * 100).toFixed(1)}% of today's scans`
              : 'no scans yet'
          }
          tone="success"
        />
        <Stat
          label="Needs attention"
          value={formatNumber((data?.unknown_today ?? 0) + (data?.errors_today ?? 0))}
          hint={`${formatNumber(data?.duplicates_today)} duplicates suppressed`}
          tone="warning"
        />
      </div>

      <div className="grid cols-2">
        <Card title="Scans - last 7 days" subtitle="Verified (green) versus all other results">
          <BarChartWrapper trend={data?.trend_7d ?? []} />
        </Card>

        <Card
          title="Pipeline status"
          subtitle="Live state of the capture and decode threads"
          actions={
            <Link className="btn small" to="/scanner">
              Open scanner
            </Link>
          }
        >
          {state.data ? (
            <div className="stack">
              <div className="flex-row">
                <Chip
                  tone={
                    state.data.error_code
                      ? 'danger'
                      : state.data.camera_connected
                        ? 'success'
                        : 'neutral'
                  }
                  pulse={state.data.camera_connected}
                >
                  {state.data.state}
                </Chip>
                <span className="muted small">{state.data.camera_device ?? 'no camera'}</span>
              </div>
              <dl className="detail-list">
                <dt>Frame rate</dt>
                <dd>{state.data.fps?.toFixed?.(1) ?? '—'} fps</dd>
                <dt>Avg frame time</dt>
                <dd>{formatMs(state.data.avg_processing_ms)}</dd>
                <dt>Avg detection</dt>
                <dd>{formatMs(state.data.avg_detection_ms)}</dd>
                <dt>Avg DB lookup</dt>
                <dd>{formatMs(state.data.avg_database_ms)}</dd>
                <dt>Frames processed</dt>
                <dd>{formatNumber(state.data.frames_processed)}</dd>
                <dt>Frame source</dt>
                <dd>{state.data.stream_source}</dd>
              </dl>
              {state.data.error_code ? (
                <div className="banner warning">
                  {state.data.error_message ?? state.data.error_code}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="loading-row">
              <span className="spinner" /> Reading the pipeline state…
            </div>
          )}
        </Card>
      </div>

      <div className="grid cols-2">
        <Card
          title="Recent scans"
          actions={
            <Link className="btn small" to="/history">
              View all
            </Link>
          }
        >
          <DataTable
            columns={columns}
            rows={recent.data ?? []}
            rowKey={(row) => row.id}
            loading={recent.loading}
            empty="No scans recorded yet. Start the scanner to record the first one."
          />
        </Card>

        <Card title="Today's activity">
          <div className="grid cols-2">
            <Stat label="Transactions" value={formatNumber(data?.transactions_today)} />
            <Stat label="Collected" value={formatCurrency(data?.revenue_today)} />
            <Stat
              label="Avg processing"
              value={formatMs(data?.avg_processing_ms)}
              hint="target: under 100 ms per frame"
            />
            <Stat
              label="Last scan"
              value={data?.last_scan ? formatRelative(data.last_scan.scan_time) : '—'}
              hint={data?.last_scan?.student_id ?? undefined}
            />
          </div>
          {data?.last_scan ? (
            <p className="small muted" style={{ marginTop: 16, marginBottom: 0 }}>
              Last result: <strong>{resultLabel(data.last_scan.result)}</strong>
              {data.last_scan.name ? ` for ${data.last_scan.name}` : ''}
            </p>
          ) : null}
        </Card>
      </div>
    </>
  )
}

function BarChartWrapper({
  trend,
}: {
  trend: Array<{
    scan_date: string
    total_scans: number
    verified: number
  }>
}) {
  if (trend.length === 0) {
    return <div className="empty">No scan history yet.</div>
  }
  const reversed = [...trend].reverse()
  return (
    <div className="chart">
      {reversed.map((day) => (
        <div
          className="bar-group"
          key={day.scan_date}
          title={`${day.scan_date}: ${day.verified}/${day.total_scans} verified`}
        >
          <div className="bar-stack" style={{ height: `${100 * (day.total_scans / maxTotal(reversed))}%` }}>
            <div
              className="bar other"
              style={{ height: `${100 - (day.total_scans ? (day.verified / day.total_scans) * 100 : 0)}%` }}
            />
            <div
              className="bar verified"
              style={{ height: `${day.total_scans ? (day.verified / day.total_scans) * 100 : 0}%` }}
            />
          </div>
          <span className="bar-label">{day.scan_date.slice(5)}</span>
        </div>
      ))}
    </div>
  )
}

function maxTotal(trend: Array<{ total_scans: number }>): number {
  return trend.reduce((highest, day) => Math.max(highest, day.total_scans), 0) || 1
}
