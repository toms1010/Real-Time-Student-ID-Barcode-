/**
 * The cashier's view of the scan workflow.
 *
 * The server owns the machine; this hook mirrors it so the UI can render the
 * right message and the right buttons immediately, without waiting for the next
 * poll. Every action still goes through `POST /api/scanner/workflow`, which
 * re-validates it against the same table and answers 409 when it is not
 * allowed - so a stale button can never drive the scanner into a state the
 * workflow does not permit.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError, system } from '@/services/api'
import {
  actionsFor,
  coerceState,
  definitionFor,
  hasStudent,
  isBusy,
  isErrorState,
  type ScanActionName,
  type ScanStateName,
} from '@/machine/scanState'
import type { ScanWorkflow } from '@/types'

export interface ScanWorkflowApi {
  /** The canonical state, after alias coercion. */
  state: ScanStateName
  label: string
  /** The operator-facing message for this state. */
  message: string
  /** True while the workflow is between two points; the UI must disable input. */
  busy: boolean
  isError: boolean
  /** A student record is on screen. */
  hasStudent: boolean
  /** The workflow state permits a charge right now. */
  canTransact: boolean
  /** The actions the cashier may take, or [] when busy. */
  actions: ScanActionName[]
  errorCode: string | null
  errorMessage: string | null
  cooldownRemainingMs: number
  /** Submit an action; rejects on 409 so the caller can explain why. */
  act: (action: ScanActionName) => Promise<void>
  /** True while an action request is in flight. */
  acting: boolean
  /** The last rejection from the server, if any. */
  actionError: string | null
}

/**
 * @param snapshot the `workflow` object embedded in `GET /api/scanner/state`
 * @param onAction called after a successful action so the caller can refetch
 */
export function useScanWorkflow(
  snapshot: ScanWorkflow | null | undefined,
  onAction?: () => void,
): ScanWorkflowApi {
  const state = coerceState(snapshot?.state)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  // Remembers the state the buttons were rendered for, so an action that
  // arrived from a stale click is not sent after the workflow has moved on.
  const renderedState = useRef(state)
  renderedState.current = state

  const act = useCallback(
    async (action: ScanActionName) => {
      setActing(true)
      setActionError(null)
      try {
        await system.workflowAction(action)
        onAction?.()
      } catch (cause) {
        setActionError(
          cause instanceof ApiError ? cause.message : 'The scanner did not accept that action.',
        )
        throw cause
      } finally {
        setActing(false)
      }
    },
    [onAction],
  )

  // Clear a stale rejection once the workflow has genuinely moved on.
  useEffect(() => {
    setActionError(null)
  }, [state])

  const definition = useMemo(() => definitionFor(state), [state])

  return {
    state,
    label: snapshot?.label || definition.label,
    message: snapshot?.message || definition.message,
    busy: snapshot?.busy ?? isBusy(state),
    isError: isErrorState(state),
    hasStudent: hasStudent(state),
    canTransact: snapshot?.can_transact ?? state === 'READY_FOR_TRANSACTION',
    actions: actionsFor(state),
    errorCode: snapshot?.error_code ?? null,
    errorMessage: snapshot?.error_message ?? null,
    cooldownRemainingMs: snapshot?.cooldown_remaining_ms ?? 0,
    act,
    acting,
    actionError,
  }
}
