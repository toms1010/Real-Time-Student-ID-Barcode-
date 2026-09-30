/**
 * The workflow status bar.
 *
 * Shows the message the current state defines and the actions that state
 * allows - nothing else. The server decides both; this component only renders
 * them, which is why it cannot offer a button the workflow would reject.
 */

import type { ScanWorkflowApi } from '@/hooks/useScanWorkflow'
import type { ScanActionName } from '@/machine/scanState'

/** Label, variant and destination for every action the machine can offer. */
const ACTIONS: Record<
  ScanActionName,
  { label: string; variant: 'primary' | 'success' | 'ghost' | 'default' }
> = {
  START_CAMERA: { label: 'Start camera', variant: 'primary' },
  STOP_CAMERA: { label: 'Stop camera', variant: 'ghost' },
  RETRY_CAMERA: { label: 'Retry camera', variant: 'primary' },
  RESCAN: { label: 'Scan again', variant: 'primary' },
  SCAN_AGAIN: { label: 'Scan another ID', variant: 'primary' },
  MANUAL_SEARCH: { label: 'Manual ID search', variant: 'default' },
  PROCEED: { label: 'Proceed', variant: 'primary' },
  CANCEL: { label: 'Cancel', variant: 'ghost' },
  RETRY_LOOKUP: { label: 'Try again', variant: 'primary' },
  SUBMIT_TRANSACTION: { label: 'Confirm', variant: 'success' },
  RETRY_TRANSACTION: { label: 'Try again', variant: 'primary' },
  ACKNOWLEDGE: { label: 'Done', variant: 'primary' },
  VIEW_HISTORY: { label: 'View history', variant: 'default' },
}

export interface WorkflowBarProps {
  workflow: ScanWorkflowApi
  /** Actions handled by the page rather than posted to the workflow endpoint. */
  onAction?: (action: ScanActionName) => void
}

export function WorkflowBar({ workflow, onAction }: WorkflowBarProps) {
  const { state, message, busy, isError, actions, act, acting, actionError } = workflow

  const run = (action: ScanActionName) => {
    // Local-only actions are the page's business (a "view history" link, for
    // instance); everything else is a workflow transition.
    if (onAction) onAction(action)
    else void act(action).catch(() => undefined)
  }

  return (
    <section className={`workflow-bar ${isError ? 'is-error' : ''}`} aria-label="Scanner status">
      <div className="workflow-head">
        <span className="workflow-state mono">{state}</span>
        {busy ? <span className="spinner" aria-label="working" /> : null}
      </div>

      <p className="workflow-message">
        {message.split('\n').map((line, index) => (
          <span key={index}>
            {line}
            {index < message.split('\n').length - 1 ? <br /> : null}
          </span>
        ))}
      </p>

      {actions.length > 0 ? (
        <div className="flex-row">
          {actions.map((action) => {
            const meta = ACTIONS[action]
            if (!meta) return null
            return (
              <button
                key={action}
                type="button"
                className={`btn ${meta.variant === 'default' ? '' : meta.variant}`.trim()}
                disabled={acting}
                onClick={() => run(action)}
              >
                {meta.label}
              </button>
            )
          })}
        </div>
      ) : null}

      {workflow.cooldownRemainingMs > 0 ? (
        <p className="small dim" style={{ margin: 0 }}>
          Next scan allowed in about {Math.ceil(workflow.cooldownRemainingMs / 100) / 10}s.
        </p>
      ) : null}

      {actionError ? (
        <div className="banner warning" role="alert">
          {actionError}
        </div>
      ) : null}
    </section>
  )
}
