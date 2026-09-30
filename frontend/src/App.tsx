/**
 * Application root: providers, routing and the shared layout.
 *
 * The layout needs the scanner state on every screen (the sidebar shows it and
 * the top bar surfaces errors), so it is fetched once here with a 2 s poll and
 * passed down.  Each page also polls whatever it needs, which is cheap because
 * the service answers in single-digit milliseconds.
 */

import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { Layout } from '@/components/Layout'
import { ToastProvider } from '@/hooks/useToast'
import { AuthProvider, useAuth } from '@/hooks/useAuth'
import { useApi } from '@/hooks/useApi'
import { system } from '@/services/api'
import { DashboardPage } from '@/pages/DashboardPage'
import { LoginPage } from '@/pages/LoginPage'
import { ReportsPage } from '@/pages/ReportsPage'
import { ScanHistoryPage } from '@/pages/ScanHistoryPage'
import { ScannerPage } from '@/pages/ScannerPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { StudentsPage } from '@/pages/StudentsPage'
import { TransactionsPage } from '@/pages/TransactionsPage'
import type { ScannerState } from '@/types'

const TITLES: Record<string, { title: string; subtitle: string }> = {
  '/': { title: 'Dashboard', subtitle: 'Today at a glance' },
  '/scanner': { title: 'Scanner', subtitle: 'Present the student ID inside the scan area' },
  '/students': { title: 'Students', subtitle: 'Enrolment records and their barcodes' },
  '/history': { title: 'Scan history', subtitle: 'Every scan attempt, successful or not' },
  '/transactions': { title: 'Transactions', subtitle: 'Cashier records attached to verified scans' },
  '/reports': { title: 'Reports', subtitle: 'Measured numbers only - nothing estimated' },
  '/settings': { title: 'Settings', subtitle: 'Runtime configuration, users and maintenance' },
}

function Shell() {
  const { user, loading, requireLogin, publicSettings } = useAuth()
  const scanner = useApi(() => system.scannerState(), [], 2000)
  const location = useLocation()
  const meta = TITLES[location.pathname] ?? TITLES['/']

  if (loading) {
    return (
      <div className="login-shell">
        <div className="loading-row">
          <span className="spinner" /> Starting the session…
        </div>
      </div>
    )
  }

  if (requireLogin && !user) {
    return <LoginPage />
  }

  return (
    <Layout
      title={meta.title}
      subtitle={meta.subtitle}
      applicationName={publicSettings?.application_name}
      scannerState={scanner.data as ScannerState | null}
    >
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/scanner" element={<ScannerPage />} />
        <Route path="/students" element={<StudentsPage />} />
        <Route path="/history" element={<ScanHistoryPage />} />
        <Route path="/transactions" element={<TransactionsPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <AuthProvider>
          <Shell />
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  )
}
