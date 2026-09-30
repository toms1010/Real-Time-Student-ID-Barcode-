"""API routers, grouped by prefix."""

from app.api import (
    routes_auth,
    routes_barcodes,
    routes_reports,
    routes_scans,
    routes_settings,
    routes_stream,
    routes_students,
    routes_transactions,
)

#: Order matters only for the generated OpenAPI document.
ALL_ROUTERS = (
    routes_settings.router,      # /api/health, /api/settings, /api/scanner, /api/maintenance
    routes_auth.router,         # /api/auth
    routes_students.router,     # /api/students
    routes_barcodes.router,     # /api/barcodes
    routes_scans.router,        # /api/scans
    routes_transactions.router, # /api/transactions
    routes_reports.router,      # /api/reports
    routes_stream.router,       # /api/stream
)

__all__ = ["ALL_ROUTERS"]
