/**
 * The cashier screen.
 *
 * Live camera on the left, the workflow status and result on the right, plus a
 * manual-entry fallback for a damaged card.
 *
 * Every button on this page comes from the workflow: the page asks
 * `useScanWorkflow` which actions the current state allows and renders those.
 * It cannot offer *Confirm transaction* unless the state is
 * READY_FOR_TRANSACTION, and while a transaction is being saved the form is
 * locked, so the same charge cannot be submitted twice.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { CameraView } from '@/components/CameraView'
import { Card, Chip, ErrorBanner } from '@/components/ui'
import { ScanResultPanel } from '@/components/ScanResultPanel'
import { useApi } from '@/hooks/useApi'
import { useAuth } from '@/hooks/useAuth'
import { useScanWorkflow } from '@/hooks/useScanWorkflow'
import { useToast } from '@/hooks/useToast'
import { ApiError, scans, system, transactions } from '@/services/api'
import type { ScanActionName } from '@/machine/scanState'
import { formatCurrency, formatRelative, formatTime } from '@/utils/format'
import type { ScanVerification, Transaction, TransactionType } from '@/types'

const TRANSACTION_TYPES: TransactionType[] = [
  'Payment',
  'Verification',
  'Registration',
  'Other',
]

export function ScannerPage() {
  const { can } = useAuth()
  const toast = useToast()
  const navigate = useNavigate()
  const state = useApi(() => system.scannerState(), [], 2000)
  const [result, setResult] = useState<ScanVerification | null>(null)
  const [transaction, setTransaction] = useState<Transaction | null>(null)
  const [manual, setManual] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [txnType, setTxnType] = useState<TransactionType>('Payment')
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const lastScanId = useRef<number | null>(null)

  const workflow = useScanWorkflow(state.data?.workflow, () => {
    void state.reload()
  })

  // The transaction form belongs to the student that was found, so a new scan
  // clears it. The workflow's own scan_id is the authority, not a local ref.
  const workflowScanId = state.data?.workflow?.scan_id ?? null
  useEffect(() => {
    if (workflowScanId !== lastScanId.current) {
      lastScanId.current = workflowScanId
      setAmount('')
      setNote('')
      setTransaction(null)
    }
  }, [workflowScanId])

  // Poll the last scan so the panel has its per-scan details. The *state* comes
  // from the workflow, not from here.
  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const current = await scans.current()
        if (cancelled) return
        if (current && current.scan_id && current.scan_id !== result?.scan_id) {
          setResult(current)
          if (current.result === 'VERIFIED') {
            toast.success(`Verified: ${current.student?.name ?? current.barcode}`)
          } else if (current.result === 'UNKNOWN_ID') {
            toast.warning('Unknown student ID', current.barcode)
          } else if (current.result === 'DUPLICATE_SCAN') {
            toast.warning(current.message)
          }
        }
      } catch {
        /* the banner in the layout already reports an unreachable service */
      }
    }
    void poll()
    const timer = window.setInterval(poll, 900)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [result?.scan_id, toast])

  const submitManual = async (event: React.FormEvent) => {
    event.preventDefault()
    const value = manual.trim()
    if (!value) return
    setSubmitting(true)
    try {
      const response = await scans.submit({
        barcode: value,
        source: 'manual',
        device_name: 'web-manual-entry',
      })
      setResult(response)
      setManual('')
      await state.reload()
      if (response.result === 'VERIFIED') toast.success(`Verified: ${response.student?.name}`)
      else if (response.result === 'DUPLICATE_SCAN') toast.warning(response.message)
      else toast.error(response.result.replace(/_/g, ' '), response.message)
    } catch (cause) {
      toast.error('Manual entry failed', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setSubmitting(false)
    }
  }

  const recordTransaction = async () => {
    if (!workflow.canTransact) return
    setSaving(true)
    try {
      const created = await transactions.create({
        // The workflow's scan_id is the authority: it is the scan whose student
        // is on screen and whose state permits the charge. Falling back to the
        // polled result would risk charging an earlier card.
        scan_id: state.data?.workflow?.scan_id ?? result?.scan_id ?? null,
        transaction_type: txnType,
        amount: amount || '0',
        reference_note: note || null,
      })
      setTransaction(created)
      toast.success('Transaction recorded', created.reference_number)
      setAmount('')
      setNote('')
      await state.reload()
    } catch (cause) {
      // The server has already moved the workflow to TRANSACTION_ERROR or
      // DUPLICATE_SCAN, so a reload shows the state; the toast explains why.
      toast.error(
        'Could not record the transaction',
        cause instanceof ApiError ? cause.message : undefined,
      )
      await state.reload()
    } finally {
      setSaving(false)
    }
  }

  /** Actions the page handles itself rather than posting as a transition. */
  const handleAction = useCallback(
    (action: ScanActionName) => {
      if (action === 'VIEW_HISTORY') {
        navigate('/scans')
        return
      }
      if (action === 'MANUAL_SEARCH') {
        document.getElementById('manual')?.focus()
        return
      }
      if (action === 'SUBMIT_TRANSACTION') {
        void recordTransaction()
        return
      }
      if (action === 'STOP_CAMERA' || action === 'START_CAMERA') {
        void system
          .workflowAction(action)
          .then(() => state.reload())
          .catch((cause: unknown) =>
            toast.error(
              'Camera',
              cause instanceof ApiError ? cause.message : 'Unexpected error',
            ),
          )
        return
      }
      void workflow.act(action).then(
        () => state.reload(),
        (cause: unknown) =>
          toast.error(
            'Scanner',
            cause instanceof ApiError ? cause.message : 'Unexpected error',
          ),
      )
    },
    // `recordTransaction` closes over the current amount/type, so it is
    // intentionally rebuilt each render rather than memoised.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [navigate, state.reload, toast, workflow.act],
  )

  const showTransactionForm = can('transaction:create')
  const student = result?.student ?? state.data?.workflow?.student ?? null

  return (
    <>
      <ErrorBanner error={state.error} onRetry={state.reload} />

      <div className="grid scanner">
        <div className="stack">
          <CameraView state={state.data} onToggle={() => handleAction(
            state.data?.camera_connected ? 'STOP_CAMERA' : 'START_CAMERA',
          )} busy={workflow.acting} />

          <Card
            title="Manual entry"
            subtitle="For a damaged card, or when the camera cannot read the barcode"
          >
            <form onSubmit={submitManual} className="flex-row">
              <div className="field" style={{ flex: 1, minWidth: 220 }}>
                <label htmlFor="manual">Student ID or barcode value</label>
                <input
                  id="manual"
                  value={manual}
                  onChange={(event) => setManual(event.target.value)}
                  placeholder="2026-000123"
                  autoComplete="off"
                />
              </div>
              <button
                type="submit"
                className="btn primary"
                disabled={submitting || !manual.trim()}
                style={{ marginBottom: 1 }}
              >
                {submitting ? <span className="spinner" /> : null}
                Look up
              </button>
            </form>
            <p className="small dim" style={{ marginTop: 10, marginBottom: 0 }}>
              A manual lookup walks the same states as a camera scan and is recorded in the scan
              log exactly like one. The duplicate cooldown applies to it too.
            </p>
          </Card>
        </div>

        <div className="stack">
          <ScanResultPanel
            workflow={workflow}
            result={result}
            transaction={transaction}
            connected={state.data?.camera_connected ?? false}
            onAction={handleAction}
          >
            {result?.student ? (
              <dl className="detail-list">
                <dt>Scanned at</dt>
                <dd>
                  {formatTime(result.scan_time)}{' '}
                  <span className="dim small">({formatRelative(result.scan_time)})</span>
                </dd>
              </dl>
            ) : null}
          </ScanResultPanel>

          {showTransactionForm ? (
            <Card title="Record a transaction" subtitle="Charged against this verified scan">
              <div className="stack">
                {student ? (
                  <div className="flex-row">
                    <Chip tone={result?.can_transact ? 'success' : 'danger'}>
                      {result?.can_transact ? 'can transact' : 'cannot transact'}
                    </Chip>
                    <span className="small muted">
                      {student.name} · {student.student_id}
                    </span>
                  </div>
                ) : (
                  <p className="small muted" style={{ margin: 0 }}>
                    Scan a valid ID first. A transaction can only be attached to a VERIFIED scan.
                  </p>
                )}

                {!workflow.canTransact ? (
                  <div className="banner info">
                    {workflow.state === 'SUCCESS'
                      ? 'This transaction is already recorded. Acknowledge it to scan the next student.'
                      : 'The cashier must press Proceed before a transaction can be submitted.'}
                  </div>
                ) : null}

                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="txn_type">Type</label>
                    <select
                      id="txn_type"
                      value={txnType}
                      onChange={(event) => setTxnType(event.target.value as TransactionType)}
                      disabled={!workflow.canTransact}
                    >
                      {TRANSACTION_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="amount">Amount (₱)</label>
                    <input
                      id="amount"
                      type="number"
                      min="0"
                      step="0.01"
                      value={amount}
                      onChange={(event) => setAmount(event.target.value)}
                      disabled={txnType !== 'Payment' || !workflow.canTransact}
                      placeholder="0.00"
                    />
                  </div>
                </div>

                <div className="field">
                  <label htmlFor="note">Reference note</label>
                  <input
                    id="note"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    maxLength={500}
                    disabled={!workflow.canTransact}
                    placeholder="Canteen purchase, lab fee, library entry…"
                  />
                  <span className="help">
                    Purpose of the transaction. Do not type card numbers or any other sensitive
                    data.
                  </span>
                </div>

                <div className="flex-row">
                  <button
                    type="button"
                    className="btn success"
                    disabled={!workflow.canTransact || saving || workflow.busy}
                    onClick={recordTransaction}
                  >
                    {saving || workflow.busy ? <span className="spinner" /> : null}
                    Confirm transaction
                  </button>
                  {txnType === 'Payment' && amount ? (
                    <span className="small muted">Amount: {formatCurrency(Number(amount))}</span>
                  ) : null}
                </div>
              </div>
            </Card>
          ) : null}
        </div>
      </div>
    </>
  )
}
