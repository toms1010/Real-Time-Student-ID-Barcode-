/**
 * The scan result panel: the screen the cashier actually looks at.
 *
 * The heading and the tone come from the **workflow state**, not from a second
 * guess based on the scan result. That is the whole point of the state machine:
 * there is one answer to "what is happening", and it is the same answer the
 * OpenCV pipeline, the API and this component give.
 *
 * The `result` still supplies the details (student, timings, barcode) because
 * those are per-scan data, not per-state data.
 */

import { WorkflowBar, type WorkflowBarProps } from '@/components/WorkflowBar'
import type { ScanWorkflowApi } from '@/hooks/useScanWorkflow'
import { definitionFor, isErrorState, type ScanStateName } from '@/machine/scanState'
import { formatCurrency, formatMs, formatRelative, formatTime } from '@/utils/format'
import { resultTone, statusTone } from '@/utils/ui'
import type { ScanVerification, StudentSummary, Transaction } from '@/types'

/** The single badge for each state, keyed by the workflow state name. */
const HEADINGS: Record<ScanStateName, { icon: string; text: string; tone: string }> = {
  IDLE: { icon: '🟡', text: 'Ready to Scan', tone: 'idle' },
  CAMERA_INITIALIZING: { icon: '⏳', text: 'Starting camera…', tone: 'scanning' },
  SCANNING: { icon: '🔍', text: 'Scanning…', tone: 'scanning' },
  BARCODE_DETECTED: { icon: '🔍', text: 'Barcode detected…', tone: 'scanning' },
  DECODING: { icon: '⏳', text: 'Decoding…', tone: 'scanning' },
  VALIDATING: { icon: '⏳', text: 'Validating…', tone: 'scanning' },
  LOOKING_UP_STUDENT: { icon: '⏳', text: 'Checking record…', tone: 'scanning' },
  STUDENT_FOUND: { icon: '✓', text: 'Student Found', tone: 'success' },
  READY_FOR_TRANSACTION: { icon: '✓', text: 'Student Found', tone: 'success' },
  TRANSACTION_PROCESSING: { icon: '⏳', text: 'Processing…', tone: 'scanning' },
  SUCCESS: { icon: '✓', text: 'Transaction Successful', tone: 'success' },
  CAMERA_ERROR: { icon: '📷', text: 'Camera unavailable', tone: 'error' },
  BARCODE_INVALID: { icon: '⚠', text: 'Invalid barcode', tone: 'error' },
  STUDENT_NOT_FOUND: { icon: '⚠', text: 'Not found', tone: 'error' },
  DATABASE_ERROR: { icon: '⚠', text: 'Records unavailable', tone: 'error' },
  TRANSACTION_ERROR: { icon: '✕', text: 'Transaction failed', tone: 'error' },
  DUPLICATE_SCAN: { icon: '↺', text: 'Already scanned', tone: 'error' },
}

/** States that are a step on the way to a student, not a resting place. */
const TRANSIENT: ScanStateName[] = [
  'CAMERA_INITIALIZING',
  'BARCODE_DETECTED',
  'DECODING',
  'VALIDATING',
  'LOOKING_UP_STUDENT',
  'TRANSACTION_PROCESSING',
]

function StudentDetails({ student }: { student: StudentSummary }) {
  return (
    <>
      <div className="student-headline">
        {student.photo_path ? (
          <img
            className="photo"
            src={`/api/students/${encodeURIComponent(student.student_id)}/photo`}
            alt={`Photo of ${student.name}`}
            onError={(event) => {
              // A missing or corrupt photo must not break the verification panel.
              event.currentTarget.style.display = 'none'
            }}
          />
        ) : (
          <div className="photo-placeholder">no photo</div>
        )}
        <div>
          <div className="student-name">{student.name}</div>
          <div className="student-id">{student.student_id}</div>
          {student.course ? (
            <div className="muted small" style={{ marginTop: 4 }}>
              {student.course}
            </div>
          ) : null}
        </div>
      </div>
      <dl className="detail-list">
        <dt>Year</dt>
        <dd>{student.year_level ?? '—'}</dd>
        <dt>Section</dt>
        <dd>{student.section ?? '—'}</dd>
        <dt>Status</dt>
        <dd>
          <span className={`chip ${statusTone(student.status)}`}>
            <span className="dot" />
            {student.status.toUpperCase()}
          </span>
        </dd>
      </dl>
    </>
  )
}

