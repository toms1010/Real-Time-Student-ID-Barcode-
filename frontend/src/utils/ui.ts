/** Small presentational helpers shared by the pages. */

import type { ScanResult, StudentStatus } from '@/types'

export type Tone = 'success' | 'danger' | 'warning' | 'info' | 'neutral'

/** Visual tone for each scan result - the brief's high-contrast states. */
export function resultTone(result: ScanResult | string | null | undefined): Tone {
  switch (result) {
    case 'VERIFIED':
      return 'success'
    case 'UNKNOWN_ID':
      return 'warning'
    case 'DUPLICATE_SCAN':
      return 'info'
    case 'INVALID_BARCODE':
    case 'UNSUPPORTED_FORMAT':
    case 'ERROR':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function resultLabel(result: ScanResult | string | null | undefined): string {
  if (!result) return '—'
  return result.replace(/_/g, ' ')
}

export function statusTone(status: StudentStatus | string | null | undefined): Tone {
  switch (status) {
    case 'active':
      return 'success'
    case 'inactive':
    case 'graduated':
    case 'transferred':
      return 'neutral'
    case 'suspended':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function statusLabel(status: StudentStatus | string | null | undefined): string {
  if (!status) return '—'
  return status.charAt(0).toUpperCase() + status.slice(1)
}

/**
 * Scanner states in which the pipeline is actively working.
 *
 * Kept here as a name-only list for the places that only need a yes/no, but it
 * mirrors `CAMERA_STATES` in `src/machine/scanState.ts`; the legacy names are
 * included so an older service build still reads as "active".
 */
export const ACTIVE_STATES = new Set([
  'CAMERA_INITIALIZING',
  'SCANNING',
  'BARCODE_DETECTED',
  'DECODING',
  'VALIDATING',
  'LOOKING_UP_STUDENT',
  // Legacy names emitted before the workflow state machine (ADR-011).
  'INITIALIZING',
  'CAMERA_READY',
  'VERIFYING',
  'VERIFIED',
  'UNKNOWN_ID',
])

export function isScanning(state: string | null | undefined): boolean {
  return ACTIVE_STATES.has(state ?? '')
}

export function errorTone(code: string | null | undefined): Tone {
  if (!code) return 'neutral'
  if (code.includes('PERMISSION') || code.includes('NOT_FOUND') || code.includes('IN_USE')) {
    return 'danger'
  }
  if (code.includes('DISCONNECTED') || code.includes('UNAVAILABLE') || code.includes('TIMEOUT')) {
    return 'warning'
  }
  return 'info'
}

/** Human label for an error code from `app/utils/errors.py`. */
export const ERROR_MESSAGES: Record<string, string> = {
  CAMERA_NOT_FOUND:
    'Camera unavailable. Please check the webcam connection and run --list-cameras.',
  CAMERA_PERMISSION_DENIED:
    'Camera access denied. Your user needs to be in the "video" group.',
  CAMERA_DISCONNECTED: 'Camera disconnected. Reconnecting automatically…',
  CAMERA_IN_USE: 'The camera is already in use by another scanner process.',
  FRAME_CAPTURE_FAILED: 'Frame capture failed. Please try again.',
  BARCODE_NOT_DETECTED:
    'No barcode detected. Please position the ID inside the scanning area.',
  BARCODE_DECODE_FAILED: 'Invalid barcode. Please try scanning again.',
  INVALID_BARCODE: 'Invalid barcode. Please try scanning again.',
  UNSUPPORTED_FORMAT: 'Unsupported barcode format. Please use a Code 128 ID card.',
  STUDENT_NOT_FOUND: 'Student record not found. Please verify the ID.',
  DUPLICATE_SCAN: 'Duplicate scan ignored by the cooldown period.',
  DATABASE_ERROR: 'Database connection failed. Please contact the administrator.',
  API_UNAVAILABLE: 'The local service is unavailable. Please contact the administrator.',
  FILE_ERROR: 'File operation failed. Please contact the administrator.',
  AUTH_REQUIRED: 'Please sign in to continue.',
  AUTH_EXPIRED: 'Your session has expired. Please sign in again.',
  FORBIDDEN: 'You do not have permission to perform this action.',
  UNKNOWN_ERROR: 'An unexpected error occurred.',
}

export function errorMessage(code: string | null | undefined, fallback?: string): string {
  if (!code) return fallback ?? 'An unexpected error occurred.'
  return ERROR_MESSAGES[code] ?? fallback ?? code.replace(/_/g, ' ').toLowerCase()
}
