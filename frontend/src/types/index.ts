/**
 * Wire types shared with the FastAPI service.
 *
 * These mirror `python/app/schemas/*` one to one.  The comment on each block
 * names the Pydantic model it comes from so the two can be diffed by hand when
 * the API changes.
 */

export type ScanResult =
  | 'VERIFIED'
  | 'UNKNOWN_ID'
  | 'INVALID_BARCODE'
  | 'DUPLICATE_SCAN'
  | 'UNSUPPORTED_FORMAT'
  | 'ERROR'

export type StudentStatus =
  | 'active'
  | 'inactive'
  | 'graduated'
  | 'suspended'
  | 'transferred'

export type Role = 'admin' | 'cashier' | 'staff' | 'operator' | 'viewer'

export type TransactionType = 'Payment' | 'Verification' | 'Registration' | 'Other'

export type TransactionStatus = 'pending' | 'completed' | 'cancelled' | 'refunded'

export type ExportFormat = 'csv' | 'xlsx' | 'pdf' | 'json'

/** BarcodeOut */
export interface Barcode {
  id: number
  barcode_value: string
  barcode_type: string
  label: string | null
  is_primary: boolean
  retired_at: string | null
  created_at: string | null
}

/** StudentOut */
export interface Student {
  id: number
  student_id: string
  first_name: string
  middle_name: string | null
  last_name: string
  full_name: string
  course: string | null
  year_level: number | null
  section: string | null
  school: string | null
  email: string | null
  phone: string | null
  photo_path: string | null
  status: StudentStatus
  notes: string | null
  barcodes: Barcode[]
  created_at: string | null
  updated_at: string | null
}

/** StudentSummaryOut */
export interface StudentSummary {
  student_id: string
  name: string
  course: string | null
  year_level: number | null
  section: string | null
  status: string
  photo_path: string | null
  internal_id: number | null
}

/** ScanTiming */
export interface ScanTiming {
  detection_ms: number | null
  decode_ms: number | null
  database_ms: number | null
  processing_ms: number | null
  total_ms: number | null
}

/** ScanVerification */
export interface ScanVerification {
  success: boolean
  result: ScanResult
  message: string
  scan_id: number | null
  barcode: string
  barcode_type: string | null
  confidence: number | null
  student: StudentSummary | null
  can_transact: boolean
  duplicate: boolean
  error_code: string | null
  timing: ScanTiming
  scan_time: string
}

/** ScanOut */
export interface Scan {
  id: number
  student_id: number | null
  student_number: string | null
  student: StudentSummary | null
  barcode_value: string
  scan_time: string
  result: ScanResult
  confidence: number | null
  barcode_type: string | null
  device_name: string | null
  source: string
  processing_time_ms: number | null
  detection_time_ms: number | null
  database_time_ms: number | null
  error_code: string | null
  error_message: string | null
  operator_id: number | null
}

/** TransactionOut */
export interface Transaction {
  id: number
  reference_number: string
  student_id: number | null
  student_number: string | null
  student: StudentSummary | null
  scan_id: number | null
  transaction_type: TransactionType
  amount: number
  quantity: number
  cashier_id: number | null
  cashier_username: string | null
  status: TransactionStatus
  reference_note: string | null
  created_at: string | null
  updated_at: string | null
}

/** UserOut */
export interface User {
  id: number
  username: string
  full_name: string | null
  role: Role
  is_active: boolean
  must_change_password: boolean
  last_login_at: string | null
  created_at: string | null
}

/** LoginResponse */
export interface LoginResponse {
  success: boolean
  token: string
  expires_at: string
  user: User
  permissions: string[]
}

/** DailyRow */
export interface DailyRow {
  scan_date: string
  total_scans: number
  verified: number
  unknown_id: number
  invalid_barcode: number
  duplicate_scan: number
  unsupported_format: number
  errors: number
  avg_processing_ms: number | null
  max_processing_ms: number | null
  avg_confidence: number | null
}

/** DailyReport */
export interface DailyReport {
  period: string
  label: string
  start: string
  end: string
  timezone: string
  totals: Record<string, number | null>
  days: DailyRow[]
  top_students: Array<{
    student_number: string
    full_name: string
    course: string | null
    total_scans: number
    last_scan_time: string
  }>
  unknown_barcodes: Array<{
    barcode_value: string
    attempts: number
    first_seen: string
    last_seen: string
  }>
  hourly_profile: Array<{ hour: string; scans: number; verified: number }>
  generated_at: string
}

