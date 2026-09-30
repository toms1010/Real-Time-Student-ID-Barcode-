# The scan state machine

The barcode workflow is an explicit state machine with **seventeen states**. It
is specified once, in `python/app/services/scan_workflow.py`, and mirrored in the
C++ scanner and the cashier UI so all three components answer "what is
happening right now?" identically.

This document is the readable version of that table. The table in the code is
the authority; `scripts/check_state_mirror.py` fails the build if the three
copies disagree.

---

## 1. Why a state machine

The obvious alternative is a set of booleans — `scanning`, `verified`,
`processing`, `error`. That design cannot answer the questions the cashier
actually needs:

- May the *Confirm transaction* button be enabled? (Only one state allows it.)
- Is the current student still safe to charge, or was it cleared?
- Did that failed save leave the screen showing a stale "VERIFIED" banner?
- Where should the scanner go after a duplicate?

With one state, each of those is a lookup. With four booleans they are sixteen
combinations, most of which are meaningless and several of which are dangerous.
The state machine also makes the "no fake data" rules structural rather than
incidental: `STUDENT_FOUND` is only reachable from `LOOKING_UP_STUDENT`, and
only a real record can enter it, so a database outage cannot masquerade as a
student.

---

## 2. The states

### Happy path

| State | Purpose | Operator message |
| --- | --- | --- |
| `IDLE` | Nothing is being scanned. The resting state. | *Ready to Scan — Please position the student ID inside the scanning area.* |
| `CAMERA_INITIALIZING` | Connecting to the webcam, waiting for the first usable frame. | *Starting camera...* |
| `SCANNING` | The camera is live. Frames are preprocessed and decoded. **No database request happens here.** | *Scanning... Position the ID inside the box.* |
| `BARCODE_DETECTED` | A candidate symbol is in frame; its geometry is captured so the overlay stays locked on it. | *Barcode detected...* |
| `DECODING` | The decoder extracts the encoded value. | *Decoding barcode...* |
| `VALIDATING` | Reject malformed payloads **before any I/O**. | *Validating barcode...* |
| `LOOKING_UP_STUDENT` | The only state in which a database request is made. | *Checking student record...* |
| `STUDENT_FOUND` | A real record came back and may be shown. | *Student found.* |
| `READY_FOR_TRANSACTION` | The only state in which a transaction may be submitted. | *Ready to process the transaction.* |
| `TRANSACTION_PROCESSING` | The backend is saving. Cashier input is locked. | *Processing transaction... Please wait.* |
| `SUCCESS` | The backend confirmed the row was written. | *Transaction successful.* |

### Error states

| State | Triggered when | Operator message | Recovery |
| --- | --- | --- | --- |
| `CAMERA_ERROR` | The webcam cannot be opened or is disconnected. | *Camera unavailable. Please check the webcam connection.* | `RETRY_CAMERA` → `CAMERA_INITIALIZING` |
| `BARCODE_INVALID` | A symbol was seen but not decoded, or the payload failed validation. | *Invalid barcode. Please position the ID correctly and try again.* | `RESCAN` → `SCANNING` |
| `STUDENT_NOT_FOUND` | The barcode is well formed but no record matches. | *Student record not found. Please verify the ID and try again.* | `SCAN_AGAIN` → `SCANNING`, or `MANUAL_SEARCH` |
| `DATABASE_ERROR` | The backend cannot reach the database. | *Unable to access student records. Please try again or contact the administrator.* | `RETRY_LOOKUP` → `LOOKING_UP_STUDENT` |
| `TRANSACTION_ERROR` | A transaction could not be completed. | *Transaction failed. No transaction was recorded. Please try again.* | `RETRY_TRANSACTION` → `READY_FOR_TRANSACTION` |
| `DUPLICATE_SCAN` | The same barcode arrived inside the cooldown, or the backend rejected a duplicate charge. | *This ID was already scanned. Please wait or verify the transaction history.* | `SCAN_AGAIN` → `SCANNING` |

---

## 3. The transition table

A self-transition is always legal: a repeated camera frame must not be logged as
an error.

