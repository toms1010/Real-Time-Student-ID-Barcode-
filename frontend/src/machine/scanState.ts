/**
 * The scan state machine, mirrored from the backend.
 *
 * `python/app/services/scan_workflow.py` is the single source of truth. This
 * file exists so the cashier screen can decide *locally* which buttons to show
 * and which state messages to render, without waiting for a poll - the server
 * still validates every action, and `POST /api/scanner/workflow` rejects
 * anything the table forbids with a 409.
 *
 * Keep the two in step. `python/tests/test_scan_workflow.py` checks the Python
 * table; the shared invariants below are asserted by
 * `frontend/src/machine/scanState.test.ts` (vitest) against the same values.
 */

export const ScanState = {
  IDLE: 'IDLE',
  CAMERA_INITIALIZING: 'CAMERA_INITIALIZING',
  SCANNING: 'SCANNING',
  BARCODE_DETECTED: 'BARCODE_DETECTED',
  DECODING: 'DECODING',
  VALIDATING: 'VALIDATING',
  LOOKING_UP_STUDENT: 'LOOKING_UP_STUDENT',
  STUDENT_FOUND: 'STUDENT_FOUND',
  READY_FOR_TRANSACTION: 'READY_FOR_TRANSACTION',
  TRANSACTION_PROCESSING: 'TRANSACTION_PROCESSING',
  SUCCESS: 'SUCCESS',
  CAMERA_ERROR: 'CAMERA_ERROR',
  BARCODE_INVALID: 'BARCODE_INVALID',
  STUDENT_NOT_FOUND: 'STUDENT_NOT_FOUND',
  DATABASE_ERROR: 'DATABASE_ERROR',
  TRANSACTION_ERROR: 'TRANSACTION_ERROR',
  DUPLICATE_SCAN: 'DUPLICATE_SCAN',
} as const

export type ScanStateName = (typeof ScanState)[keyof typeof ScanState]

export const ScanAction = {
  START_CAMERA: 'START_CAMERA',
  STOP_CAMERA: 'STOP_CAMERA',
  RETRY_CAMERA: 'RETRY_CAMERA',
  RESCAN: 'RESCAN',
  SCAN_AGAIN: 'SCAN_AGAIN',
  MANUAL_SEARCH: 'MANUAL_SEARCH',
  PROCEED: 'PROCEED',
  CANCEL: 'CANCEL',
  RETRY_LOOKUP: 'RETRY_LOOKUP',
  SUBMIT_TRANSACTION: 'SUBMIT_TRANSACTION',
  RETRY_TRANSACTION: 'RETRY_TRANSACTION',
  ACKNOWLEDGE: 'ACKNOWLEDGE',
  VIEW_HISTORY: 'VIEW_HISTORY',
} as const

export type ScanActionName = (typeof ScanAction)[keyof typeof ScanAction]

export type ScanPhase =
  | 'idle'
  | 'acquiring'
  | 'detecting'
  | 'processing'
  | 'resolved'
  | 'actionable'
  | 'done'
  | 'error'

export interface ScanDefinition {
  state: ScanStateName
  label: string
  phase: ScanPhase
  message: string
  busy: boolean
  actions: ScanActionName[]
  nextStates: ScanStateName[]
}

/**
 * The transition table. Must match `TRANSITIONS` in
 * `python/app/services/scan_workflow.py`, including the two "direct
 * submission" edges: a payload typed by hand never passed a frame, so it enters
 * at VALIDATING rather than pretending to have been detected and decoded.
 */
export const TRANSITIONS: Record<ScanStateName, ScanStateName[]> = {
  IDLE: ['CAMERA_INITIALIZING', 'VALIDATING'],
  CAMERA_INITIALIZING: ['SCANNING', 'CAMERA_ERROR'],
  SCANNING: ['BARCODE_DETECTED', 'CAMERA_ERROR', 'VALIDATING', 'IDLE'],
  BARCODE_DETECTED: ['DECODING', 'BARCODE_INVALID', 'SCANNING'],
  DECODING: ['VALIDATING', 'BARCODE_INVALID', 'DUPLICATE_SCAN'],
  VALIDATING: ['LOOKING_UP_STUDENT', 'BARCODE_INVALID'],
  LOOKING_UP_STUDENT: [
    'STUDENT_FOUND',
    'STUDENT_NOT_FOUND',
    'DATABASE_ERROR',
    'BARCODE_INVALID',
    'DUPLICATE_SCAN',
  ],
  STUDENT_FOUND: ['READY_FOR_TRANSACTION', 'SCANNING', 'IDLE'],
  READY_FOR_TRANSACTION: [
    'TRANSACTION_PROCESSING',
    'SCANNING',
    'IDLE',
    'TRANSACTION_ERROR',
  ],
  TRANSACTION_PROCESSING: ['SUCCESS', 'TRANSACTION_ERROR', 'DUPLICATE_SCAN'],
  SUCCESS: ['IDLE', 'SCANNING'],
  CAMERA_ERROR: ['CAMERA_INITIALIZING', 'VALIDATING', 'IDLE'],
  BARCODE_INVALID: ['SCANNING', 'IDLE'],
  STUDENT_NOT_FOUND: ['SCANNING', 'IDLE'],
  DATABASE_ERROR: ['LOOKING_UP_STUDENT', 'IDLE'],
  TRANSACTION_ERROR: ['READY_FOR_TRANSACTION', 'IDLE'],
  DUPLICATE_SCAN: ['SCANNING', 'IDLE'],
}

