-- ===========================================================================
--  Real-Time Student ID Barcode Detection and Verification System
--  SQLite schema - authoritative definition of the database.
--
--  Design notes
--  ------------
--  * `students.student_id` is the human readable number printed on the card
--    (e.g. "2026-000123").  The barcode payload equals this value, see
--    docs/architecture/decisions.md (ADR-002).
--  * Barcodes live in their own table so one student can hold several
--    symbologies at once (legacy Code 39 card + current Code 128 card).
--  * `scan_logs.student_id` is a *nullable* reference to `students.id`: an
--    unknown card must still be logged, and deleting a student must not erase
--    the audit trail of their past scans.
--  * Every timestamp column is stored as ISO-8601 UTC text
--    ("YYYY-MM-DDTHH:MM:SS.sssZ") so reports do not depend on the reader's
--    local timezone.  See python/app/utils/timeutil.py.
--  * This file is applied by `python -m app.cli db-init` and by
--    `scripts/setup.sh`.  Changes must be added as a new numbered file in
--    database/migrations/ (see database/migrations/README.md).
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Schema bookkeeping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT
);

-- ---------------------------------------------------------------------------
-- Students
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS students (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    TEXT    NOT NULL UNIQUE,
    first_name    TEXT    NOT NULL,
    middle_name   TEXT,
    last_name     TEXT    NOT NULL,
    course        TEXT,
    year_level    INTEGER,
    section       TEXT,
    school        TEXT,
    email         TEXT,
    phone         TEXT,
    photo_path    TEXT,
    status        TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'inactive', 'graduated',
                                            'suspended', 'transferred')),
    notes         TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (length(trim(student_id)) BETWEEN 3 AND 64),
    CHECK (year_level IS NULL OR year_level BETWEEN 1 AND 12)
);

CREATE INDEX IF NOT EXISTS idx_students_status      ON students (status);
CREATE INDEX IF NOT EXISTS idx_students_last_name   ON students (last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_students_course      ON students (course);

-- Full display name helper used by the report exporters.
CREATE VIEW IF NOT EXISTS v_student_display AS
SELECT id,
       student_id,
       first_name,
       middle_name,
       last_name,
       trim(first_name || ' ' || coalesce(middle_name || ' ', '') || last_name) AS full_name,
       course,
       year_level,
       section,
       school,
       status
FROM students;

-- ---------------------------------------------------------------------------
-- Student barcodes (1 : N from students)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS student_barcodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL,
    barcode_value TEXT    NOT NULL UNIQUE,
    barcode_type  TEXT    NOT NULL DEFAULT 'Code128',
    label         TEXT,
    is_primary    INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    retired_at    TEXT,

    FOREIGN KEY (student_id) REFERENCES students (id) ON DELETE CASCADE,
    CHECK (length(trim(barcode_value)) BETWEEN 1 AND 128)
);

CREATE INDEX IF NOT EXISTS idx_barcodes_student ON student_barcodes (student_id);
CREATE INDEX IF NOT EXISTS idx_barcodes_type   ON student_barcodes (barcode_type);
-- Only one active primary barcode per student.
CREATE UNIQUE INDEX IF NOT EXISTS uq_barcodes_primary_student
    ON student_barcodes (student_id) WHERE is_primary = 1;

-- ---------------------------------------------------------------------------
-- Scan log - one row per scan attempt that reached the decoder
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id          INTEGER,          -- students.id, NULL when unknown
    barcode_value       TEXT    NOT NULL,
    scan_time           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    result              TEXT    NOT NULL
                                CHECK (result IN ('VERIFIED', 'UNKNOWN_ID', 'INVALID_BARCODE',
                                                  'DUPLICATE_SCAN', 'UNSUPPORTED_FORMAT', 'ERROR')),
    confidence          REAL,
    barcode_type        TEXT,
    device_name         TEXT,
    source              TEXT    NOT NULL DEFAULT 'cpp'
                                CHECK (source IN ('cpp', 'python', 'manual', 'api')),
    processing_time_ms  REAL,
    detection_time_ms   REAL,
    database_time_ms    REAL,
    frame_path          TEXT,
    error_code          TEXT,
    error_message       TEXT,
    operator_id         INTEGER,          -- users.id, NULL for the kiosk session

    FOREIGN KEY (student_id)  REFERENCES students (id) ON DELETE SET NULL,
    FOREIGN KEY (operator_id) REFERENCES users (id)    ON DELETE SET NULL,
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    CHECK (processing_time_ms IS NULL OR processing_time_ms >= 0.0)
);

CREATE INDEX IF NOT EXISTS idx_scans_time      ON scan_logs (scan_time DESC);
CREATE INDEX IF NOT EXISTS idx_scans_student   ON scan_logs (student_id, scan_time DESC);
CREATE INDEX IF NOT EXISTS idx_scans_result    ON scan_logs (result, scan_time DESC);
CREATE INDEX IF NOT EXISTS idx_scans_barcode   ON scan_logs (barcode_value, scan_time DESC);

