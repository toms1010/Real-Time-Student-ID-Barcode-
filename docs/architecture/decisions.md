# Architecture Decision Records

This file records the decisions that the two project briefs left open, and the
reasoning behind each choice. It is the reference used during the project
defense when a reviewer asks "why did you do it this way?".

---

## ADR-001 — Scanner is C++, business logic and UI are Python + a local web app

**Context.** The two briefs disagree on the presentation layer: one asks for a
Qt 6 desktop GUI, the other for a React cashier dashboard. Both are "optional"
in their own brief, and both carry different tool-chain risk.

**Decision.** The C++17 scanner (OpenCV capture + preprocessing + ZXing-C++
decoding) talks to a Python FastAPI service over plain HTTP on loopback. The
cashier and administrator interface is a React + TypeScript single-page app
served by that same FastAPI process, including an MJPEG preview of the live
camera feed.

**Reasons.**

* A single decoder stack (ZXing-C++) is used on both sides, so a barcode that
  the scanner decodes is the same barcode the API validates. There is no
  second, divergent decoder to keep in sync.
* The web UI is served by the backend, so the whole system is a single
  `localhost` origin: no CORS surface, no port juggling, works offline.
* Qt 6 would have added a native build dependency (`qt6-base-dev`,
  `libqt6svg`) and duplicated the dashboard work; a browser-based UI is
  inspectable during a defense and trivially responsive.
* HTML/CSS makes the verification states (VERIFIED / UNKNOWN / ERROR) easy to
  make high-contrast, which is the actual requirement in the UI section.

**Consequence.** The native OpenCV `highgui` window is still implemented and
usable (`student_id_scanner --mode=native`) for kiosks that must not depend on
a browser. It is an additional surface, not the primary one.

---

## ADR-002 — Decoded value is the `student_id` string, encoded as Code 128

**Context.** Barcode content could be the internal integer primary key, an
opaque serial, or the human-readable student number.

**Decision.** The barcode payload is the human-readable `students.student_id`
text, e.g. `2026-000123`, encoded in **Code 128** by default. The canonical
`student_id` also has a numeric form (`2026000123`) that is normalised, so a
card printed with either form verifies.

**Reasons.**

* A school can print the number already on the card; no separate mapping table
  or re-issue is required.
* Code 128 has built-in check digits and high error correction, and reads well
  from a webcam at low resolution.
* Human-readable values make demo cards, test fixtures and manual data entry
  auditable, which matters for an academic evaluation.

**Consequence.** `barcode_value` remains a separate table
(`student_barcodes`) so a student may hold several symbologies (legacy Code 39
card + new Code 128 card) at the same time.

---

## ADR-003 — Accepted symbologies are a configured allow-list

**Context.** ZXing-C++ can decode ~15 symbologies. Accepting everything would
let a QR code on a random poster resolve to a student number by accident.

**Decision.** `scanner.allowed_formats` (C++) and `ALLOWED_BARCODE_FORMATS`
(environment) default to `Code128, Code39, EAN13, EAN8, UPCA, UPCE, ITF,
QRCode, DataMatrix`. A payload decoded with a symbology outside the list is
logged as `UNSUPPORTED_FORMAT` and never reaches the database lookup.

**Consequence.** The `scan_logs.result` enum is the brief's five values plus
`UNSUPPORTED_FORMAT`. This is the only extension to the specified enum and is
called out in `docs/database-design.md`.

---

## ADR-004 — SQLite through a repository layer, PostgreSQL-ready but not claimed

**Context.** One brief wants SQLite for v1, the other wants PostgreSQL for
production and SQLite for development.

**Decision.** SQLite is the shipped, tested database. All SQL lives in
`python/app/database/repository.py` and `cpp/src/database/DatabaseClient.cpp`;
no query is written inside a route handler or service. The C++ scanner never
touches SQLite directly for verification — it uses the API, so the C++ side has
no schema knowledge at all.

**Reasons.** The repository boundary means a SQLAlchemy/psycopg repository can
be dropped in behind the same interface. Claiming PostgreSQL support without a
running PostgreSQL instance and a migration test would violate the project's
"no fabricated functionality" rule, so it is listed as future work instead.

**Consequence.** SQLite is opened in WAL mode with `foreign_keys=ON` and a
busy timeout; writes are short and the scanner reads concurrently.

---

## ADR-005 — Opaque session tokens instead of JWT

**Context.** The brief asks for "session/token management" without naming a
standard.

**Decision.** Login returns a 256-bit random token; only its SHA-256 hash is
stored in `user_sessions` with an expiry. The token travels in an
`HttpOnly; SameSite=Strict` cookie. Authorization is role-based
(`admin`, `cashier`, `staff`).