/** The message and the actions for each state. */
export const DEFINITIONS: Record<ScanStateName, Omit<ScanDefinition, 'nextStates'>> = {
  IDLE: {
    state: 'IDLE',
    label: 'Ready to scan',
    phase: 'idle',
    busy: false,
    message: 'Ready to Scan\n\nPlease position the student ID inside the scanning area.',
    actions: ['START_CAMERA', 'SCAN_AGAIN', 'MANUAL_SEARCH'],
  },
  CAMERA_INITIALIZING: {
    state: 'CAMERA_INITIALIZING',
    label: 'Starting camera',
    phase: 'acquiring',
    busy: true,
    message: 'Starting camera...',
    actions: [],
  },
  SCANNING: {
    state: 'SCANNING',
    label: 'Scanning',
    phase: 'detecting',
    busy: false,
    message: 'Scanning...\nPosition the ID inside the box.',
    actions: ['STOP_CAMERA', 'SCAN_AGAIN'],
  },
  BARCODE_DETECTED: {
    state: 'BARCODE_DETECTED',
    label: 'Barcode detected',
    phase: 'detecting',
    busy: true,
    message: 'Barcode detected...',
    actions: [],
  },
  DECODING: {
    state: 'DECODING',
    label: 'Decoding',
    phase: 'processing',
    busy: true,
    message: 'Decoding barcode...',
    actions: [],
  },
  VALIDATING: {
    state: 'VALIDATING',
    label: 'Validating',
    phase: 'processing',
    busy: true,
    message: 'Validating barcode...',
    actions: [],
  },
  LOOKING_UP_STUDENT: {
    state: 'LOOKING_UP_STUDENT',
    label: 'Looking up student',
    phase: 'processing',
    busy: true,
    message: 'Checking student record...',
    actions: [],
  },
  STUDENT_FOUND: {
    state: 'STUDENT_FOUND',
    label: 'Student found',
    phase: 'resolved',
    busy: false,
    message: 'Student found.',
    actions: ['PROCEED', 'CANCEL', 'SCAN_AGAIN'],
  },
  READY_FOR_TRANSACTION: {
    state: 'READY_FOR_TRANSACTION',
    label: 'Ready',
    phase: 'actionable',
    busy: false,
    message: 'Ready to process the transaction.',
    actions: ['PROCEED', 'CANCEL', 'SCAN_AGAIN', 'SUBMIT_TRANSACTION'],
  },
  TRANSACTION_PROCESSING: {
    state: 'TRANSACTION_PROCESSING',
    label: 'Processing',
    phase: 'processing',
    busy: true,
    message: 'Processing transaction...\nPlease wait.',
    actions: [],
  },
  SUCCESS: {
    state: 'SUCCESS',
    label: 'Success',
    phase: 'done',
    busy: false,
    message: 'Transaction successful.',
    actions: ['ACKNOWLEDGE', 'SCAN_AGAIN'],
  },
  CAMERA_ERROR: {
    state: 'CAMERA_ERROR',
    label: 'Camera error',
    phase: 'error',
    busy: false,
    message: 'Camera unavailable.\n\nPlease check the webcam connection.',
    actions: ['RETRY_CAMERA', 'MANUAL_SEARCH', 'CANCEL'],
  },
  BARCODE_INVALID: {
    state: 'BARCODE_INVALID',
    label: 'Invalid barcode',
    phase: 'error',
    busy: false,
    message: 'Invalid barcode.\n\nPlease position the ID correctly and try again.',
    actions: ['RESCAN', 'CANCEL', 'MANUAL_SEARCH'],
  },
  STUDENT_NOT_FOUND: {
    state: 'STUDENT_NOT_FOUND',
    label: 'Not found',
    phase: 'error',
    busy: false,
    message: 'Student record not found.\n\nPlease verify the ID and try again.',
    actions: ['SCAN_AGAIN', 'MANUAL_SEARCH', 'CANCEL'],
  },
  DATABASE_ERROR: {
    state: 'DATABASE_ERROR',
    label: 'Database error',
    phase: 'error',
    busy: false,
    message:
      'Unable to access student records.\n\nPlease try again or contact the administrator.',
    actions: ['RETRY_LOOKUP', 'CANCEL'],
  },
  TRANSACTION_ERROR: {
    state: 'TRANSACTION_ERROR',
    label: 'Transaction failed',
    phase: 'error',
    busy: false,
    message: 'Transaction failed.\n\nNo transaction was recorded.\nPlease try again.',
    actions: ['RETRY_TRANSACTION', 'CANCEL'],
  },
  DUPLICATE_SCAN: {
    state: 'DUPLICATE_SCAN',
    label: 'Duplicate',
    phase: 'error',
    busy: false,
    message: 'This ID was already scanned.\n\nPlease wait or verify the transaction history.',
    actions: ['SCAN_AGAIN', 'VIEW_HISTORY', 'CANCEL'],
  },
}

