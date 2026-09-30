/** Add / edit student form, used by the Students page. */

import { useEffect, useState } from 'react'

import type { Student, StudentStatus } from '@/types'

const STATUSES: StudentStatus[] = ['active', 'inactive', 'suspended', 'graduated', 'transferred']

export interface StudentFormValues {
  student_id: string
  first_name: string
  middle_name: string
  last_name: string
  course: string
  year_level: string
  section: string
  school: string
  email: string
  phone: string
  status: StudentStatus
  notes: string
}

export function emptyValues(): StudentFormValues {
  return {
    student_id: '',
    first_name: '',
    middle_name: '',
    last_name: '',
    course: '',
    year_level: '',
    section: '',
    school: '',
    email: '',
    phone: '',
    status: 'active',
    notes: '',
  }
}

export function valuesFromStudent(student: Student): StudentFormValues {
  return {
    student_id: student.student_id,
    first_name: student.first_name,
    middle_name: student.middle_name ?? '',
    last_name: student.last_name,
    course: student.course ?? '',
    year_level: student.year_level ? String(student.year_level) : '',
    section: student.section ?? '',
    school: student.school ?? '',
    email: student.email ?? '',
    phone: student.phone ?? '',
    status: student.status,
    notes: student.notes ?? '',
  }
}

/** Client-side validation mirroring the Pydantic constraints, so the cashier
 *  gets an inline message instead of a 422 round trip. */
export function validate(values: StudentFormValues, isNew: boolean): Record<string, string> {
  const errors: Record<string, string> = {}
  if (isNew && !/^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$/.test(values.student_id)) {
    errors.student_id =
      '3-64 characters, starting with a letter or digit; letters, digits, hyphen and underscore only.'
  }
  if (!values.first_name.trim()) errors.first_name = 'First name is required.'
  if (!values.last_name.trim()) errors.last_name = 'Last name is required.'
  if (values.year_level) {
    const year = Number(values.year_level)
    if (!Number.isInteger(year) || year < 1 || year > 12) {
      errors.year_level = 'Enter a year between 1 and 12.'
    }
  }
  if (values.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email)) {
    errors.email = 'Enter a valid email address.'
  }
  return errors
}

export function StudentForm({
  initial,
  isNew,
  onSubmit,
  onCancel,
  busy,
}: {
  initial: StudentFormValues
  isNew: boolean
  onSubmit: (values: StudentFormValues) => void
  onCancel: () => void
  busy?: boolean
}) {
  const [values, setValues] = useState<StudentFormValues>(initial)
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => setValues(initial), [initial])

  const set = (key: keyof StudentFormValues) => (value: string) =>
    setValues((current) => ({ ...current, [key]: value }))

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        const found = validate(values, isNew)
        setErrors(found)
        if (Object.keys(found).length === 0) onSubmit(values)
      }}
    >
      <div className="form-grid">
        <div className="field">
          <label htmlFor="student_id">Student ID *</label>
          <input
            id="student_id"
            value={values.student_id}
            onChange={(event) => set('student_id')(event.target.value)}
            disabled={!isNew}
            placeholder="2026-000123"
            autoComplete="off"
          />
          {errors.student_id ? <span className="error-text">{errors.student_id}</span> : null}
          {!isNew ? (
            <span className="help">The ID cannot be changed after the card is issued.</span>
          ) : null}
        </div>

        <div className="field">
          <label htmlFor="first_name">First name *</label>
          <input
            id="first_name"
            value={values.first_name}
            onChange={(event) => set('first_name')(event.target.value)}
          />
          {errors.first_name ? <span className="error-text">{errors.first_name}</span> : null}
        </div>

        <div className="field">
          <label htmlFor="middle_name">Middle name</label>
          <input
            id="middle_name"
            value={values.middle_name}
            onChange={(event) => set('middle_name')(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="last_name">Last name *</label>
          <input
            id="last_name"
            value={values.last_name}
            onChange={(event) => set('last_name')(event.target.value)}
          />
          {errors.last_name ? <span className="error-text">{errors.last_name}</span> : null}
        </div>

        <div className="field">
          <label htmlFor="course">Course / programme</label>
          <input
            id="course"
            value={values.course}
            onChange={(event) => set('course')(event.target.value)}
            placeholder="BS Computer Engineering"
          />
        </div>

        <div className="field">
          <label htmlFor="year_level">Year level</label>
          <input
            id="year_level"
            type="number"
            min={1}
            max={12}
            value={values.year_level}
            onChange={(event) => set('year_level')(event.target.value)}
          />
          {errors.year_level ? <span className="error-text">{errors.year_level}</span> : null}
        </div>

        <div className="field">
          <label htmlFor="section">Section</label>
          <input
            id="section"
            value={values.section}
            onChange={(event) => set('section')(event.target.value)}
            placeholder="A"
          />
        </div>

        <div className="field">
          <label htmlFor="school">College / school</label>
          <input
            id="school"
            value={values.school}
            onChange={(event) => set('school')(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            value={values.email}
            onChange={(event) => set('email')(event.target.value)}
          />
          {errors.email ? <span className="error-text">{errors.email}</span> : null}
        </div>

        <div className="field">
          <label htmlFor="phone">Phone</label>
          <input
            id="phone"
            value={values.phone}
            onChange={(event) => set('phone')(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="status">Status</label>
          <select
            id="status"
            value={values.status}
            onChange={(event) => set('status')(event.target.value)}
          >
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="field" style={{ marginTop: 16 }}>
        <label htmlFor="notes">Internal notes</label>
        <textarea
          id="notes"
          value={values.notes}
          onChange={(event) => set('notes')(event.target.value)}
          placeholder="Not shown on the cashier screen."
        />
        <span className="help">Never store card numbers, passwords or medical information here.</span>
      </div>

      <div className="form-actions">
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn primary" disabled={busy}>
          {busy ? <span className="spinner" /> : null}
          {isNew ? 'Create student' : 'Save changes'}
        </button>
      </div>
    </form>
  )
}
