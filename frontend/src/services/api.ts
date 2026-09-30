/**
 * Thin fetch wrapper around the local API.
 *
 * Design notes
 * ------------
 * * The session token lives in an HttpOnly cookie, so there is no token to
 *   attach here and no way for JavaScript to read it.  `credentials:
 *   'include'` is what makes the cookie work.
 * * Every non-2xx response is turned into an `ApiError` carrying the service's
 *   stable `code` (e.g. STUDENT_NOT_FOUND), which is what the UI matches on -
 *   never the HTTP status alone.
 * * `ApiError.message` is the operator-facing text from the service, so screens
 *   can show the exact wording the backend intends.
 */

import type {
  ApiErrorBody,
  AuditRow,
  Barcode,
  DailyReport,
  DashboardSummary,
  ExportDescriptor,
  ExportFormat,
  LoginResponse,
  Paged,
  PerformanceReport,
  PublicSettings,
  Scan,
  ScanResult,
  ScanStateDefinition,
  ScanVerification,
  ScanWorkflow,
  ScannerState,
  Setting,
  Student,
  StudentStatus,
  Transaction,
  TransactionStatus,
  TransactionType,
  User,
} from '@/types'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || 'Request failed')
    this.name = 'ApiError'
    this.status = status
    this.code = body.code || 'UNKNOWN_ERROR'
    this.details = body.details ?? {}
  }

  /** True when the session expired and the user has to sign in again. */
  get isAuthError(): boolean {
    return this.status === 401
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

function buildUrl(path: string, query?: Query): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    params.append(key, String(value))
  }
  const queryString = params.toString()
  return queryString ? `${path}?${queryString}` : path
}

async function request<T>(
  path: string,
  options: RequestInit & { query?: Query } = {},
): Promise<T> {
  const { query, headers, ...rest } = options
  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      credentials: 'include',
      ...rest,
      headers: {
        Accept: 'application/json',
        ...(rest.body ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
    })
  } catch (cause) {
    // The service is not running: this is the single most common offline
    // failure, so it gets a message the operator can act on.
    throw new ApiError(0, {
      code: 'API_UNAVAILABLE',
      message:
        'Cannot reach the local service. Start it with ./scripts/start.sh, then try again.',
    })
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    const body = (payload as { error?: ApiErrorBody } | null)?.error
    throw new ApiError(
      response.status,
      body ?? {
        code: 'UNKNOWN_ERROR',
        message: `Request failed with HTTP ${response.status}`,
      },
    )
  }
  return payload as T
}

const get = <T,>(path: string, query?: Query) => request<T>(path, { method: 'GET', query })
const post = <T,>(path: string, body?: unknown, query?: Query) =>
  request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
    query,
  })
const put = <T,>(path: string, body?: unknown, query?: Query) =>
  request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body), query })
const del = <T,>(path: string, query?: Query) => request<T>(path, { method: 'DELETE', query })

/* ------------------------------------------------------------------ auth */
export const auth = {
  login: (username: string, password: string) =>
    post<LoginResponse>('/api/auth/login', { username, password }),
  logout: () => post<{ success: boolean; message: string }>('/api/auth/logout'),
  me: () => get<User>('/api/auth/me'),
  permissions: () => get<Record<string, string[]>>('/api/auth/permissions'),
  changePassword: (current_password: string, new_password: string) =>
    post<{ success: boolean; message: string }>('/api/auth/change-password', {
      current_password,
      new_password,
    }),
  listUsers: () => get<Paged<User>>('/api/auth/users'),
  createUser: (body: {
    username: string
    password: string
    full_name?: string | null
    role?: string
    must_change_password?: boolean
  }) => post<User>('/api/auth/users', body),
  updateUser: (id: number, body: Record<string, unknown>) => put<User>(`/api/auth/users/${id}`, body),
}

