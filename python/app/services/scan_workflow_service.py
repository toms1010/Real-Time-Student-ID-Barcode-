"""The cashier-facing half of the scan workflow.

:mod:`app.services.scan_workflow` owns the *rules*; this service owns a
*running instance* of them and the context that goes with it - the student that
was found, the scan row, the transaction, and the current error.  It is the only
place allowed to move the machine, so the invariants the brief asks for are
enforced in one spot rather than spread across the pipeline, the routes and the
UI:

* a student record is displayed only when a real record came back from the
  database (``student_found`` refuses a payload without one);
* a success is shown only when the backend confirmed the row was written
  (``transaction_succeeded`` refuses a payload without a reference number);
* returning to ``IDLE`` clears the previous student, so a stale card can never
  be charged;
* while the workflow is in ``TRANSACTION_PROCESSING`` a second submission is
  rejected with 409 instead of creating a duplicate charge.

Threading
---------
The camera thread (:class:`app.vision.pipeline.ScannerPipeline`) and the request
handlers both drive the same instance, so every mutation goes through
:meth:`ScanWorkflowService._move`, which holds one lock for the whole
read-modify-write.  One cashier workstation is one workflow; see ADR-012.
"""

from __future__ import annotations

import threading
from typing import Any

from app.services.scan_workflow import (
    ScanAction,
    ScanState,
    ScanStateMachine,
    actions_for,
    describe_states,
)
from app.utils.errors import ErrorCode, WorkflowConflictError
from app.utils.logger import get_logger
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)