/** States in which the camera is expected to be delivering frames. */
export const CAMERA_STATES: ScanStateName[] = [
  'CAMERA_INITIALIZING',
  'SCANNING',
  'BARCODE_DETECTED',
  'DECODING',
  'VALIDATING',
  'LOOKING_UP_STUDENT',
]

/** States in which a student record is on screen. */
export const STUDENT_STATES: ScanStateName[] = [
  'STUDENT_FOUND',
  'READY_FOR_TRANSACTION',
  'TRANSACTION_PROCESSING',
  'SUCCESS',
  'DUPLICATE_SCAN',
]

export const ALL_STATES = Object.keys(DEFINITIONS) as ScanStateName[]

export function isScanState(value: string | null | undefined): value is ScanStateName {
  return !!value && value in DEFINITIONS
}

/** Whether `next` may follow `from`. A self-transition is always allowed. */
export function isAllowed(from: ScanStateName, to: ScanStateName): boolean {
  if (from === to) return true
  return (TRANSITIONS[from] ?? []).includes(to)
}

/** The states the UI may offer, or [] when the workflow is busy. */
export function actionsFor(state: ScanStateName): ScanActionName[] {
  const definition = DEFINITIONS[state]
  if (!definition) return []
  return definition.busy ? [] : definition.actions
}

export function definitionFor(state: ScanStateName) {
  return DEFINITIONS[state] ?? DEFINITIONS.IDLE
}

export function phaseFor(state: ScanStateName): ScanPhase {
  return definitionFor(state).phase
}

export function isErrorState(state: ScanStateName): boolean {
  return phaseFor(state) === 'error'
}

export function isBusy(state: ScanStateName): boolean {
  return definitionFor(state).busy
}

/** The camera is expected to be live (used to decide whether to show the feed). */
export function expectsCamera(state: ScanStateName): boolean {
  return CAMERA_STATES.includes(state)
}

/** A student record is on screen and may be charged. */
export function hasStudent(state: ScanStateName): boolean {
  return STUDENT_STATES.includes(state)
}

/**
 * Older scanner builds reported a shorter set of names. Accepting them means a
 * stale `dist/` bundle or an old C++ binary does not look like a bug.
 */
export const LEGACY_STATE_ALIASES: Record<string, ScanStateName> = {
  INITIALIZING: 'CAMERA_INITIALIZING',
  CAMERA_READY: 'SCANNING',
  VERIFYING: 'LOOKING_UP_STUDENT',
  VERIFIED: 'STUDENT_FOUND',
  UNKNOWN_ID: 'STUDENT_NOT_FOUND',
  ERROR: 'DATABASE_ERROR',
  CAMERA_DISCONNECTED: 'CAMERA_ERROR',
  STOPPED: 'IDLE',
  INVALID_BARCODE: 'BARCODE_INVALID',
  TRANSACTION_COMPLETED: 'SUCCESS',
}

export function coerceState(value: string | null | undefined): ScanStateName {
  if (!value) return 'IDLE'
  const text = value.trim().toUpperCase()
  if (isScanState(text)) return text
  return LEGACY_STATE_ALIASES[text] ?? 'IDLE'
}