| From | To |
| --- | --- |
| `IDLE` | `CAMERA_INITIALIZING`, `VALIDATING` |
| `CAMERA_INITIALIZING` | `SCANNING`, `CAMERA_ERROR` |
| `SCANNING` | `BARCODE_DETECTED`, `CAMERA_ERROR`, `VALIDATING`, `IDLE` |
| `BARCODE_DETECTED` | `DECODING`, `BARCODE_INVALID`, `SCANNING` |
| `DECODING` | `VALIDATING`, `BARCODE_INVALID`, `DUPLICATE_SCAN` |
| `VALIDATING` | `LOOKING_UP_STUDENT`, `BARCODE_INVALID` |
| `LOOKING_UP_STUDENT` | `STUDENT_FOUND`, `STUDENT_NOT_FOUND`, `DATABASE_ERROR`, `BARCODE_INVALID`, `DUPLICATE_SCAN` |
| `STUDENT_FOUND` | `READY_FOR_TRANSACTION`, `SCANNING`, `IDLE` |
| `READY_FOR_TRANSACTION` | `TRANSACTION_PROCESSING`, `SCANNING`, `IDLE`, `TRANSACTION_ERROR` |
| `TRANSACTION_PROCESSING` | `SUCCESS`, `TRANSACTION_ERROR`, `DUPLICATE_SCAN` |
| `SUCCESS` | `IDLE`, `SCANNING` |
| `CAMERA_ERROR` | `CAMERA_INITIALIZING`, `IDLE` |
| `BARCODE_INVALID` | `SCANNING`, `IDLE` |
| `STUDENT_NOT_FOUND` | `SCANNING`, `IDLE` |
| `DATABASE_ERROR` | `LOOKING_UP_STUDENT`, `IDLE` |
| `TRANSACTION_ERROR` | `READY_FOR_TRANSACTION`, `IDLE` |
| `DUPLICATE_SCAN` | `SCANNING`, `IDLE` |

Anything not listed is rejected, logged at error level, and counted. The
previous state is kept, so a logic bug degrades rather than corrupts the
workflow.

### The two edges that are not in the brief's diagram

Both are additions, and both exist because a legitimate path arrives at a state
the diagram did not draw.

**`IDLE → VALIDATING` and `SCANNING → VALIDATING`.** A payload submitted through
the API — manual ID entry, a replay, or the C++ scanner posting on the
operator's behalf — never came from a frame. There is nothing to detect and
nothing to decode. Forcing those submissions through `BARCODE_DETECTED` and
`DECODING` would mean recording camera events that never happened, so
validation is entered directly instead. The stages that *did* happen are still
recorded, and `DECODING` is entered as soon as a real frame is involved.

**`DECODING → DUPLICATE_SCAN`.** The duplicate cooldown is checked the moment a
payload is in hand, before any lookup, so that is where a duplicate is
discovered. The Python service discovers it one step later (it asks the shared
cooldown map, inside the lookup, and arrives via `LOOKING_UP_STUDENT`); the C++
scanner suppresses duplicates locally at exactly the `DECODING` boundary, which
is why the edge exists.

---

## 4. Diagram