/** DashboardSummary */
export interface DashboardSummary {
  students_total: number
  students_active: number
  students_inactive: number
  scans_total: number
  scans_today: number
  verified_today: number
  errors_today: number
  unknown_today: number
  duplicates_today: number
  transactions_today: number
  revenue_today: number
  avg_processing_ms: number | null
  last_scan: {
    id: number
    student_id: string | null
    name: string | null
    barcode_value: string
    result: ScanResult
    scan_time: string
  } | null
  trend_7d: DailyRow[]
  hourly_today: Array<{ hour: string; scans: number; verified: number }>
  generated_at: string
}

/** PerformanceReport */
export interface PerformanceReport {
  period: string
  label: string
  start: string
  end: string
  frames_observed: number
  scans: number
  verified: number
  failed: number
  avg_processing_ms: number | null
  min_processing_ms: number | null
  max_processing_ms: number | null
  avg_detection_ms: number | null
  avg_database_ms: number | null
  p95_processing_ms: number | null
  avg_confidence: number | null
  slowest: Array<Record<string, string | number | null>>
  by_device: Array<{
    device_name: string
    scans: number
    verified: number
    avg_processing_ms: number | null
    avg_confidence: number | null
  }>
  generated_at: string
}

/** SettingsOut */
export interface Setting {
  key: string
  value: string | null
  value_type: string
  category: string
  description: string | null
  updated_at: string | null
}

/**
 * The scan workflow, as served by `GET /api/scanner/workflow` and embedded
 * under `workflow` in `GET /api/scanner/state`.
 *
 * Mirrors `app/services/scan_workflow.py`. See `src/machine/scanState.ts` for
 * the client-side copy of the transition table.
 */
export interface ScanWorkflow {
  /** A canonical state name; legacy names are accepted and coerced. */
  state: string
  label: string
  /** The operator-facing text for this state. */
  message: string
  /** The same text without any error override, for the "what does this mean" copy. */
  default_message: string
  phase: 'idle' | 'acquiring' | 'detecting' | 'processing' | 'resolved' | 'actionable' | 'done' | 'error'
  /** True while the workflow is mid-flight; the UI must disable input. */
  busy: boolean
  /** The actions the cashier may take right now; empty while busy. */
  actions: string[]
  next_states: string[]
  can_transact: boolean
  student: StudentSummary | null
  scan_id: number | null
  barcode: string | null
  result: ScanResult | null
  transaction: Transaction | null
  error_code: string | null
  error_message: string | null
  /** Milliseconds left on the duplicate-scan cooldown, when suppressed. */
  cooldown_remaining_ms: number
  last_reason: string
  state_visits: Record<string, number>
  rejected_transitions: number
  history: Array<{ from: string; to: string; reason: string; at: number }>
  updated_at: string | null
}

/** One row of the state table, so the UI never hard-codes a transition rule. */
export interface ScanStateDefinition {
  state: string
  label: string
  phase: ScanWorkflow['phase']
  purpose: string
  message: string
  entry: string
  exit: string
  error_handling: string
  busy: boolean
  actions: string[]
  next_states: string[]
}

/** `GET /api/scanner/state` (pipeline stats + the embedded workflow). */
export interface ScannerState {
  state: string
  workflow?: ScanWorkflow
  owner?: string
  camera_connected: boolean
  camera_device: string | null
  stream_source: string
  fps: number
  frame_width: number | null
  frame_height: number | null
  frames_processed: number
  avg_processing_ms: number | null
  avg_detection_ms: number | null
  avg_database_ms: number | null
  last_barcode: string | null
  last_result: string | null
  cooldowns_active: number
  error_code: string | null
  error_message: string | null
  updated_at: string | null
  detections?: Array<{
    text: string
    format: string | null
    valid: boolean
    points: number[][]
  }>
}

export interface ApiErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
}

export interface Paged<T> {
  items: T[]
  page: {
    total: number
    limit: number
    offset: number
    has_more: boolean
  }
}

export interface ExportDescriptor {
  filename: string
  format: ExportFormat
  rows: number
  size_bytes: number
  download_url: string
}

export interface AuditRow {
  id: number
  user_id: number | null
  username: string | null
  action: string
  entity_type: string | null
  entity_id: string | null
  details: string | null
  ip_address: string | null
  created_at: string
}

export interface PublicSettings {
  application_name: string
  theme: 'dark' | 'light' | 'system'
  require_login: boolean
  version: string
}