/* --------------------------------------------------------------- students */
export interface StudentPayload {
  student_id?: string
  first_name?: string
  last_name?: string
  middle_name?: string | null
  course?: string | null
  year_level?: number | null
  section?: string | null
  school?: string | null
  email?: string | null
  phone?: string | null
  status?: StudentStatus
  notes?: string | null
}

export const students = {
  list: (query: Query) => get<Paged<Student>>('/api/students', query),
  courses: () => get<string[]>('/api/students/courses'),
  get: (studentId: string) => get<Student>(`/api/students/${encodeURIComponent(studentId)}`),
  create: (body: StudentPayload) => post<Student>('/api/students', body),
  update: (studentId: string, body: StudentPayload) =>
    put<Student>(`/api/students/${encodeURIComponent(studentId)}`, body),
  deactivate: (studentId: string) =>
    del<Student>(`/api/students/${encodeURIComponent(studentId)}`),
  reactivate: (studentId: string) =>
    post<Student>(`/api/students/${encodeURIComponent(studentId)}/reactivate`),
  hardDelete: (studentId: string) =>
    del<{ success: boolean; message: string }>(
      `/api/students/${encodeURIComponent(studentId)}/permanent`,
    ),
  barcodes: (studentId: string) =>
    get<Barcode[]>(`/api/students/${encodeURIComponent(studentId)}/barcodes`),
  addBarcode: (
    studentId: string,
    body: { barcode_value: string; barcode_type?: string; label?: string | null; is_primary?: boolean },
  ) => post<Barcode>(`/api/students/${encodeURIComponent(studentId)}/barcodes`, body),
  removeBarcode: (studentId: string, barcodeId: number) =>
    del<{ success: boolean; message: string }>(
      `/api/students/${encodeURIComponent(studentId)}/barcodes/${barcodeId}`,
    ),
  photoUrl: (studentId: string) => `/api/students/${encodeURIComponent(studentId)}/photo`,
  uploadPhoto: async (studentId: string, file: File): Promise<Student> => {
    const form = new FormData()
    form.append('file', file)
    const response = await fetch(
      `/api/students/${encodeURIComponent(studentId)}/photo`,
      { method: 'POST', credentials: 'include', body: form },
    )
    if (!response.ok) {
      const body = await response.json().catch(() => null)
      throw new ApiError(response.status, body?.error ?? {
        code: 'FILE_ERROR',
        message: `Upload failed with HTTP ${response.status}`,
      })
    }
    return (await response.json()) as Student
  },
}

/* --------------------------------------------------------------- barcodes */
export const barcodes = {
  lookup: (value: string) =>
    get<{ found: boolean; barcode_value: string; barcode_type: string | null; student: Student | null }>(
      `/api/barcodes/${encodeURIComponent(value)}`,
    ),
}

/* ------------------------------------------------------------------ scans */
export const scans = {
  submit: (body: {
    barcode: string
    barcode_type?: string | null
    device_name?: string | null
    source?: string
    confidence?: number | null
  }) => post<ScanVerification>('/api/scans', body),
  list: (query: Query) => get<Paged<Scan>>('/api/scans', query),
  current: () => get<ScanVerification | null>('/api/scans/current'),
  summary: (query?: Query) => get<Record<string, number | null>>('/api/scans/summary', query),
  recent: (limit = 10) => get<Scan[]>('/api/scans/recent', { limit }),
  get: (id: number) => get<Scan>(`/api/scans/${id}`),
  clearCooldowns: () => post<{ success: boolean; cleared: number }>('/api/scans/cooldown/clear'),
}

/* ----------------------------------------------------------- transactions */
export const transactions = {
  list: (query: Query) => get<Paged<Transaction>>('/api/transactions', query),
  get: (reference: string) => get<Transaction>(`/api/transactions/${encodeURIComponent(reference)}`),
  create: (body: {
    scan_id?: number | null
    student_id?: string | null
    transaction_type: TransactionType
    amount: string | number
    quantity?: number
    reference_note?: string | null
  }) => post<Transaction>('/api/transactions', body),
  setStatus: (reference: string, status: TransactionStatus, reason?: string) =>
    put<Transaction>(`/api/transactions/${encodeURIComponent(reference)}/status`, {
      status,
      reason,
    }),
  totals: (query?: Query) => get<Record<string, number | null>>('/api/transactions/totals', query),
}

