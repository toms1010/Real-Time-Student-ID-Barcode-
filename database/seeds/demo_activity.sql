-- Optional demo activity for the presentation: a handful of scan logs and
-- cashier transactions so the dashboard, scan history and reports have rows to
-- display before a live demonstration.
--
--   python -m app.cli seed-demo-activity        # applies this file
--   python -m app.cli seed-demo-activity --clear  # removes demo activity only
--
-- These rows are clearly marked:
--   * scan_logs.source   = 'manual'   (not produced by the scanner)
--   * scan_logs.device_name = 'demo-seed'
--   * transactions.reference_note starts with '[demo]'
--
-- They are NOT measurements.  Real benchmark numbers come from
-- `python -m app.cli benchmark` and the procedure in
-- docs/performance-evaluation.md.  Nothing here is used to fill the [MEASURE]
-- placeholders in the documentation.

DELETE FROM transactions WHERE reference_note LIKE '[demo]%';
DELETE FROM scan_logs    WHERE device_name = 'demo-seed';

INSERT OR IGNORE INTO scan_logs
    (student_id, barcode_value, scan_time, result, confidence, barcode_type,
     device_name, source, processing_time_ms, detection_time_ms, database_time_ms, error_code)
SELECT s.id,
       s.student_id,
       strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-' || (offset_minutes || ' minutes')),
       outcome.result,
       outcome.confidence,
       'Code128',
       'demo-seed',
       'manual',
       outcome.processing_ms,
       outcome.processing_ms * 0.62,
       outcome.processing_ms * 0.11,
       outcome.error_code
FROM students s
JOIN (
    SELECT '2026-000001' AS sid, 'VERIFIED'        AS result, 0.97 AS confidence, 21.4 AS processing_ms, NULL AS error_code, 3   AS offset_minutes UNION ALL
    SELECT '2026-000002',              'VERIFIED',             0.95,             19.8,            NULL,               11  UNION ALL
    SELECT '2026-000004',              'VERIFIED',             0.99,             17.2,            NULL,               26  UNION ALL
    SELECT '2026-000008',              'VERIFIED',             0.93,             24.6,            NULL,               41  UNION ALL
    SELECT '999999999',                'UNKNOWN_ID',           NULL,              12.4,            NULL,               55  UNION ALL
    SELECT 'ID-2026-000003',           'VERIFIED',             0.91,             26.1,            NULL,               70  UNION ALL
    SELECT '2026-000010',              'VERIFIED',             0.96,             18.9,            NULL,               88  UNION ALL
    SELECT '###',                      'INVALID_BARCODE',      NULL,               9.7,            NULL,              104  UNION ALL
    SELECT '2026-000012',              'VERIFIED',             0.94,             20.3,            NULL,              121  UNION ALL
    SELECT '2026-000005',              'VERIFIED',             0.90,             27.5,            NULL,              140
) AS outcome
  ON outcome.sid IN (s.student_id, 'ID-' || s.student_id);

INSERT OR IGNORE INTO transactions
    (reference_number, student_id, transaction_type, amount, cashier_id, status, reference_note, created_at)
SELECT 'TXN-DEMO-' || substr(hex(randomblob(4)), 1, 8),
       s.id,
       t.txn_type,
       t.amount,
       u.id,
       'completed',
       '[demo] ' || t.note,
       strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-' || t.offset_minutes || ' minutes')
FROM students s
JOIN users u ON u.username = 'cashier'
JOIN (
    SELECT '2026-000001' AS sid, 'Payment'      AS txn_type, 120.00 AS amount, '[demo] canteen'     AS note, 30 AS offset_minutes UNION ALL
    SELECT '2026-000002',              'Registration',  450.00,         '[demo] lab fee',      95            UNION ALL
    SELECT '2026-000004',              'Verification',    0.00,         '[demo] library entry',140            UNION ALL
    SELECT '2026-000008',              'Payment',        85.50,         '[demo] printing',    175
) AS t
  ON t.sid = s.student_id;