**Reasons.**

* Revocation is immediate: deleting the row logs the device out, which matters
  for a shared cashier workstation.
* No signing key to leak or rotate, and no algorithm-confusion class of bugs.
* Works fully offline with no external identity provider.

**Password hashing** is Argon2id via `argon2-cffi`, with the reference
parameters from the OWASP password storage cheat sheet.

---

## ADR-006 — Camera is owned by exactly one process at a time

**Context.** A V4L2 device can usually be opened by one process only. The web UI
and the native C++ window both want frames.

**Decision.** Capture is a single-owner resource. The service exposes a
`stream_source` setting with three modes:

| Mode | Owner | Used for |
| --- | --- | --- |
| `python` | FastAPI (`python/app/vision/`) | default, browser dashboard |
| `cpp` | `student_id_scanner --mode=service` | kiosk / hardware testing |
| `file` | either | replaying a recorded `.mp4` for reproducible tests |

When the C++ service mode is active it pushes annotated JPEG frames to
`POST /api/stream/frame`; FastAPI relays them as the MJPEG stream, so the
browser never has to know which process owns the device. Requesting a source
that is already owned returns `409` with `CAMERA_IN_USE` instead of silently
producing a black feed.

---

## ADR-007 — Preprocessing is adaptive, not unconditional

**Context.** The brief explicitly warns against blindly applying every
OpenCV filter. Applying `adaptiveThreshold` to a well-lit, sharp frame destroys
the quiet zone that Code 128 needs.

