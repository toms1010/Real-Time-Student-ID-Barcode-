/** Settings: runtime settings, users, database maintenance, audit log. */

import { useState } from 'react'

import { Card, Chip, DataTable, ErrorBanner, Modal, type Column } from '@/components/ui'
import { useApi } from '@/hooks/useApi'
import { useAuth } from '@/hooks/useAuth'
import { useToast } from '@/hooks/useToast'
import { ApiError, auth as authApi, settings as settingsApi, system } from '@/services/api'
import { formatBytes, formatDateTime } from '@/utils/format'
import type { AuditRow, Setting, User } from '@/types'

type Tab = 'runtime' | 'users' | 'maintenance' | 'audit'

export function SettingsPage() {
  const { can, user, theme, setTheme, publicSettings, signOut } = useAuth()
  const [tab, setTab] = useState<Tab>('runtime')

  const tabs: Array<{ id: Tab; label: string; show: boolean }> = [
    { id: 'runtime', label: 'Runtime settings', show: true },
    { id: 'users', label: 'Users', show: can('users:read') },
    { id: 'maintenance', label: 'Database', show: can('backup') },
    { id: 'audit', label: 'Audit log', show: can('audit:read') },
  ]
  const visible = tabs.filter((item) => item.show)

  return (
    <>
      <div className="flex-row" role="tablist" style={{ marginBottom: 4 }}>
        {visible.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={`btn small ${tab === item.id ? 'primary' : ''}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === 'runtime' ? (
        <RuntimeSettings
          theme={theme}
          setTheme={setTheme}
          applicationName={publicSettings?.application_name ?? 'Student ID Barcode Detection System'}
          version={publicSettings?.version ?? '1.0.0'}
          currentUser={user?.username ?? ''}
          onSignOut={signOut}
        />
      ) : null}
      {tab === 'users' ? <UsersPanel /> : null}
      {tab === 'maintenance' ? <MaintenancePanel /> : null}
      {tab === 'audit' ? <AuditPanel /> : null}
    </>
  )
}

/* ---------------------------------------------------------------- runtime */
function RuntimeSettings({
  theme,
  setTheme,
  applicationName,
  version,
  currentUser,
  onSignOut,
}: {
  theme: 'dark' | 'light'
  setTheme: (value: 'dark' | 'light') => void
  applicationName: string
  version: string
  currentUser: string
  onSignOut: () => Promise<void>
}) {
  const toast = useToast()
  const { can } = useAuth()
  const settings = useApi(() => settingsApi.list(), [])
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)

  const groups = (settings.data ?? []).reduce<Record<string, Setting[]>>((accumulator, setting) => {
    accumulator[setting.category] = [...(accumulator[setting.category] ?? []), setting]
    return accumulator
  }, {})

  const valueOf = (setting: Setting) => draft[setting.key] ?? (setting.value ?? '')

  const save = async () => {
    const changed: Record<string, string | number | boolean> = {}
    for (const setting of settings.data ?? []) {
      const current = setting.value ?? ''
      const next = draft[setting.key]
      if (next !== undefined && next !== current) {
        changed[setting.key] =
          setting.value_type === 'int' || setting.value_type === 'float'
            ? Number(next)
            : setting.value_type === 'bool'
              ? next === 'true'
              : next
      }
    }
    if (Object.keys(changed).length === 0) {
      toast.info('Nothing to save', 'No value was changed.')
      return
    }
    setSaving(true)
    try {
      await settingsApi.update(changed, 'updated from the settings screen')
      toast.success('Settings saved', `${Object.keys(changed).length} value(s) updated`)
      setDraft({})
      await settings.reload()
    } catch (cause) {
      toast.error('Could not save', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setSaving(false)
    }
  }

  const reset = async (keys: string[]) => {
    try {
      await settingsApi.reset(keys)
      toast.success('Reset to the config.yaml values')
      await settings.reload()
    } catch (cause) {
      toast.error('Reset failed', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  return (
    <>
      <ErrorBanner error={settings.error} onRetry={settings.reload} />

      <Card
        title="Runtime settings"
        subtitle="These override config/config.yaml without a restart. Deployment-level values (bind address, database path, camera geometry) are only in the YAML file."
        actions={
          can('settings:write') ? (
            <button type="button" className="btn primary" onClick={save} disabled={saving}>
              {saving ? <span className="spinner" /> : null}
              Save changes
            </button>
          ) : null
        }
      >
        {Object.entries(groups).map(([category, items]) => (
          <div key={category} style={{ marginBottom: 24 }}>
            <h3 style={{ textTransform: 'capitalize', marginBottom: 8 }}>{category}</h3>
            <div className="form-grid">
              {items.map((setting) => {
                const isLong = setting.description !== null && setting.description.length > 40
                return (
                  <div className="field" key={setting.key}>
                    <label htmlFor={setting.key}>{setting.key.replace(/_/g, ' ')}</label>
                    {setting.value_type === 'bool' ? (
                      <label className="inline-check">
                        <input
                          id={setting.key}
                          type="checkbox"
                          checked={valueOf(setting) === 'true'}
                          disabled={!can('settings:write')}
                          onChange={(event) =>
                            setDraft((current) => ({
                              ...current,
                              [setting.key]: event.target.checked ? 'true' : 'false',
                            }))
                          }
                        />
                        enabled
                      </label>
                    ) : (
                      <input
                        id={setting.key}
                        value={valueOf(setting)}
                        disabled={!can('settings:write')}
                        onChange={(event) =>
                          setDraft((current) => ({ ...current, [setting.key]: event.target.value }))
                        }
                      />
                    )}
                    {setting.description ? <span className="help">{setting.description}</span> : null}
                    {can('settings:write') ? (
                      <button
                        type="button"
                        className="btn ghost small"
                        style={{ alignSelf: 'flex-start' }}
                        onClick={() => reset([setting.key])}
                        title={isLong ? 'Discard the override' : 'Discard this override'}
                      >
                        reset
                      </button>
                    ) : null}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </Card>

      <div className="grid cols-2">
        <Card title="Appearance" subtitle="Stored in this browser and mirrored to the server setting">
          <div className="flex-row">
            <button
              type="button"
              className={`btn ${theme === 'dark' ? 'primary' : ''}`}
              onClick={() => setTheme('dark')}
            >
              🌙 Dark
            </button>
            <button
              type="button"
              className={`btn ${theme === 'light' ? 'primary' : ''}`}
              onClick={() => setTheme('light')}
            >
              ☀ Light
            </button>
          </div>
          <p className="small dim" style={{ marginTop: 12, marginBottom: 0 }}>
            Both themes keep the verification states high-contrast: VERIFIED is green, NOT FOUND is
            amber, ERROR is red, in either theme.
          </p>
        </Card>

        <Card title="Account" subtitle={currentUser}>
          <ChangePasswordForm onDone={onSignOut} />
        </Card>
      </div>

      <Card title="About">
        <dl className="detail-list">
          <dt>Application</dt>
          <dd>{applicationName}</dd>
          <dt>Version</dt>
          <dd className="mono">{version}</dd>
          <dt>Works offline</dt>
          <dd>Yes — no cloud service, no external API, no internet connection required.</dd>
        </dl>
      </Card>
    </>
  )
}

function ChangePasswordForm({ onDone }: { onDone: () => Promise<void> }) {
  const toast = useToast()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (next !== confirm) {
      toast.error('The new passwords do not match')
      return
    }
    setBusy(true)
    try {
      const response = await authApi.changePassword(current, next)
      toast.success(response.message)
      setCurrent('')
      setNext('')
      setConfirm('')
      await onDone()
    } catch (cause) {
      toast.error('Could not change the password', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="stack">
      <div className="field">
        <label htmlFor="current_password">Current password</label>
        <input
          id="current_password"
          type="password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          autoComplete="current-password"
        />
      </div>
      <div className="field">
        <label htmlFor="new_password">New password</label>
        <input
          id="new_password"
          type="password"
          value={next}
          onChange={(event) => setNext(event.target.value)}
          autoComplete="new-password"
        />
        <span className="help">
          At least 10 characters with an uppercase letter, a lowercase letter and a digit.
          Changing it signs out every other device.
        </span>
      </div>
      <div className="field">
        <label htmlFor="confirm_password">Confirm new password</label>
        <input
          id="confirm_password"
          type="password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          autoComplete="new-password"
        />
      </div>
      <div>
        <button type="submit" className="btn" disabled={busy || !current || !next}>
          {busy ? <span className="spinner" /> : null}
          Change password
        </button>
      </div>
    </form>
  )
}

/* ------------------------------------------------------------------ users */
function UsersPanel() {
  const toast = useToast()
  const { user: me } = useAuth()
  const users = useApi(() => authApi.listUsers(), [])
  const [creating, setCreating] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [role, setRole] = useState('cashier')
  const [busy, setBusy] = useState(false)

  const create = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    try {
      await authApi.createUser({
        username,
        password,
        full_name: fullName || null,
        role,
        must_change_password: true,
      })
      toast.success('User created', username)
      setCreating(false)
      setUsername('')
      setPassword('')
      setFullName('')
      await users.reload()
    } catch (cause) {
      toast.error('Could not create the user', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setBusy(false)
    }
  }

  const setUserActive = async (target: User, active: boolean) => {
    try {
      await authApi.updateUser(target.id, { is_active: active })
      toast.success(active ? 'User activated' : 'User deactivated', target.username)
      await users.reload()
    } catch (cause) {
      toast.error('Update failed', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const setUserRole = async (target: User, nextRole: string) => {
    try {
      await authApi.updateUser(target.id, { role: nextRole })
      toast.success('Role updated', `${target.username} is now ${nextRole}`)
      await users.reload()
    } catch (cause) {
      toast.error('Update failed', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const columns: Column<User>[] = [
    { key: 'username', header: 'Username', render: (row) => <span className="mono">{row.username}</span> },
    { key: 'name', header: 'Full name', render: (row) => row.full_name ?? '—' },
    {
      key: 'role',
      header: 'Role',
      render: (row) => (
        <select
          value={row.role}
          onChange={(event) => setUserRole(row, event.target.value)}
          disabled={row.id === me?.id}
        >
          {['admin', 'cashier', 'staff', 'operator', 'viewer'].map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      ),
    },
    {
      key: 'active',
      header: 'Status',
      render: (row) => (
        <span className={`chip ${row.is_active ? 'success' : 'neutral'}`}>
          <span className="dot" />
          {row.is_active ? 'active' : 'disabled'}
        </span>
      ),
    },
    { key: 'last', header: 'Last sign-in', render: (row) => formatDateTime(row.last_login_at) },
    {
      key: 'actions',
      header: '',
      render: (row) =>
        row.id === me?.id ? (
          <span className="dim small">you</span>
        ) : (
          <button type="button" className="btn ghost small" onClick={() => setUserActive(row, !row.is_active)}>
            {row.is_active ? 'Disable' : 'Enable'}
          </button>
        ),
    },
  ]

  return (
    <>
      <ErrorBanner error={users.error} onRetry={users.reload} />
      <Card
        title="Users"
        subtitle="Passwords are stored as Argon2id hashes; the service never sees or logs a plaintext password"
        actions={
          <button type="button" className="btn primary" onClick={() => setCreating(true)}>
            + Add user
          </button>
        }
      >
        <DataTable
          columns={columns}
          rows={users.data?.items ?? []}
          rowKey={(row) => row.id}
          loading={users.loading}
        />
      </Card>

      {creating ? (
        <Modal title="Add user" onClose={() => setCreating(false)}>
          <form onSubmit={create} className="stack">
            <div className="field">
              <label htmlFor="new_username">Username</label>
              <input
                id="new_username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="off"
                required
              />
            </div>
            <div className="field">
              <label htmlFor="new_fullname">Full name</label>
              <input
                id="new_fullname"
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="new_password">Temporary password</label>
              <input
                id="new_password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
                required
              />
              <span className="help">
                The user must change it at first sign-in. Send it over a channel the person trusts.
              </span>
            </div>
            <div className="field">
              <label htmlFor="new_role">Role</label>
              <select id="new_role" value={role} onChange={(event) => setRole(event.target.value)}>
                <option value="cashier">cashier — scan, verify, record transactions</option>
                <option value="staff">staff — scan, verify, read reports</option>
                <option value="viewer">viewer — read-only</option>
                <option value="operator">operator — legacy cashier permissions</option>
                <option value="admin">admin — full access</option>
              </select>
            </div>
            <div className="form-actions">
              <button type="button" className="btn" onClick={() => setCreating(false)}>
                Cancel
              </button>
              <button type="submit" className="btn primary" disabled={busy}>
                {busy ? <span className="spinner" /> : null}
                Create user
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
    </>
  )
}

/* ------------------------------------------------------------ maintenance */
function MaintenancePanel() {
  const toast = useToast()
  const files = useApi(() => system.files(), [])
  const health = useApi(() => system.health(), [], 10_000)
  const [busy, setBusy] = useState(false)

  const run = async (action: 'backup' | 'cleanup') => {
    setBusy(true)
    try {
      const response =
        action === 'backup' ? await system.backup() : await system.cleanup()
      toast.success('Done', response.message)
      await files.reload()
    } catch (cause) {
      toast.error('Operation failed', cause instanceof ApiError ? cause.message : undefined)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Card
        title="Database"
        subtitle="Backups use SQLite's online backup API, so the copy is consistent even while the scanner is writing"
        actions={
          <>
            <button type="button" className="btn" onClick={() => run('backup')} disabled={busy}>
              Create backup
            </button>
            <button type="button" className="btn" onClick={() => run('cleanup')} disabled={busy}>
              Clean up old sessions
            </button>
          </>
        }
      >
        <ErrorBanner error={files.error} onRetry={files.reload} />
        {health.data ? (
          <div className="flex-row" style={{ marginBottom: 16 }}>
            <Chip tone={health.data.status === 'ok' ? 'success' : 'warning'}>
              {health.data.status}
            </Chip>
            <span className="small muted">
              database {(health.data.database as { status?: string })?.status ?? 'unknown'}
            </span>
          </div>
        ) : null}

        <DataTable
          columns={[
            { key: 'name', header: 'File', render: (row) => <span className="mono">{String(row.name)}</span> },
            { key: 'size', header: 'Size', numeric: true, render: (row) => formatBytes(Number(row.size_bytes)) },
            { key: 'modified', header: 'Modified', render: (row) => formatDateTime(String(row.modified)) },
          ]}
          rows={(files.data?.backups ?? []) as Array<Record<string, unknown>>}
          rowKey={(row) => String(row.name)}
          loading={files.loading}
          empty="No backups yet. Create one before any risky change."
        />

        {files.data?.database ? (
          <p className="small dim" style={{ marginTop: 12, marginBottom: 0 }}>
            Active database: <span className="mono">{String(files.data.database.path)}</span> (
            {formatBytes(Number(files.data.database.size_bytes))})
          </p>
        ) : null}
      </Card>
    </>
  )
}

/* ------------------------------------------------------------------ audit */
function AuditPanel() {
  const audit = useApi(() => system.audit({ limit: 200 }), [], 20_000)

  const columns: Column<AuditRow>[] = [
    { key: 'time', header: 'Time', render: (row) => formatDateTime(row.created_at) },
    { key: 'user', header: 'User', render: (row) => row.username ?? '—' },
    { key: 'action', header: 'Action', render: (row) => <span className="mono small">{row.action}</span> },
    { key: 'entity', header: 'Entity', render: (row) => `${row.entity_type ?? '—'} ${row.entity_id ?? ''}` },
    { key: 'ip', header: 'Source', render: (row) => <span className="dim small">{row.ip_address ?? '—'}</span> },
  ]

  return (
    <Card title="Audit log" subtitle="Every privileged action, kept for the registrar's records">
      <ErrorBanner error={audit.error} onRetry={audit.reload} />
      <DataTable
        columns={columns}
        rows={audit.data?.items ?? []}
        rowKey={(row) => row.id}
        loading={audit.loading}
        empty="No audit entries yet."
      />
    </Card>
  )
}
