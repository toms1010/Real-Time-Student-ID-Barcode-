# Screenshots

Real captures of the running system, taken by driving the cashier dashboard in a
headless browser against a live service (`./scripts/start.sh`, then sign in as
`cashier` / `DemoPass!2026`).

They are not mock-ups and they are not retouched. Two honest caveats:

* **The camera shows "Camera unavailable".** The capture machine had a webcam
  device node but no working V4L2 stream, so the pipeline correctly sat in
  `CAMERA_ERROR`. That is the intended behaviour for a disconnected camera, and
  it is why the workflow in these shots is driven through the **manual entry**
  field — which walks the same states a camera scan does
  (`VALIDATING → LOOKING_UP_STUDENT → …`).
* **`frame` and `detect` timings read `—`.** Those need a real camera.

Every shot shows the live workflow state, so they double as evidence for
`docs/architecture/scan-state-machine.md`.

## The scan workflow, end to end

| # | File | State | What it shows |
| --- | --- | --- | --- |
| 01 | [`01-login.png`](01-login.png) | — | The sign-in screen |
| 02 | [`02-student-found.png`](02-student-found.png) | `STUDENT_FOUND` | A verified card. The only actions offered are *Proceed*, *Cancel* and *Scan another ID*; the transaction form is locked with "The cashier must press Proceed before a transaction can be submitted." |
| 03 | [`03-ready-for-transaction.png`](03-ready-for-transaction.png) | `READY_FOR_TRANSACTION` | After *Proceed*. `SUBMIT_TRANSACTION` is now offered and the form is enabled. |
| 04 | [`04-transaction-successful.png`](04-transaction-successful.png) | `SUCCESS` | The saved transaction: student, type, ₱100.00, reference `TXN-…`. Only shown because the backend confirmed the write. |
| 10 | [`10-scanner-at-rest.png`](10-scanner-at-rest.png) | `CAMERA_ERROR` | The error state, offering *Retry camera* and *Manual ID search*. |

## The rest of the application

| # | File | What it shows |
| --- | --- | --- |
| 05 | [`05-dashboard.png`](05-dashboard.png) | Overview: counts, the scan chart, live pipeline status |
| 06 | [`06-students.png`](06-students.png) | The student records the barcodes resolve to |
| 07 | [`07-scan-history.png`](07-scan-history.png) | The scan log — every attempt, successful or not |
| 08 | [`08-transactions.png`](08-transactions.png) | Recorded transactions with their reference numbers |
| 09 | [`09-reports.png`](09-reports.png) | Reporting and export |

## Recreating them

```bash
./scripts/start.sh
cd /tmp && python3 -m venv shotenv && ./shotenv/bin/pip install playwright
./shotenv/bin/python shots.py docs/screenshots
```

`shots.py` logs in, walks the workflow with real requests, and captures the
viewport (not `full_page`, which stitches `position: sticky` headers and
produces a doubled header).
