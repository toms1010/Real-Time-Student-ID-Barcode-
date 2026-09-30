-- ===========================================================================
--  Demo seed data - clearly fictional records.
--
--  IMPORTANT
--  ----------
--  * Every person below is invented for demonstration. No real student's
--    personal information is stored in this repository (brief section 28).
--  * Addresses/phones use the reserved 555-01xx fictional range and the
--    example.test domain reserved by RFC 2606.
--  * The demo administrator password is a *documented demo value* stored as an
--    Argon2id hash; the account is flagged `must_change_password = 1` and the
--    server refuses `must_change_password` accounts for anything other than
--    changing the password.  Production deployments should create their own
--    account with `python -m app.cli create-admin` and never run this seed.
--  * Applied by:  python -m app.cli db-seed   (or scripts/setup.sh)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Students (12 demo records) + primary Code 128 barcodes
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO students
    (student_id, first_name, middle_name, last_name, course, year_level, section, school, email, phone, status)
VALUES
    ('2026-000001', 'Juan',    'Ramirez', 'Dela Cruz', 'BS Computer Engineering',     3, 'A', 'College of Engineering',   'juan.delacruz@example.test',  '+63-555-0101', 'active'),
    ('2026-000002', 'Maria',   'Lopez',   'Santos',    'BS Computer Engineering',     3, 'A', 'College of Engineering',   'maria.santos@example.test',  '+63-555-0102', 'active'),
    ('2026-000003', 'Angelo',  NULL,      'Reyes',     'BS Information Technology',   2, 'B', 'College of Computing',     'angelo.reyes@example.test',  '+63-555-0103', 'active'),
    ('2026-000004', 'Kristine','Bautista','Garcia',    'BS Nursing',                  3, 'C', 'College of Allied Health', 'kristine.garcia@example.test','+63-555-0104','active'),
    ('2026-000005', 'Paolo',   NULL,      'Mendoza',   'BS Business Administration',  4, 'A', 'School of Business',       'paolo.mendoza@example.test',  '+63-555-0105', 'active'),
    ('2026-000006', 'Bea',     'Tan',     'Villanueva','BS Accountancy',             2, 'B', 'School of Business',       'bea.villanueva@example.test', '+63-555-0106', 'active'),
    ('2026-000007', 'Carlo',   NULL,      'Aquino',    'BS Information Technology',   4, 'C', 'College of Computing',     'carlo.aquino@example.test',   '+63-555-0107', 'inactive'),
    ('2026-000008', 'Sofia',   'Cruz',    'Domingo',   'BS Computer Engineering',     1, 'A', 'College of Engineering',   'sofia.domingo@example.test',  '+63-555-0108', 'active'),
    ('2026-000009', 'Nico',    NULL,      'Farolano',  'BS Information Technology',   1, 'B', 'College of Computing',     'nico.farolano@example.test',  '+63-555-0109', 'active'),
    ('2026-000010', 'Trisha',  'Ocampo',  'Alvarez',   'BS Nursing',                  2, 'C', 'College of Allied Health', 'trisha.alvarez@example.test', '+63-555-0110', 'active'),
    ('2026-000011', 'Miguel',  NULL,      'Buenaventura','BS Mechanical Engineering', 3, 'B', 'College of Engineering',   'miguel.buenaventura@example.test','+63-555-0111','active'),
    ('2026-000012', 'Hannah',  'Lim',     'Salazar',   'BS Accountancy',             4, 'A', 'School of Business',       'hannah.salazar@example.test', '+63-555-0112', 'suspended');

-- One primary Code 128 barcode per student.  The payload equals student_id
-- (ADR-002).  A legacy Code 39 card is also registered for one student to
-- exercise the multi-barcode (1:N) relationship.
INSERT OR IGNORE INTO student_barcodes (student_id, barcode_value, barcode_type, label, is_primary)
SELECT s.id, s.student_id, 'Code128', 'Primary card', 1
FROM students s
WHERE s.student_id LIKE '2026-%';

INSERT OR IGNORE INTO student_barcodes (student_id, barcode_value, barcode_type, label, is_primary)
SELECT id, 'ID-' || student_id, 'Code39', 'Legacy card', 0
FROM students
WHERE student_id IN ('2026-000001', '2026-000004', '2026-000008');

-- ---------------------------------------------------------------------------
-- Users
--   Demo password for all three accounts:  DemoPass!2026
--   The admin account is flagged must_change_password = 1.
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO users (username, password_hash, full_name, role, must_change_password)
VALUES
    ('admin',   '$argon2id$v=19$m=65536,t=3,p=4$c3R1ZGVudC1kZW1vLXNhbHQ$PLACEHOLDER_REPLACED_AT_SEED_TIME', 'Demo Administrator', 'admin',   1),
    ('cashier', '$argon2id$v=19$m=65536,t=3,p=4$c3R1ZGVudC1kZW1vLXNhbHQ$PLACEHOLDER_REPLACED_AT_SEED_TIME', 'Demo Cashier',       'cashier', 0),
    ('staff',   '$argon2id$v=19$m=65536,t=3,p=4$c3R1ZGVudC1kZW1vLXNhbHQ$PLACEHOLDER_REPLACED_AT_SEED_TIME', 'Demo Staff',         'staff',   0);

-- ---------------------------------------------------------------------------
-- System settings
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO system_settings (key, value, value_type, category, description) VALUES
    ('application_name',        'Student ID Barcode Detection System', 'string', 'general',  'Name shown in the title bar and on reports'),
    ('theme',                   'dark',                                 'string', 'general',  'UI theme: dark | light'),
    ('camera_index',            '0',                                    'int',    'camera',   'V4L2 device index of the webcam'),
    ('camera_width',            '1280',                                 'int',    'camera',   'Requested capture width in pixels'),
    ('camera_height',           '720',                                  'int',    'camera',   'Requested capture height in pixels'),
    ('camera_fps',              '30',                                   'int',    'camera',   'Requested capture frame rate'),
    ('scan_cooldown_ms',        '2000',                                 'int',    'scanner',  'Suppress repeat scans of the same barcode for this long'),
    ('barcode_timeout_ms',      '3000',                                 'int',    'scanner',  'How long the scanner keeps trying to decode a held card'),
    ('confidence_threshold',    '0.80',                                 'float',  'scanner',  'Minimum decoder confidence accepted for a lookup'),
    ('barcode_pattern',         '^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$',     'string', 'scanner',  'Regular expression a decoded payload must match'),
    ('allowed_barcode_formats', 'Code128,Code39,EAN13,EAN8,UPCA,UPCE,ITF,QRCode,DataMatrix', 'string', 'scanner', 'Accepted symbologies'),
    ('sound_enabled',           'true',                                 'bool',   'audio',    'Play audio feedback on scan results'),
    ('sound_volume',            '0.8',                                  'float',  'audio',    'Playback volume 0.0 - 1.0'),
    ('success_sound',           'verification_success',                 'string', 'audio',    'Sound file played on VERIFIED'),
    ('error_sound',             'error',                                'string', 'audio',    'Sound file played on errors'),
    ('unknown_sound',           'unknown_student',                      'string', 'audio',    'Sound file played on UNKNOWN_ID'),
    ('scan_sound',              'scan_detected',                        'string', 'audio',    'Sound file played when a barcode decodes'),
    ('session_ttl_minutes',     '720',                                  'int',    'security', 'Session lifetime before re-login is required'),
    ('require_login',           'true',                                 'bool',   'security', 'Require authentication before using the UI');