class ScanWorkflowService:
    """A single running scan workflow."""

    def __init__(self, machine: ScanStateMachine | None = None) -> None:
        self.machine = machine or ScanStateMachine()
        self._lock = threading.RLock()
        self._student: Any = None
        self._scan: Any = None
        self._transaction: Any = None
        self._error_code: str | None = None
        self._error_message: str | None = None
        self._pending_barcode: str | None = None
        self._cooldown_remaining_ms: int = 0
        self._updated_at = utc_now_iso()

    # ------------------------------------------------------------------ read
    @property
    def state(self) -> ScanState:
        return self.machine.current

    @property
    def message(self) -> str:
        return self.machine.description().message

    @property
    def actions(self) -> list[str]:
        """The actions the cashier may take right now (busy states offer none)."""
        if self.machine.description().busy:
            return []
        return actions_for(self.machine.current)

    def may(self, action: ScanAction | str) -> bool:
        return self.machine.may(action)

    @property
    def student(self) -> Any:
        return self._student

    @property
    def scan(self) -> Any:
        return self._scan

    @property
    def transaction(self) -> Any:
        return self._transaction

    @property
    def error_code(self) -> str | None:
        return self._error_code

    @property
    def error_message(self) -> str | None:
        return self._error_message

    @property
    def updated_at(self) -> str:
        return self._updated_at

    @property
    def can_transact(self) -> bool:
        """A transaction may only be submitted from READY_FOR_TRANSACTION."""
        return self.machine.is_in(ScanState.READY_FOR_TRANSACTION)

    def snapshot(self) -> dict[str, Any]:
        """Everything a client needs to render the workflow.

        This is embedded in ``GET /api/scanner/state`` so the cashier screen
        needs a single poll, and it is also served on its own by
        ``GET /api/scanner/workflow``.
        """
        definition = self.machine.description()
        return {
            "state": str(self.machine.current),
            "label": definition.label,
            "message": self._error_message or definition.message,
            "default_message": definition.message,
            "phase": str(definition.phase),
            "busy": definition.busy,
            "actions": self.actions,
            "next_states": self.machine.as_dict()["next_states"],
            "can_transact": self.can_transact,
            "student": self._student,
            "scan_id": getattr(self._scan, "scan_id", None),
            "barcode": getattr(self._scan, "barcode", None) or self._pending_barcode,
            "result": getattr(self._scan, "result", None),
            "transaction": self._transaction,
            "error_code": self._error_code,
            "error_message": self._error_message,
            "cooldown_remaining_ms": self._cooldown_remaining_ms,
            "last_reason": self.machine.last_reason,
            "state_visits": self.machine.as_dict()["visits"],
            "rejected_transitions": self.machine.rejected,
            "history": self.machine.history_as_dicts(limit=12),
            "updated_at": self._updated_at,
        }

    @staticmethod
    def table() -> list[dict[str, Any]]:
        """The full state table, so the UI never hard-codes a transition rule."""
        return describe_states()

    # ----------------------------------------------------------------- write
    def _move(
        self,
        target: ScanState,
        reason: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        force: bool = False,
    ) -> bool:
        """The single mutation point.  Returns False when the move was rejected."""
        with self._lock:
            moved = self.machine.transition(target, reason, force=force)
            if moved:
                self._error_code = error_code
                self._error_message = error_message
                self._updated_at = utc_now_iso()
            return moved

    def _clear_student(self) -> None:
        """Rule 9.5.4: never carry a student record into a new scan."""
        self._student = None
        self._scan = None
        self._pending_barcode = None
        self._transaction = None

    # -- scan half: driven by the capture/decode threads -------------------
    def begin_scan(self, reason: str = "starting the camera") -> bool:
        with self._lock:
            if self.machine.is_in(ScanState.SUCCESS, ScanState.READY_FOR_TRANSACTION,
                                  ScanState.STUDENT_FOUND, ScanState.CAMERA_ERROR,
                                  ScanState.BARCODE_INVALID, ScanState.STUDENT_NOT_FOUND,
                                  ScanState.DATABASE_ERROR, ScanState.TRANSACTION_ERROR,
                                  ScanState.DUPLICATE_SCAN):
                # A new card invalidates whatever the previous one was doing.
                self._clear_student()
                self._move(ScanState.IDLE, "cleared for a new scan")
            return self._move(ScanState.CAMERA_INITIALIZING, reason)

    def camera_ready(self, reason: str = "camera opened") -> bool:
        return self._move(ScanState.SCANNING, reason)

    def camera_error(self, code: str | ErrorCode, message: str | None = None,
                     reason: str = "camera unavailable") -> bool:
        resolved = _as_code(code)
        text = message or resolved.user_message
        # A late frame-read failure after the operator stopped the camera must
        # not drag the workflow out of IDLE.
        if not self.machine.allows(ScanState.CAMERA_ERROR) and \
                self.machine.is_in(ScanState.IDLE):
            logger.debug("ignoring camera error while idle: %s", resolved)
            return False
        return self._move(ScanState.CAMERA_ERROR, reason,
                          error_code=str(resolved), error_message=text)

    def barcode_detected(self, payload: str = "", *, reason: str | None = None) -> bool:
        """A symbol is in frame.  ``payload`` may be empty for a symbol that was
        located but could not be read - the detection happened, the decode did
        not."""
        with self._lock:
            self._pending_barcode = payload or None
            return self._move(
                ScanState.BARCODE_DETECTED,
                reason or (f"symbol found: {payload}" if payload else "unreadable symbol"),
            )

    def can_accept_scan(self) -> bool:
        """Whether a new payload may enter the workflow from here.

        The decode loop asks this before doing the work: there is no point
        decoding a frame that the workflow could not accept anyway. It is
        ``False`` while a transaction is saving, and in ``CAMERA_ERROR`` and
        ``DATABASE_ERROR`` - where scanning another card cannot succeed, so the
        frame is dropped rather than decoded into another error.

        Uses exactly the same predicate as :meth:`scan_payload`, so the check
        and the walk can never disagree.
        """
        with self._lock:
            return self._entry_state() is not None

    def _entry_state(self) -> ScanState | None:
        """Which stage this state can accept a payload at.

        ``BARCODE_DETECTED`` when the camera is live, ``VALIDATING`` when the
        payload arrives without a frame behind it (typed, replayed, or posted),
        and ``None`` when the state cannot accept a scan at all.
        """
        for candidate in (ScanState.BARCODE_DETECTED, ScanState.SCANNING,
                          ScanState.VALIDATING):
            if self.machine.allows(candidate):
                return candidate
        return None

    def begin_detection(self, payload: str = "", *, reason: str | None = None) -> bool:
        """A symbol is in frame, and the workflow is about to decode it.

        When the machine is still holding the previous card's result
        (``STUDENT_FOUND``, ``DUPLICATE_SCAN``, ...) a new card supersedes it,
        so the workflow goes back to ``SCANNING`` first. Returning ``False``
        means the current state cannot accept a new symbol at all.
        """
        with self._lock:
            if not self.machine.allows(ScanState.BARCODE_DETECTED):
                if not self.machine.allows(ScanState.SCANNING):
                    return False
                self._move(ScanState.SCANNING, "a new symbol is in view")
            return self.barcode_detected(payload, reason=reason)

    def decoding(self, reason: str = "decoding the symbol") -> bool:
        return self._move(ScanState.DECODING, reason)

    def validating(self, reason: str = "validating the payload") -> bool:
        return self._move(ScanState.VALIDATING, reason)

    def look_up(self, payload: str, reason: str = "looking up the student") -> bool:
        with self._lock:
            self._pending_barcode = payload
            return self._move(ScanState.LOOKING_UP_STUDENT, reason)

    def barcode_invalid(self, code: str | ErrorCode, message: str | None = None,
                        reason: str = "payload rejected") -> bool:
        resolved = _as_code(code)
        return self._move(
            ScanState.BARCODE_INVALID, reason,
            error_code=str(resolved), error_message=message or resolved.user_message,
        )

    def student_found(self, scan: Any) -> bool:
        """Enter STUDENT_FOUND.  Refuses anything that is not a real record."""
        student = getattr(scan, "student", None)
        if student is None:
            logger.error(
                "refusing STUDENT_FOUND: the scan response carries no student record"
            )
            return False
        with self._lock:
            self._scan = scan
            self._student = student
            self._transaction = None
            return self._move(ScanState.STUDENT_FOUND,
                              f"verified {getattr(student, 'student_id', '?')}")

    def student_not_found(self, scan: Any, code: str | ErrorCode | None = None,
                          message: str | None = None) -> bool:
        resolved = _as_code(code or ErrorCode.STUDENT_NOT_FOUND)
        with self._lock:
            self._scan = scan
            self._student = None
            return self._move(
                ScanState.STUDENT_NOT_FOUND, "no record for the barcode",
                error_code=str(resolved),
                error_message=message or resolved.user_message,
            )

    def database_error(self, code: str | ErrorCode | None = None,
                       message: str | None = None) -> bool:
        """The database is unreachable.  No student is shown, none is invented."""
        resolved = _as_code(code or ErrorCode.DATABASE_ERROR)
        with self._lock:
            self._student = None
            self._transaction = None
            return self._move(
                ScanState.DATABASE_ERROR, "database unavailable",
                error_code=str(resolved), error_message=message or resolved.user_message,
            )

    def duplicate_scan(self, scan: Any, remaining_ms: int = 0) -> bool:
        resolved = ErrorCode.DUPLICATE_SCAN
        with self._lock:
            self._scan = scan
            self._cooldown_remaining_ms = max(0, int(remaining_ms))
            return self._move(
                ScanState.DUPLICATE_SCAN, "suppressed by the cooldown window",
                error_code=str(resolved),
                error_message=(
                    f"{resolved.user_message} Try again in "
                    f"{self._cooldown_remaining_ms} ms."
                ),
            )

    def resume_scanning(self, reason: str = "returning to scanning") -> bool:
        """The recovery move shared by BARCODE_INVALID, STUDENT_NOT_FOUND,
        DUPLICATE_SCAN and CAMERA_ERROR."""
        with self._lock:
            self._cooldown_remaining_ms = 0
            self._error_code = None
            self._error_message = None
            return self._move(ScanState.SCANNING, reason)

    def stop(self, reason: str = "scanner stopped") -> bool:
        with self._lock:
            self._clear_student()
            # `force` is deliberate: shutting down must always land in IDLE, even
            # from a state the table would otherwise not allow.
            moved = self.machine.transition(ScanState.IDLE, reason, force=True)
            self._error_code = None
            self._error_message = None
            self._updated_at = utc_now_iso()
            return moved

    # -- transaction half: driven by the API -------------------------------
    def ready_for_transaction(self, reason: str = "cashier chose to proceed") -> bool:
        if self.machine.is_in(ScanState.READY_FOR_TRANSACTION):
            return True
        if not self.machine.allows(ScanState.READY_FOR_TRANSACTION):
            self._reject("PROCEED", ScanState.READY_FOR_TRANSACTION)
            return False
        return self._move(ScanState.READY_FOR_TRANSACTION, reason)

    def begin_transaction(self, reason: str = "submitting the transaction") -> bool:
        """Enter TRANSACTION_PROCESSING, or raise.

        This is the guard behind *do not submit the same transaction twice
        while it is processing*: a second call from a busy or finished state is
        a 409, not a second charge.
        """
        with self._lock:
            if self.machine.is_in(ScanState.TRANSACTION_PROCESSING):
                raise WorkflowConflictError(
                    "A transaction is already being processed. Please wait.",
                    details={"state": str(self.machine.current)},
                )
            if not self.can_transact:
                # The state's own message is display copy ("Transaction
                # successful."), which would be nonsense as an error. Say what
                # is wrong and what to do instead.
                raise WorkflowConflictError(
                    f"A transaction cannot be submitted from {self.machine.current}. "
                    "Scan a verified ID and press Proceed first.",
                    details={"state": str(self.machine.current)},
                )
            return self._move(ScanState.TRANSACTION_PROCESSING, reason)

    def transaction_succeeded(self, transaction: Any) -> bool:
        """Enter SUCCESS.  Refuses anything the backend did not confirm."""
        reference = getattr(transaction, "reference_number", None)
        if not reference:
            logger.error("refusing SUCCESS: the service returned no reference number")
            return False
        with self._lock:
            self._transaction = transaction
            return self._move(ScanState.SUCCESS, f"recorded {reference}")

    def transaction_failed(self, code: str | ErrorCode,
                           message: str | None = None) -> bool:
        """A failed transaction is never presented as a success."""
        resolved = _as_code(code)
        with self._lock:
            self._transaction = None
            return self._move(
                ScanState.TRANSACTION_ERROR, "transaction could not be completed",
                error_code=str(resolved), error_message=message or resolved.user_message,
            )

    def duplicate_transaction(self, code: str | ErrorCode,
                              message: str | None = None) -> bool:
        resolved = _as_code(code)
        with self._lock:
            return self._move(
                ScanState.DUPLICATE_SCAN, "duplicate transaction rejected by the backend",
                error_code=str(resolved),
                error_message=message or resolved.user_message,
            )

    def cancel(self, reason: str = "cancelled by the cashier") -> bool:
        """Abandon the current student and return to the resting state."""
        with self._lock:
            self._clear_student()
            moved = self.machine.transition(ScanState.IDLE, reason, force=True)
            self._error_code = None
            self._error_message = None
            self._updated_at = utc_now_iso()
            return moved

    def retry_lookup(self) -> bool:
        """From DATABASE_ERROR, go back to the lookup with the same barcode."""
        if not self.machine.allows(ScanState.LOOKING_UP_STUDENT):
            self._reject("RETRY_LOOKUP", ScanState.LOOKING_UP_STUDENT)
            return False
        with self._lock:
            payload = getattr(self._scan, "barcode", None) or self._pending_barcode or ""
            return self._move(ScanState.LOOKING_UP_STUDENT, f"retrying lookup: {payload}")

    def retry_transaction(self) -> bool:
        """From TRANSACTION_ERROR, re-enable the form for the same student."""
        if not self.machine.allows(ScanState.READY_FOR_TRANSACTION):
            self._reject("RETRY_TRANSACTION", ScanState.READY_FOR_TRANSACTION)
            return False
        return self._move(ScanState.READY_FOR_TRANSACTION, "retrying the transaction")

    def _reject(self, action: str, target: ScanState) -> None:
        logger.warning(
            "action %s is not allowed from %s (wanted %s)",
            action, self.machine.current, target,
        )

    # -- the one scan path --------------------------------------------------
    def scan_payload(
        self,
        payload: str,
        *,
        scans: Any,
        barcode_type: str | None = None,
        confidence: float | None = None,
        source: str = "api",
        device_name: str | None = None,
        operator_id: int | None = None,
        processing_time_ms: float | None = None,
        detection_time_ms: float | None = None,
    ) -> Any:
        """Validate a payload, look the student up, and land in a terminal state.

        This is the **only** place a scan result becomes a workflow state, so
        the camera, the manual-entry form and a replay all behave identically.
        ``scans`` is the :class:`~app.services.scan_service.ScanService`; it is
        passed in rather than imported so this module stays free of a service
        dependency and can be unit-tested with a stub.

        The stages a submission skips are skipped honestly: when the camera is
        live the real detection stages are walked, and when the payload arrived
        without a frame behind it (typed, replayed or posted) validation is
        entered directly rather than claiming a camera event that never
        happened.  Returns the
        :class:`~app.schemas.scan.ScanVerification`, or ``None`` when the
        current state cannot accept the payload, in which case nothing is
        looked up and nothing is recorded.
        """
        with self._lock:
            entry = self._entry_state()
            if entry is None:
                logger.info("workflow is in %s; ignoring a submitted payload", self.state)
                return None
            if entry in (ScanState.BARCODE_DETECTED, ScanState.SCANNING):
                if not self.begin_detection(payload):
                    return None
                self.decoding()
                self.validating(f"checking the payload format: {payload}")
            else:
                self.validating(f"checking the payload format: {payload}")

        if not self.look_up(payload):
            logger.warning("could not enter LOOKING_UP_STUDENT from %s; no lookup made",
                           self.state)
            return None

        response = scans.process_scan(
            {
                "barcode": payload,
                "barcode_type": barcode_type,
                "confidence": confidence,
                "source": source,
                "device_name": device_name,
                "processing_time_ms": processing_time_ms,
                "detection_time_ms": detection_time_ms,
            },
            operator_id=operator_id,
        )
        self.apply_scan_response(response, scans=scans)
        return response

    def apply_scan_response(self, response: Any, *, scans: Any = None) -> None:
        """Map a :class:`ScanVerification` onto the terminal lookup state."""
        result = str(response.result)
        error_code = str(response.error_code or "")
        barcode = getattr(response, "barcode", None) or self._pending_barcode or ""
        if result == "VERIFIED":
            self.student_found(response)
        elif result == "DUPLICATE_SCAN":
            remaining = 0
            if scans is not None and barcode:
                remaining = scans.cooldowns.remaining_ms(f"barcode:{barcode}")
            self.duplicate_scan(response, remaining)
        elif ErrorCode.DATABASE_ERROR in error_code:
            # The database is unreachable: report DATABASE_ERROR and show no
            # student, rather than pretending the barcode is unregistered.
            self.database_error()
        elif result == "UNKNOWN_ID":
            self.student_not_found(response)
        else:
            # INVALID_BARCODE / UNSUPPORTED_FORMAT / ERROR: the payload never
            # produced a lookup, so it must not look like a registry miss.
            self.barcode_invalid(
                response.error_code or ErrorCode.INVALID_BARCODE, response.message
            )


def _as_code(code: str | ErrorCode) -> ErrorCode:
    if isinstance(code, ErrorCode):
        return code
    try:
        return ErrorCode(str(code))
    except ValueError:
        logger.warning("unknown error code %r; reporting it as UNKNOWN_ERROR", code)
        return ErrorCode.UNKNOWN_ERROR


_workflow: ScanWorkflowService | None = None
_workflow_lock = threading.Lock()


def get_workflow() -> ScanWorkflowService:
    """The process-wide workflow.

    A cashier workstation runs one screen, so the workflow is a singleton.  The
    alternative - one workflow per authenticated operator - would need the
    camera to be attributed to a specific session, which is not meaningful
    while the pipeline is process-wide (ADR-012).
    """
    global _workflow
    with _workflow_lock:
        if _workflow is None:
            _workflow = ScanWorkflowService()
        return _workflow


def set_workflow(workflow: ScanWorkflowService | None) -> None:
    """Test seam: replace or clear the singleton."""
    global _workflow
    with _workflow_lock:
        _workflow = workflow


__all__ = ["ScanWorkflowService", "get_workflow", "set_workflow"]