**Decision.** `ImagePreprocessor` runs a cascade: crop to the region of
interest -> resize to a target width -> grayscale -> optional CLAHE contrast
(enabled only when the frame's mean luminance is below a threshold) ->
optional denoise (only when high-frequency noise energy is above a threshold) ->
optional adaptive threshold (only when the frame is bimodal *and* the previous
frame failed to decode). Every step is individually switchable from
`config.yaml` and each is reported in the per-frame timing breakdown.

**Consequence.** `scanner.preprocess_always_adaptive_threshold` exists purely
for A/B measurement during the lighting study; the default is `false`.

---

## ADR-008 — Cooldown is keyed on the barcode value, and expires

**Context.** A student may legitimately scan twice (two meals a day) but must
not be logged 30 times a second while the card sits in front of the lens.

**Decision.** After a scan is recorded, that `barcode_value` is suppressed for
`scanner.cooldown_ms` (default 2000 ms). The suppression map is in-memory,
bounded (`max_tracked_barcodes`, default 512, LRU eviction) and dropped on
restart, because the durable record is `scan_logs`.

**Consequence.** A `DUPLICATE_SCAN` result is *not* written to `scan_logs` on
every frame (that would be thousands of rows a minute). The suppression is
reported through `/api/scans/current` and the UI shows "duplicate suppressed",
and a `DUPLICATE_SCAN` row is written once per cooldown window per barcode so
that duplicate attempts remain auditable.

---

## ADR-009 — Photos are optional, validated, and never required for verification

**Context.** Privacy requirement: minimise personal data.

**Decision.** `students.photo_path` is nullable. Uploads are limited to
2 MiB, restricted to `.jpg/.jpeg/.png` by extension *and* by decoded content
type, stored outside the web root under `uploads/photos/` with a random
filename, and served only through an authenticated endpoint. Deleting or
deactivating a student is a soft operation so the audit trail survives.

---

## ADR-010 — No fabricated measurements

Results documents use `[MEASURE]` placeholders. The benchmark harness
(`python/tests/performance/`, `student_id_scanner --benchmark`) emits real
numbers into `tests/performance/results/` which is git-ignored, and
`docs/performance-evaluation.md` documents the exact procedure to fill the
placeholders in.

---

## ADR-011 — The scan workflow is one explicit state machine, specified once

**Context.** The original implementation tracked progress with a free-form
`state` string on the pipeline, assigned in eight places, plus a second
guessing function in the UI (`panelState()` in `ScanResultPanel.tsx`) that
re-derived a headline from the scan result. Three problems followed:

* "May the cashier press *Confirm transaction*?" was answered by a
  conjunction of unrelated conditions spread over the page;
* `DUPLICATE_SCAN` was rendered with the "NOT FOUND" heading, because the panel
  lumped it in with `UNKNOWN_ID`; and
* nothing rejected an impossible transition — the string was simply assigned,
  so a bug silently produced a state the operator could act on.

**Decision.** Replace the string with an explicit machine of seventeen states
(`IDLE`, `CAMERA_INITIALIZING`, `SCANNING`, `BARCODE_DETECTED`, `DECODING`,
`VALIDATING`, `LOOKING_UP_STUDENT`, `STUDENT_FOUND`, `READY_FOR_TRANSACTION`,
`TRANSACTION_PROCESSING`, `SUCCESS`, and the six error states). Each state
carries a purpose, an entry behaviour, an operator message, its allowed
actions, its legal successors, its error handling and an exit behaviour.

The table lives in `python/app/services/scan_workflow.py` and is mirrored in
`cpp/include/scanner/ScannerState.h` and `frontend/src/machine/scanState.ts`.
An illegal transition is rejected, logged at error level and counted; the
previous state is kept. The full table is served by
`GET /api/scanner/workflow`, so the UI never hard-codes a rule.

The invariants become structural rather than incidental: `STUDENT_FOUND` is
reachable only from `LOOKING_UP_STUDENT` and only a real record can enter it, so
a database outage cannot masquerade as a student. `READY_FOR_TRANSACTION` is
the only state that permits a charge, and it is entered *before* the insert, so
a double click on *Confirm* is a 409 rather than a second charge.

**Alternatives rejected.** Scattered booleans cannot express "exactly one of
these sixteen combinations is legal". A pure front-end machine would drift from
the backend. One machine per component with no checker would drift from itself —
hence `scripts/check_state_mirror.py`, which diffs all three copies and runs
first in `scripts/test.sh`.

**Consequence.** Adding a state is a three-file change plus the checker. Legacy
state names are still accepted and coerced, so an old client is not mistaken for
a bug. See `docs/architecture/scan-state-machine.md`.

---

## ADR-012 — The workflow is a process-wide singleton, because the camera is

**Context.** The scan workflow has a transaction half, and the transaction half
is driven by an authenticated operator. Keying the machine per operator would
be tidier in principle.

**Decision.** `get_workflow()` returns one process-wide instance. The capture
pipeline is also process-wide and owns the camera, so a per-operator workflow
would have to claim ownership of a device that is not attributable to a session
— and the camera's state genuinely does belong to whoever is at the counter
right now.

**Consequence.** Two operators sharing one machine would see each other's
state. The deployment target is a single cashier workstation, which is stated in
the README's scope section. The machine is protected by one lock, and both the
capture thread and the request handlers mutate it, so a
per-operator split would need the camera to move to a dedicated process first.

---

## ADR-013 — A database outage is `DATABASE_ERROR`, not `UNKNOWN_ID`

**Context.** `VerificationService.verify` called `students.find_by_barcode`
without a `try`. When the database was unreachable the driver exception
propagated, and the workflow had no state for "the lookup failed" — only
`STUDENT_NOT_FOUND`, which reads to the cashier as "this student does not
exist". That is the most damaging possible misreading: the operator would
turn away a real student, or keep retrying a card that is perfectly valid.

**Decision.** The lookup is wrapped and a `DatabaseError` becomes a distinct
outcome carrying `error_code = DATABASE_ERROR`. It maps to its own
`DATABASE_ERROR` state, which offers *Try again* rather than *Scan again*, and
no student is shown and no placeholder record is created (brief rule 9.5.7).

The `scan_logs.result` vocabulary is unchanged — the row is stored as `ERROR`
with `error_code = 'DATABASE_ERROR'`, so the existing reporting views (which
already bucket `ERROR`) count it without a schema migration. The *state*
vocabulary is what gained the new name.

**Consequence.** A test asserts that `DATABASE_ERROR` cannot reach
`STUDENT_FOUND` or `READY_FOR_TRANSACTION`, in both the Python and the C++
table. The driver message is logged server-side and never returned to the
cashier.

---

## ADR-014 — Barcode confidence is reported as `null`, not invented

**Context.** The brief asks for a confidence figure per scan, and
`ScanVerification.confidence` is a nullable float.

**Decision.** ZXing-C++ validates every symbol it returns (a checksum for
EAN/UPC/Code 128, error correction for QR/DataMatrix) but does **not** expose a
numeric score. Rather than derive a pseudo-confidence from an unrelated signal
or hard-code a value, the decoder reports `None`.

**Consequence.** `confidence_threshold` in `scanner:` config is implemented and
enforced in `VerificationService.verify` for any caller that *does* supply a
score (a future decoder, or a hardware scanner), but the bundled ZXing-C++
pipeline always sends `None`, so the floor never rejects a symbol on its own.
A `valid=false` result carries `error_type` and surfaces as
`BARCODE_DECODE_FAILED`, which is the honest signal that something is wrong.
The results table in `docs/testing/test-results.md` therefore reports
"confident" as pass/fail per symbology rather than as an average score.
