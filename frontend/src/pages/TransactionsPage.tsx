/** Transaction history with cancellation and export. */

import { useMemo, useState } from 'react'

import { Card, DataTable, ErrorBanner, Modal, type Column } from '@/components/ui'
import { useApi, useLocalState } from '@/hooks/useApi'
import { useAuth } from '@/hooks/useAuth'
import { useToast } from '@/hooks/useToast'
import { ApiError, reports, transactions as transactionsApi } from '@/services/api'
import { endOfDayIso, formatCurrency, formatDateTime, startOfDayIso, todayIso } from '@/utils/format'
import type { ExportFormat, Transaction } from '@/types'

const STATUSES: Record<string, string> = {
  completed: 'success',
  pending: 'warning',
  cancelled: 'neutral',
  refunded: 'info',
}

export function TransactionsPage() {
  const { can } = useAuth()
  const toast = useToast()
  const [date, setDate] = useState(todayIso())
  const [type, setType] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(0)
  const [cancelling, setCancelling] = useState<Transaction | null>(null)
  const [reason, setReason] = useState('')
  const [exporting, setExporting] = useState<ExportFormat | null>(null)
  const [pageSize, setPageSize] = useLocalState('sidb-transactions-page-size', 25)

  const query = useMemo(
    () => ({
      start: startOfDayIso(new Date(date)),
      end: endOfDayIso(new Date(date)),
      transaction_type: type || undefined,
      status: status || undefined,
      limit: pageSize,
      offset: page * pageSize,
    }),
    [date, type, status, page, pageSize],
  )

  const list = useApi(() => transactionsApi.list(query), [query])
  const totals = useApi(() => transactionsApi.totals({ start: query.start, end: query.end }), [
    query.start,
    query.end,
  ])

  const rows = list.data?.items ?? []
  const total = list.data?.page.total ?? 0

  const doExport = async (format: ExportFormat) => {
    setExporting(format)
    try {
      const descriptor = await reports.export({
        report: 'transactions',
        format,
        start: query.start,
        end: query.end,
        limit: 50_000,
      })
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

  const confirmCancel = async () => {
    if (!cancelling) return
    try {
      await transactionsApi.setStatus(cancelling.reference_number, 'cancelled', reason || undefined)
      toast.success('Transaction cancelled', cancelling.reference_number)
      setCancelling(null)
      setReason('')
      await list.reload()
      await totals.reload()
    } catch (cause) {
      toast.error('Could not cancel', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const columns: Column<Transaction>[] = [
    { key: 'created', header: 'Date', render: (row) => formatDateTime(row.created_at) },
    {
      key: 'reference',
      header: 'Reference',
      render: (row) => <span className="mono small">{row.reference_number}</span>,
    },
    {
      key: 'student',
      header: 'Student',
      render: (row) => (
        <>
          <div className="mono small">{row.student_number ?? '—'}</div>
          <div className="small dim">{row.student?.name ?? ''}</div>
        </>
      ),
    },
    { key: 'type', header: 'Type', render: (row) => row.transaction_type },
    {
      key: 'amount',
      header: 'Amount',
      numeric: true,
      render: (row) => (row.transaction_type === 'Payment' ? formatCurrency(row.amount) : '—'),
    },
    {
      key: 'status',
      header: 'Status',
      render: (row) => (
        <span className={`chip ${STATUSES[row.status] ?? 'neutral'}`}>
          <span className="dot" />
          {row.status}
        </span>
      ),
    },
    { key: 'cashier', header: 'Cashier', render: (row) => row.cashier_username ?? '—' },
    { key: 'note', header: 'Note', render: (row) => row.reference_note ?? '—' },
    {
      key: 'actions',
      header: '',
      render: (row) =>
        can('transaction:cancel') && (row.status === 'completed' || row.status === 'pending') ? (
          <button
            type="button"
            className="btn ghost small"
            onClick={(event) => {
              event.stopPropagation()
              setCancelling(row)
            }}
          >
            Cancel
          </button>
        ) : null,
    },
  ]

  return (
    <>
      <ErrorBanner error={list.error} onRetry={list.reload} />

      <div className="grid cols-4">
        <div className="stat accent">
          <span className="label">Transactions</span>
          <span className="value">{(totals.data?.count ?? 0).toLocaleString()}</span>
        </div>
        <div className="stat success">
          <span className="label">Collected</span>
          <span className="value">{formatCurrency(totals.data?.total_amount ?? 0)}</span>
        </div>
        <div className="stat">
          <span className="label">Average</span>
          <span className="value">{formatCurrency(totals.data?.avg_amount ?? 0)}</span>
        </div>
        <div className="stat warning">
          <span className="label">Cancelled</span>
          <span className="value">{(totals.data?.cancelled ?? 0).toLocaleString()}</span>
        </div>
      </div>

      <Card
        title="Transactions"
        subtitle={`${total} record${total === 1 ? '' : 's'} on ${date}`}
        actions={
          can('reports:export') ? (
            <>
              <button
                type="button"
                className="btn small"
                onClick={() => doExport('csv')}
                disabled={exporting !== null}
              >
                Export CSV
              </button>
              <button
                type="button"
                className="btn small primary"
                onClick={() => doExport('xlsx')}
                disabled={exporting !== null}
              >
                Export Excel
              </button>
            </>
          ) : null
        }
      >
        <div className="toolbar" style={{ marginBottom: 16 }}>
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
          <div className="field">
            <label htmlFor="type">Type</label>
            <select
              id="type"
              value={type}
              onChange={(event) => {
                setType(event.target.value)
                setPage(0)
              }}
            >
              <option value="">All</option>
              <option value="Payment">Payment</option>
              <option value="Verification">Verification</option>
              <option value="Registration">Registration</option>
              <option value="Other">Other</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="status">Status</label>
            <select
              id="status"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value)
                setPage(0)
              }}
            >
              <option value="">All</option>
              <option value="completed">Completed</option>
              <option value="pending">Pending</option>
              <option value="cancelled">Cancelled</option>
              <option value="refunded">Refunded</option>
            </select>
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
              {[10, 25, 50, 100].map((size) => (
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
          empty="No transactions recorded for this date."
        />

        <div className="table-footer">
          <span>
            Showing {rows.length === 0 ? 0 : page * pageSize + 1}–{page * pageSize + rows.length} of{' '}
            {total}
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

      {cancelling ? (
        <Modal
          title={`Cancel ${cancelling.reference_number}?`}
          onClose={() => setCancelling(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setCancelling(null)}>
                Keep it
              </button>
              <button type="button" className="btn danger" onClick={confirmCancel}>
                Cancel transaction
              </button>
            </>
          }
        >
          <p>
            {cancelling.student?.name ?? 'This transaction'} ·{' '}
            {cancelling.transaction_type === 'Payment'
              ? formatCurrency(cancelling.amount)
              : cancelling.transaction_type}
          </p>
          <p className="small muted">
            The record is kept and marked as cancelled - it is never deleted, so the cashier's audit
            trail stays complete.
          </p>
          <div className="field">
            <label htmlFor="reason">Reason (optional)</label>
            <input id="reason" value={reason} onChange={(event) => setReason(event.target.value)} />
          </div>
        </Modal>
      ) : null}
    </>
  )
}
