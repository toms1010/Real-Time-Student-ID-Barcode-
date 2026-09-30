-- Adds covering indexes used by the cashier reconciliation and status filter
-- reports.  Demonstrates the additive-migration path: this file runs safely
-- against a database created by schema.sql v1 and is recorded in
-- schema_migrations so it never runs twice.
--
--   python -m app.cli db-migrate

CREATE INDEX IF NOT EXISTS idx_txn_status_created
    ON transactions (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_scans_barcode_result
    ON scan_logs (barcode_value, result, scan_time DESC);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('0002', 'covering indexes for transaction status and barcode history reports');