-- ---------------------------------------------------------------------------
-- Cashier transactions (performs 1 : N from users, N : 1 from students)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_number  TEXT    NOT NULL UNIQUE,
    student_id        INTEGER,
    scan_id           INTEGER,        -- originating scan_logs.id
    transaction_type  TEXT    NOT NULL DEFAULT 'Payment'
                            CHECK (transaction_type IN
                                   ('Payment', 'Verification', 'Registration', 'Other')),
    amount            REAL    NOT NULL DEFAULT 0.0 CHECK (amount >= 0.0),
    quantity          INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
    cashier_id        INTEGER,
    status            TEXT    NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('pending', 'completed', 'cancelled', 'refunded')),
    reference_note    TEXT,           -- free-text purpose / OR number, never card data
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (student_id) REFERENCES students (id)   ON DELETE SET NULL,
    FOREIGN KEY (scan_id)    REFERENCES scan_logs (id)  ON DELETE SET NULL,
    FOREIGN KEY (cashier_id) REFERENCES users (id)      ON DELETE SET NULL,
    CHECK (length(trim(reference_note)) <= 500)
);

CREATE INDEX IF NOT EXISTS idx_txn_created  ON transactions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_student  ON transactions (student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_cashier  ON transactions (cashier_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_type     ON transactions (transaction_type, created_at DESC);

-- ---------------------------------------------------------------------------
-- Users / authentication / audit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,        -- Argon2id, never reversible
    full_name     TEXT,
    role          TEXT    NOT NULL DEFAULT 'operator'
                          CHECK (role IN ('admin', 'cashier', 'staff', 'operator', 'viewer')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_login_at TEXT,

    CHECK (length(trim(username)) BETWEEN 3 AND 64)
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);

CREATE TABLE IF NOT EXISTS user_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,   -- SHA-256 of the bearer token
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at   TEXT    NOT NULL,
    revoked_at   TEXT,
    user_agent   TEXT,
    client_ip    TEXT,

    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user   ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON user_sessions (expires_at);

-- Brute-force throttling (see python/app/services/auth_service.py).
CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    ip_address   TEXT,
    successful   INTEGER NOT NULL DEFAULT 0 CHECK (successful IN (0, 1)),
    attempted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts (username, attempted_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    username    TEXT,
    action      TEXT    NOT NULL,     -- e.g. student.create, auth.login_failed
    entity_type TEXT,                 -- e.g. student, scan, settings
    entity_id   TEXT,
    details     TEXT,                 -- JSON payload, never secrets
    ip_address  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user    ON audit_logs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit_logs (action, created_at DESC);

-- ---------------------------------------------------------------------------
-- System configuration (runtime-editable; see routes_settings.py)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    value_type  TEXT NOT NULL DEFAULT 'string'
                        CHECK (value_type IN ('string', 'int', 'float', 'bool', 'json')),
    category    TEXT NOT NULL DEFAULT 'general',
    description TEXT,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_by  INTEGER,

    FOREIGN KEY (updated_by) REFERENCES users (id) ON DELETE SET NULL
);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('0001', 'initial schema: students, barcodes, scans, transactions, users, audit, settings');

-- ---------------------------------------------------------------------------
-- Reporting views
-- ---------------------------------------------------------------------------

-- Daily roll-up used by GET /api/reports/daily
CREATE VIEW IF NOT EXISTS v_daily_scan_summary AS
SELECT substr(scan_time, 1, 10)                       AS scan_date,
       COUNT(*)                                        AS total_scans,
       SUM(result = 'VERIFIED')                        AS verified,
       SUM(result = 'UNKNOWN_ID')                      AS unknown_id,
       SUM(result = 'INVALID_BARCODE')                 AS invalid_barcode,
       SUM(result = 'DUPLICATE_SCAN')                  AS duplicate_scan,
       SUM(result = 'UNSUPPORTED_FORMAT')              AS unsupported_format,
       SUM(result = 'ERROR')                           AS errors,
       ROUND(AVG(processing_time_ms), 2)               AS avg_processing_ms,
       ROUND(MAX(processing_time_ms), 2)               AS max_processing_ms,
       ROUND(AVG(confidence), 4)                       AS avg_confidence
FROM scan_logs
GROUP BY substr(scan_time, 1, 10);

-- Per-student roll-up used by the student scan history report
CREATE VIEW IF NOT EXISTS v_student_scan_history AS
SELECT st.id                                              AS student_pk,
       st.student_id                                      AS student_number,
       trim(st.first_name || ' ' || coalesce(st.middle_name || ' ', '') || st.last_name)
                                                          AS full_name,
       st.course,
       st.year_level,
       st.section,
       st.status                                          AS student_status,
       COUNT(sl.id)                                       AS total_scans,
       SUM(sl.result = 'VERIFIED')                        AS verified_scans,
       MAX(sl.scan_time)                                  AS last_scan_time,
       ROUND(AVG(sl.processing_time_ms), 2)               AS avg_processing_ms
FROM students st
LEFT JOIN scan_logs sl ON sl.student_id = st.id
GROUP BY st.id;

-- Cashier reconciliation view
CREATE VIEW IF NOT EXISTS v_transaction_detail AS
SELECT t.id                 AS transaction_id,
       t.reference_number,
       t.created_at,
       t.transaction_type,
       t.amount,
       t.quantity,
       t.status,
       t.reference_note,
       t.cashier_id,
       u.username           AS cashier_username,
       u.full_name          AS cashier_name,
       st.student_id        AS student_number,
       trim(st.first_name || ' ' || coalesce(st.middle_name || ' ', '') || st.last_name)
                              AS student_name,
       st.course,
       sl.barcode_value,
       sl.result            AS scan_result,
       sl.confidence,
       sl.processing_time_ms
FROM transactions t
LEFT JOIN students  st ON st.id = t.student_id
LEFT JOIN users     u  ON u.id  = t.cashier_id
LEFT JOIN scan_logs sl ON sl.id = t.scan_id;