/* ---------------------------------------------------------------- reports */
export const reports = {
  summary: () => get<DashboardSummary>('/api/reports/summary'),
  daily: (query: Query) => get<DailyReport>('/api/reports/daily', query),
  performance: (query: Query) => get<PerformanceReport>('/api/reports/performance', query),
  unknownBarcodes: (query: Query) =>
    get<{
      label: string
      start: string
      end: string
      rows: Array<{ barcode_value: string; attempts: number; first_seen: string; last_seen: string }>
    }>('/api/reports/unknown-barcodes', query),
  studentHistory: (studentId: string) =>
    get<{
      student: Record<string, string | number | null> | null
      scans: Scan[]
      scan_count: number
    }>(`/api/reports/students/${encodeURIComponent(studentId)}`),
  export: (body: {
    report: 'daily' | 'scans' | 'transactions' | 'unknown_barcodes' | 'student_history' | 'performance'
    format: ExportFormat
    start?: string | null
    end?: string | null
    student_id?: string | null
    result?: ScanResult | null
    limit?: number
  }) => post<ExportDescriptor>('/api/reports/export', body),
}

/* --------------------------------------------------------------- settings */
export const settings = {
  list: () => get<Setting[]>('/api/settings'),
  public: () => get<PublicSettings>('/api/settings/public'),
  update: (values: Record<string, string | number | boolean>, reason?: string) =>
    put<Setting[]>('/api/settings', { settings: values, reason }),
  reset: (keys: string[]) => request<Setting[]>('/api/settings?keys=' + keys.join('&'), { method: 'DELETE' }),
}

/* ----------------------------------------------------------------- system */
export const system = {
  health: () => get<{ status: string; database: Record<string, unknown> }>('/api/health'),
  info: () => get<Record<string, unknown>>('/api/info'),
  scannerState: () => get<ScannerState>('/api/scanner/state'),
  /** The scan workflow: current state, allowed actions and the full table. */
  scannerWorkflow: () =>
    get<ScanWorkflow & { success: boolean; states: ScanStateDefinition[] }>(
      '/api/scanner/workflow',
    ),
  /**
   * Apply a cashier action. Answers 409 `INVALID_STATE_TRANSITION` when the
   * current state does not allow it - the UI shows that rather than pretending
   * the action worked.
   */
  workflowAction: (action: string) =>
    post<{ success: boolean; action: string; workflow: ScanWorkflow }>(
      '/api/scanner/workflow',
      { action },
    ),
  startScanner: () => post<{ success: boolean; message: string }>('/api/scanner/start'),
  stopScanner: () => post<{ success: boolean; message: string }>('/api/scanner/stop'),
  audit: (query: Query) => get<{ items: AuditRow[]; total: number }>('/api/audit', query),
  backup: () => post<{ success: boolean; message: string }>('/api/maintenance/backup'),
  files: () => get<{ database: Record<string, unknown>; backups: Array<Record<string, unknown>> }>(
    '/api/maintenance/files',
  ),
  cleanup: () => post<{ success: boolean; message: string }>('/api/maintenance/cleanup'),
}

/* ----------------------------------------------------------------- stream */
export const stream = {
  /** MJPEG endpoint for the live preview. */
  mjpegUrl: (withCacheBuster = false) =>
    withCacheBuster ? `/api/stream/mjpeg?t=${Date.now()}` : '/api/stream/mjpeg',
  snapshotUrl: (quality = 82) => `/api/stream/snapshot.jpg?quality=${quality}`,
  status: () => get<Record<string, unknown>>('/api/stream/status'),
}
