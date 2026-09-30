/** Student management: search, list, add, edit, deactivate, barcodes, photo. */

import { useMemo, useState } from 'react'

import { Card, Chip, DataTable, ErrorBanner, Modal, type Column } from '@/components/ui'
import {
  StudentForm,
  emptyValues,
  valuesFromStudent,
  type StudentFormValues,
} from '@/components/StudentForm'
import { useApi, useDebounced, useLocalState } from '@/hooks/useApi'
import { useAuth } from '@/hooks/useAuth'
import { useToast } from '@/hooks/useToast'
import { ApiError, students as studentsApi, system } from '@/services/api'
import { formatDateTime } from '@/utils/format'
import { statusLabel, statusTone } from '@/utils/ui'
import type { Student } from '@/types'

const PAGE_SIZE = 25

export function StudentsPage() {
  const { can } = useAuth()
  const toast = useToast()

  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [course, setCourse] = useState('')
  const [page, setPage] = useState(0)
  const debouncedSearch = useDebounced(search, 320)
  const [pageSize, setPageSize] = useLocalState('sidb-students-page-size', PAGE_SIZE)

  const [editing, setEditing] = useState<Student | null>(null)
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState<Student | null>(null)

  const query = useMemo(
    () => ({
      search: debouncedSearch || undefined,
      status: status || undefined,
      course: course || undefined,
      limit: pageSize,
      offset: page * pageSize,
      sort: 'student_id',
      order: 'asc',
    }),
    [debouncedSearch, status, course, page, pageSize],
  )

  const list = useApi(() => studentsApi.list(query), [query])
  const courses = useApi(() => studentsApi.courses(), [])

  const rows = list.data?.items ?? []
  const total = list.data?.page.total ?? 0
  const canWrite = can('students:write')

  const submit = async (values: StudentFormValues) => {
    setBusy(true)
    try {
      if (editing) {
        await studentsApi.update(editing.student_id, {
          first_name: values.first_name,
          last_name: values.last_name,
          middle_name: values.middle_name || null,
          course: values.course || null,
          year_level: values.year_level ? Number(values.year_level) : null,
          section: values.section || null,
          school: values.school || null,
          email: values.email || null,
          phone: values.phone || null,
          status: values.status,
          notes: values.notes || null,
        })
        toast.success('Student updated', values.first_name + ' ' + values.last_name)
      } else {
        const created = await studentsApi.create({
          student_id: values.student_id,
          first_name: values.first_name,
          last_name: values.last_name,
          middle_name: values.middle_name || null,
          course: values.course || null,
          year_level: values.year_level ? Number(values.year_level) : null,
          section: values.section || null,
          school: values.school || null,
          email: values.email || null,
          phone: values.phone || null,
          status: values.status,
          notes: values.notes || null,
        })
        toast.success('Student created', `${created.full_name} (${created.student_id})`)
      }
      setEditing(null)
      setCreating(false)
      await list.reload()
      await courses.reload()
    } catch (cause) {
      toast.error('Save failed', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setBusy(false)
    }
  }

  const toggleStatus = async (student: Student) => {
    try {
      if (student.status === 'active') {
        await studentsApi.deactivate(student.student_id)
        toast.warning('Student deactivated', `${student.full_name} can no longer transact`)
      } else {
        await studentsApi.reactivate(student.student_id)
        toast.success('Student reactivated', student.full_name)
      }
      await list.reload()
    } catch (cause) {
      toast.error('Action failed', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const addBarcode = async (student: Student, value: string) => {
    try {
      await studentsApi.addBarcode(student.student_id, { barcode_value: value, barcode_type: 'Code128' })
      toast.success('Barcode registered', value)
      await list.reload()
    } catch (cause) {
      toast.error('Could not register the barcode', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const columns: Column<Student>[] = [
    {
      key: 'student_id',
      header: 'Student ID',
      render: (row) => <span className="mono">{row.student_id}</span>,
    },
    {
      key: 'name',
      header: 'Name',
      render: (row) => (
        <>
          <div>{row.full_name}</div>
          {row.email ? <div className="dim small">{row.email}</div> : null}
        </>
      ),
    },
    {
      key: 'course',
      header: 'Course',
      render: (row) => (
        <>
          <div>{row.course ?? '—'}</div>
          <div className="dim small">
            {row.year_level ? `Year ${row.year_level}` : ''}
            {row.section ? ` · Section ${row.section}` : ''}
          </div>
        </>
      ),
    },
    {
      key: 'barcodes',
      header: 'Barcodes',
      render: (row) => (
        <span className="mono small">
          {row.barcodes.length === 0 ? (
            <span className="dim">none</span>
          ) : (
            row.barcodes
              .filter((barcode) => !barcode.retired_at)
              .map((barcode) => barcode.barcode_value)
              .join(', ')
          )}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (row) => (
        <span className={`chip ${statusTone(row.status)}`}>
          <span className="dot" />
          {statusLabel(row.status)}
        </span>
      ),
    },
    {
      key: 'actions',
      header: '',
      render: (row) => (
        <div className="flex-row" onClick={(event) => event.stopPropagation()}>
          <button type="button" className="btn ghost small" onClick={() => setDetail(row)}>
            View
          </button>
          {canWrite ? (
            <>
              <button type="button" className="btn ghost small" onClick={() => setEditing(row)}>
                Edit
              </button>
              <button
                type="button"
                className="btn ghost small"
                onClick={() => toggleStatus(row)}
                title={row.status === 'active' ? 'Deactivate' : 'Reactivate'}
              >
                {row.status === 'active' ? 'Deactivate' : 'Reactivate'}
              </button>
            </>
          ) : null}
        </div>
      ),
    },
  ]

  return (
    <>
      <ErrorBanner error={list.error} onRetry={list.reload} />

      <Card
        title="Students"
        subtitle={`${total} record${total === 1 ? '' : 's'} match the current filters`}
        actions={
          canWrite ? (
            <button type="button" className="btn primary" onClick={() => setCreating(true)}>
              + Add student
            </button>
          ) : null
        }
      >
        <div className="toolbar" style={{ marginBottom: 16 }}>
          <div className="field" style={{ flex: 1, minWidth: 240 }}>
            <label htmlFor="search">Search</label>
            <input
              id="search"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value)
                setPage(0)
              }}
              placeholder="ID, name, email or barcode…"
              autoComplete="off"
            />
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
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
              <option value="suspended">Suspended</option>
              <option value="graduated">Graduated</option>
              <option value="transferred">Transferred</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="course">Course</label>
            <select
              id="course"
              value={course}
              onChange={(event) => {
                setCourse(event.target.value)
                setPage(0)
              }}
            >
              <option value="">All</option>
              {(courses.data ?? []).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
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
          onRowClick={(row) => setDetail(row)}
          empty="No students match these filters."
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

      {creating ? (
        <Modal title="Add student" onClose={() => setCreating(false)}>
          <p className="small muted" style={{ marginTop: 0 }}>
            A Code 128 barcode is registered automatically with the student number (ADR-002). More
            barcodes can be added afterwards.
          </p>
          <StudentForm
            initial={emptyValues()}
            isNew
            busy={busy}
            onSubmit={submit}
            onCancel={() => setCreating(false)}
          />
        </Modal>
      ) : null}

      {editing ? (
        <Modal title={`Edit ${editing.full_name}`} onClose={() => setEditing(null)}>
          <StudentForm
            initial={valuesFromStudent(editing)}
            isNew={false}
            busy={busy}
            onSubmit={submit}
            onCancel={() => setEditing(null)}
          />
        </Modal>
      ) : null}

      {detail ? (
        <StudentDetail
          student={detail}
          onClose={() => setDetail(null)}
          onAddBarcode={(value) => addBarcode(detail, value)}
          onRefresh={list.reload}
        />
      ) : null}
    </>
  )
}

function StudentDetail({
  student,
  onClose,
  onAddBarcode,
  onRefresh,
}: {
  student: Student
  onClose: () => void
  onAddBarcode: (value: string) => Promise<void>
  onRefresh: () => Promise<void>
}) {
  const [newBarcode, setNewBarcode] = useState('')
  const [uploading, setUploading] = useState(false)
  const toast = useToast()
  const audit = useApi(() => system.audit({ limit: 20 }), [])

  return (
    <Modal
      title={`${student.full_name} · ${student.student_id}`}
      onClose={onClose}
      footer={
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      }
    >
      <div className="grid cols-2">
        <div>
          <div className="student-headline">
            {student.photo_path ? (
              <img className="photo" src={studentsApi.photoUrl(student.student_id)} alt="" />
            ) : (
              <div className="photo-placeholder">no photo</div>
            )}
            <div>
              <div className="student-name">{student.full_name}</div>
              <div className="student-id">{student.student_id}</div>
            </div>
          </div>
          <dl className="detail-list" style={{ marginTop: 16 }}>
            <dt>Course</dt>
            <dd>{student.course ?? '—'}</dd>
            <dt>Year / Section</dt>
            <dd>
              {student.year_level ?? '—'} / {student.section ?? '—'}
            </dd>
            <dt>College</dt>
            <dd>{student.school ?? '—'}</dd>
            <dt>Email</dt>
            <dd>{student.email ?? '—'}</dd>
            <dt>Phone</dt>
            <dd>{student.phone ?? '—'}</dd>
            <dt>Status</dt>
            <dd>
              <span className={`chip ${statusTone(student.status)}`}>
                <span className="dot" />
                {statusLabel(student.status)}
              </span>
            </dd>
            <dt>Created</dt>
            <dd>{formatDateTime(student.created_at)}</dd>
            <dt>Updated</dt>
            <dd>{formatDateTime(student.updated_at)}</dd>
          </dl>
          {student.notes ? (
            <div className="banner info" style={{ marginTop: 12 }}>
              {student.notes}
            </div>
          ) : null}

          <div className="field" style={{ marginTop: 16 }}>
            <label htmlFor="photo">Photo (optional, JPEG/PNG, max 2 MiB)</label>
            <input
              id="photo"
              type="file"
              accept="image/jpeg,image/png"
              onChange={async (event) => {
                const file = event.target.files?.[0]
                if (!file) return
                setUploading(true)
                try {
                  await studentsApi.uploadPhoto(student.student_id, file)
                  toast.success('Photo stored')
                  await onRefresh()
                } catch (cause) {
                  toast.error('Upload rejected', cause instanceof ApiError ? cause.message : undefined)
                } finally {
                  setUploading(false)
                  event.target.value = ''
                }
              }}
            />
            {uploading ? <span className="help">Uploading…</span> : null}
          </div>
        </div>

        <div>
          <h3>Barcodes</h3>
          <div className="stack" style={{ marginTop: 8 }}>
            {student.barcodes.length === 0 ? (
              <p className="small muted">No barcodes registered.</p>
            ) : (
              student.barcodes.map((barcode) => (
                <div className="flex-row" key={barcode.id}>
                  <Chip tone={barcode.retired_at ? 'neutral' : 'success'}>
                    {barcode.barcode_type}
                  </Chip>
                  <span className="mono">{barcode.barcode_value}</span>
                  {barcode.is_primary ? <span className="dim small">primary</span> : null}
                  {barcode.retired_at ? <span className="dim small">retired</span> : null}
                </div>
              ))
            )}
            <div className="flex-row">
              <input
                value={newBarcode}
                onChange={(event) => setNewBarcode(event.target.value)}
                placeholder="Additional barcode value"
                style={{ flex: 1 }}
              />
              <button
                type="button"
                className="btn"
                disabled={!newBarcode.trim()}
                onClick={async () => {
                  await onAddBarcode(newBarcode.trim())
                  setNewBarcode('')
                }}
              >
                Register
              </button>
            </div>
            <p className="small dim" style={{ margin: 0 }}>
              A card whose symbology is not in the scanner's allow-list will be reported as
              UNSUPPORTED_FORMAT instead of resolving to a student.
            </p>
          </div>

          <h3 style={{ marginTop: 20 }}>Recent activity</h3>
          <div className="stack" style={{ marginTop: 8 }}>
            {(audit.data?.items ?? []).slice(0, 8).map((row) => (
              <div key={row.id} className="flex-row small">
                <span className="dim mono">{formatDateTime(row.created_at)}</span>
                <span>{row.action}</span>
                <span className="dim">{row.entity_id ?? ''}</span>
              </div>
            ))}
            {audit.loading ? <span className="small dim">Loading…</span> : null}
          </div>
        </div>
      </div>
    </Modal>
  )
}