/** The confirmation block for SUCCESS - only ever shown for a saved transaction. */
function TransactionSummary({
  transaction,
  student,
}: {
  transaction: Transaction
  student: StudentSummary | null
}) {
  return (
    <dl className="detail-list">
      {student ? (
        <>
          <dt>Student</dt>
          <dd>{student.name}</dd>
        </>
      ) : null}
      <dt>Transaction</dt>
      <dd>{transaction.transaction_type}</dd>
      <dt>Amount</dt>
      <dd>{formatCurrency(transaction.amount)}</dd>
      <dt>Reference</dt>
      <dd className="mono">{transaction.reference_number}</dd>
    </dl>
  )
}

export function ScanResultPanel({
  workflow,
  result,
  transaction,
  connected,
  onAction,
  children,
}: {
  workflow: ScanWorkflowApi
  result: ScanVerification | null
  transaction?: Transaction | null
  connected: boolean
  onAction?: WorkflowBarProps['onAction']
  children?: React.ReactNode
}) {
  const state = workflow.state
  const heading = HEADINGS[state] ?? HEADINGS.IDLE
  const student = result?.student ?? null
  const transient = TRANSIENT.includes(state)
  const isError = isErrorState(state)

  return (
    <div className="result-panel">
      <div className={`result-head ${heading.tone}`}>
        <span aria-hidden="true">{heading.icon}</span>
        <span>{heading.text}</span>
        {transient ? <span className="spinner" style={{ marginLeft: 'auto' }} /> : null}
      </div>

      <div className="result-body">
        <WorkflowBar workflow={workflow} onAction={onAction} />

        {isError && workflow.errorMessage ? (
          <div className="banner danger" role="alert">
            {workflow.errorMessage}
          </div>
        ) : null}

        {state === 'SUCCESS' && transaction ? (
          <TransactionSummary transaction={transaction} student={student} />
        ) : null}

        {student && state !== 'SUCCESS' ? <StudentDetails student={student} /> : null}

        {result && !student && !isError ? (
          <p style={{ margin: 0 }}>{result.message}</p>
        ) : null}

        {result?.student && !result.can_transact ? (
          <div className="banner warning">
            This card is valid but the student cannot transact
            {result.student.status ? ` (status: ${result.student.status})` : ''}. Please refer to
            the registrar's office.
          </div>
        ) : null}

        {result ? (
          <>
            <dl className="detail-list">
              <dt>Barcode</dt>
              <dd className="mono">{result.barcode}</dd>
              {result.barcode_type ? (
                <>
                  <dt>Symbology</dt>
                  <dd>{result.barcode_type}</dd>
                </>
              ) : null}
              <dt>Scan time</dt>
              <dd>
                {formatTime(result.scan_time)}{' '}
                <span className="dim small">({formatRelative(result.scan_time)})</span>
              </dd>
              <dt>Result</dt>
              <dd>
                <span className={`chip ${resultTone(result.result)}`}>
                  <span className="dot" />
                  {result.result.replace(/_/g, ' ')}
                </span>
              </dd>
            </dl>

            <div className="metrics-row">
              <span>detect {formatMs(result.timing.detection_ms)}</span>
              <span>db {formatMs(result.timing.database_ms)}</span>
              <span>total {formatMs(result.timing.total_ms)}</span>
              {result.timing.processing_ms ? (
                <span>frame {formatMs(result.timing.processing_ms)}</span>
              ) : null}
            </div>
          </>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            {connected
              ? definitionFor(state).message.replace(/\n+/g, ' ')
              : 'The camera is not running. Start it from the toolbar, or scan a number manually below.'}
          </p>
        )}

        {children}
      </div>
    </div>
  )
}