```text
                        ┌──────────────┐
                        │     IDLE     │
                        └──────┬───────┘
                               │
                ┌──────────────┴──────────────┐
                │                             │  (typed / replayed)
     ┌──────────▼───────────┐        ┌────────▼────────┐
     │ CAMERA_INITIALIZING  │        │    VALIDATING   │
     └──────────┬───────────┘        └────────┬────────┘
         ok │        │ error                 │ invalid
            │        │                       │
     ┌──────▼─────┐  │                       │
     │  SCANNING  │  │                       │
     └──────┬─────┘  │                       │
            │        │                       │
     ┌──────▼──────┐ │                       │
     │BARCODE_     │ │                       │
     │ DETECTED    │ │                       │
     └──────┬──────┘ │                       │
            │        │                       │
     ┌──────▼──────┐ │                       │
     │  DECODING   │ │                       │
     └──────┬──────┘ │                       │
            │        │                       │
            └────────┼───────────────────────┤
                     │                       │
            ┌────────▼────────┐              │
            │   LOOKING_UP_   │◄─────────────┤ (RETRY_LOOKUP)
            │    STUDENT      │              │
            └───┬────┬────┬───┘              │
      found     │    │    │  not found       │
   ┌────────────▼─┐  │    └──────────────┐   │
   │STUDENT_FOUND │  │                   │   │
   └────┬────┬────┘  │  db down          │   │
        │    │       │            ┌──────▼──────┐
        │    │       │            │DATABASE_ERR │
        │  cancel    │            └──────┬──────┘
        │    │       │                   │ IDLE
┌───────▼──┐ │  ┌────▼──────────┐   ┌────▼───────────┐
│  READY_  │ │  │TRANSACTION_   │   │STUDENT_NOT_    │
│TRANSACTION│ │ │  PROCESSING   │   │    FOUND       │
└─────┬────┘ │  └───┬───────┬───┘   └────┬───────────┘
      │      │      │       │            │
      │      │      │  fail │            │ SCAN_AGAIN
      │      │      ├───────┼────────────┼──► SCANNING
      │      │      │       │            │
      │      │  ┌───▼────┐  │  ┌─────────▼──┐
      │      │  │SUCCESS │  │  │DUPLICATE_  │
      │      │  └───┬────┘  │  │    SCAN    │
      │      │      │       │  └────────────┘
      │      │      └───────┼────────────┐
      │      │              │            │ SCAN_AGAIN
      │      │      ┌───────▼──────────┐ │
      │      │      │TRANSACTION_ERROR │ │
      │      │      └───────┬──────────┘ │
      │      │              │ RETRY_      │
      │      └──────────────┤ TRANSACTION │
      │                     └─────────────┘
      │
      └─► IDLE
```

---

## 5. Who owns what

| Component | File | Responsibility |
| --- | --- | --- |
| **Source of truth** | `python/app/services/scan_workflow.py` | The states, the transition table, the messages, the actions, and a `ScanStateMachine` that rejects illegal moves. Pure; no I/O. |
| **Running instance** | `python/app/services/scan_workflow_service.py` | Owns one machine plus the student, the scan, the transaction and the current error. The only place allowed to move the machine. Enforces the "no fake data" rules. |
| **Camera half** | `python/app/vision/pipeline.py` | Drives `IDLE → CAMERA_INITIALIZING → SCANNING → BARCODE_DETECTED → DECODING`, then hands the payload to the workflow. |
| **Transaction half** | `python/app/api/routes_transactions.py` | Enters `TRANSACTION_PROCESSING` *before* the insert, which is what makes a double click harmless. |
| **HTTP surface** | `python/app/api/routes_settings.py` | `GET /api/scanner/workflow` (state + actions + the full table) and `POST /api/scanner/workflow` (apply an action; 409 when the state forbids it). |
| **C++ scanner** | `cpp/include/scanner/ScannerState.h`, `cpp/src/scanner/ScannerState.cpp` | The same seventeen states and the same table, for the native overlay and its own tests. |
| **Cashier UI** | `frontend/src/machine/scanState.ts` | The same table, so the screen can render the right message and the right buttons without waiting for a poll. |
| **Guard** | `scripts/check_state_mirror.py` | Diffs the three copies. Run by `scripts/test.sh` before anything else. |

---

## 6. The rules the machine enforces

These are the requirements from section 9.5, and where each one is implemented.

| # | Rule | Where |
| --- | --- | --- |
| 1 | Do not query the database for every frame. | `ScannerPipeline._process_frame` only calls `ScanService` after a successful decode; an empty frame returns to `SCANNING` without any I/O. |
| 2 | Only look up after decoding **and** validating. | `VALIDATING` precedes `LOOKING_UP_STUDENT`, and no edge skips `VALIDATING`. |
| 3 | Prevent duplicates on both sides. | `ScanService.cooldowns` (`barcode:<payload>`) and `TransactionService`'s 3 s per-student debounce; both map to `DUPLICATE_SCAN`. |
| 4 | Clear the previous student on return to `IDLE`. | `ScanWorkflowService._clear_student`, called from `cancel()`, `stop()` and `begin_scan()`. |
| 5 | Never display a student the database did not return. | `student_found()` refuses a response with no `student`, and logs the refusal. |
| 6 | Never show success the backend did not confirm. | `transaction_succeeded()` refuses a transaction with no `reference_number`. |
| 7 | If the database is down, invent nothing. | `VerificationService.verify` catches `DatabaseError` and returns `DATABASE_ERROR`, which is a distinct state from `STUDENT_NOT_FOUND`. |
| 8 | If the camera disconnects, stop scanning. | The capture loop enters `CAMERA_ERROR`; a late read failure while `IDLE` is ignored so a stop does not drag the workflow out of rest. |
| 9 | Log every state change. | `ScanStateMachine.transition` logs every accepted move and every rejection. |
| 10 | The cashier must always be able to recover. | Every error state is `busy = False` and offers at least one action; asserted by the test suite in all three languages. |

---

## 7. Duplicate suppression, in two places

A card held in front of the lens is decoded on every frame, so a duplicate can
arrive in two different ways and is stopped in two different places:

1. **Frontend cooldown.** `ScanService.cooldowns` is keyed on the barcode value
   and suppresses a repeat inside `scanner.cooldown_ms`. The browser, the C++
   scanner and the pipeline all share this one map, so there is a single policy.
   At most one `DUPLICATE_SCAN` row is written per cooldown window
   (`scanner.duplicate_log_per_cooldown`), so a suppressed scan stays auditable
   without flooding `scan_logs`.
2. **Backend transaction check.** `TransactionService.create` additionally
   debounces per student for 3 seconds and raises `DuplicateTransactionError`
   (409 `DUPLICATE_SCAN`), because two *different* barcodes belonging to the
   same student, or a retried request, would both slip past a payload-keyed
   cooldown.

`CooldownTracker` in C++ is a third copy of the *first* rule only. It exists so
the native overlay stops re-rendering the same card thirty times a second even
when no request is made; it is not a substitute for the server check.

---

## 8. Keeping the three copies in step

Three hand-written copies of a table will drift. `scripts/check_state_mirror.py`
prevents that by parsing all three and comparing:

- the seventeen state names, and their order;
- every transition edge;
- every per-state operator message;
- every per-state action set;
- the `can_accept_scan` predicate, which decides whether the decode loop drops
  a frame outright.

It exits non-zero with a precise diff. It is step 1 of `scripts/test.sh`, before
the C++ and Python suites, because a mismatch there explains any downstream
state failure.

Each language also asserts the table in its own test suite, so the invariants
hold even if the checker is not run:

- `python/tests/test_scan_workflow.py` — 72 tests, including the invariants in
  the table above;
- `cpp/tests/test_scanner.cpp` — the 15 `state:` test cases.

Two of those tests are worth calling out because they caught real bugs while
this was being built:

- `test_can_accept_scan_agrees_with_what_scan_payload_will_do` drives the
  machine into **every** state and asserts that the decode-loop guard and the
  actual walk reach the same verdict. It found a missing `validating()` step.
- `test_a_state_that_cannot_succeed_refuses_the_card` asserts that no lookup is
  made in `CAMERA_ERROR`, `DATABASE_ERROR`, `TRANSACTION_ERROR`,
  `TRANSACTION_PROCESSING` or `LOOKING_UP_STUDENT` — that is, the frame is
  dropped rather than decoded into another error.

---

## 9. Legacy state names

Builds from before this machine reported a shorter vocabulary. It is still
accepted on the wire and coerced, so a stale browser bundle or an old native
binary is not mistaken for a bug:

| Legacy name | Canonical |
| --- | --- |
| `INITIALIZING` | `CAMERA_INITIALIZING` |
| `CAMERA_READY` | `SCANNING` |
| `VERIFYING` | `LOOKING_UP_STUDENT` |
| `VERIFIED` | `STUDENT_FOUND` |
| `UNKNOWN_ID` | `STUDENT_NOT_FOUND` |
| `ERROR` | `DATABASE_ERROR` |
| `CAMERA_DISCONNECTED` | `CAMERA_ERROR` |
| `STOPPED` | `IDLE` |

`coerce_state` in all three languages does the mapping. The canonical names are
what is emitted from now on.

---

## 10. Limitations

- **One workflow per process.** `get_workflow()` is a singleton, because a
  cashier workstation runs one screen. Two operators sharing a machine would
  see each other's state. See ADR-012.
- **The transaction states are API-driven.** The C++ scanner owns the capture
  half of the machine; it does not record transactions, so it never enters
  `READY_FOR_TRANSACTION` or beyond. That is correct, not a gap.
- **No persistence.** The machine lives in memory. Restarting the service
  returns it to `IDLE`; the durable record is `scan_logs` and `transactions`.
