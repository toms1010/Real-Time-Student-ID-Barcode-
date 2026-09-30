"""The canonical barcode scan state machine.

This module is the **single source of truth** for the scanning workflow.  The
OpenCV pipeline, the REST API and the cashier UI all speak the same seventeen
states, the same transition table and the same operator-facing wording, so one
log line can be traced from the camera thread to the browser.

Design rules (from the project brief, section 9):

* **Explicit states, not scattered booleans.**  There is exactly one
  :class:`ScanState` at a time.  "Is the camera up?", "is a transaction in
  flight?" and "may the cashier press Confirm?" are all answered by asking
  *which state* the machine is in - see :func:`ScanDefinition.actions` and
  :func:`ScanStateMachine.allows`.
* **Every state has a job.**  Each :class:`ScanDefinition` carries a purpose, an
  entry behaviour, an operator message, the actions the cashier may take, the
  legal next states, the error handling and an exit behaviour.
* **Illegal transitions are rejected and logged.**  :meth:`ScanStateMachine.transition`
  returns ``False`` and keeps the previous state, so a bug surfaces in the log
  instead of silently corrupting the workflow.
* **The database is never faked.**  ``STUDENT_FOUND`` is only reachable from
  ``LOOKING_UP_STUDENT``, and only a real record can enter it, so a
  ``DATABASE_ERROR`` can never masquerade as a student.

The table is mirrored - deliberately, and tested against this file - by
``cpp/include/scanner/ScannerState.h`` and
``frontend/src/machine/scanState.ts``.  Keep the three in step.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from app.utils.logger import get_logger

logger = get_logger(__name__)


class ScanState(StrEnum):
    """Every state the scan workflow can be in."""

    # --- happy path ---------------------------------------------------------
    IDLE = "IDLE"
    CAMERA_INITIALIZING = "CAMERA_INITIALIZING"
    SCANNING = "SCANNING"
    BARCODE_DETECTED = "BARCODE_DETECTED"
    DECODING = "DECODING"
    VALIDATING = "VALIDATING"
    LOOKING_UP_STUDENT = "LOOKING_UP_STUDENT"
    STUDENT_FOUND = "STUDENT_FOUND"
    READY_FOR_TRANSACTION = "READY_FOR_TRANSACTION"
    TRANSACTION_PROCESSING = "TRANSACTION_PROCESSING"
    SUCCESS = "SUCCESS"

    # --- error states -------------------------------------------------------
    CAMERA_ERROR = "CAMERA_ERROR"
    BARCODE_INVALID = "BARCODE_INVALID"
    STUDENT_NOT_FOUND = "STUDENT_NOT_FOUND"
    DATABASE_ERROR = "DATABASE_ERROR"
    TRANSACTION_ERROR = "TRANSACTION_ERROR"
    DUPLICATE_SCAN = "DUPLICATE_SCAN"


class ScanAction(StrEnum):
    """Operator actions, i.e. the things the UI can offer in a given state."""

    START_CAMERA = "START_CAMERA"
    STOP_CAMERA = "STOP_CAMERA"
    RETRY_CAMERA = "RETRY_CAMERA"
    RESCAN = "RESCAN"
    SCAN_AGAIN = "SCAN_AGAIN"
    MANUAL_SEARCH = "MANUAL_SEARCH"
    PROCEED = "PROCEED"
    CANCEL = "CANCEL"
    RETRY_LOOKUP = "RETRY_LOOKUP"
    SUBMIT_TRANSACTION = "SUBMIT_TRANSACTION"
    RETRY_TRANSACTION = "RETRY_TRANSACTION"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    VIEW_HISTORY = "VIEW_HISTORY"


class ScanPhase(StrEnum):
    """Coarse grouping used for theming and for ``is_scanning``-style checks."""

    IDLE = "idle"
    ACQUIRING = "acquiring"
    DETECTING = "detecting"
    PROCESSING = "processing"
    RESOLVED = "resolved"
    ACTIONABLE = "actionable"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ScanDefinition:
    """Everything the system knows about one state."""

    state: ScanState
    purpose: str
    #: Operator-facing text.  Shown verbatim in the cashier UI and burned into
    #: the MJPEG overlay, so it must never contain a path, SQL or a stack trace.
    message: str
    #: Short label for the status chip.
    label: str
    phase: ScanPhase
    entry: str
    exit: str
    actions: frozenset[ScanAction] = frozenset()
    error_handling: str = ""
    #: A busy state accepts no cashier action at all: the workflow is between
    #: two points and the operator must wait.
    busy: bool = False


def _d(**kwargs: Any) -> ScanDefinition:
    return ScanDefinition(**kwargs)


#: The table.  Read it top to bottom: it *is* the specification of the workflow.
DEFINITIONS: dict[ScanState, ScanDefinition] = {
    ScanState.IDLE: _d(
        state=ScanState.IDLE,
        purpose="Nothing is being scanned. This is the resting state and the only "
                "state from which a new scan may begin.",
        message="Ready to Scan\n\nPlease position the student ID inside the scanning area.",
        label="Ready to scan",
        phase=ScanPhase.IDLE,
        entry="Clear the previous student record, the pending transaction and any error.",
        exit="Hand control to the camera acquisition step.",
        actions=frozenset({
            ScanAction.START_CAMERA, ScanAction.SCAN_AGAIN, ScanAction.MANUAL_SEARCH,
        }),
    ),
    ScanState.CAMERA_INITIALIZING: _d(
        state=ScanState.CAMERA_INITIALIZING,
        purpose="Connect to the webcam and wait for the first usable frame.",
        message="Starting camera...",
        label="Starting camera",
        phase=ScanPhase.ACQUIRING,
        entry="Open the capture device and drain the warm-up frames.",
        exit="Start decoding frames, or report why the device could not be opened.",
        busy=True,
        error_handling="Any open failure goes to CAMERA_ERROR with the device error code.",
    ),
    ScanState.SCANNING: _d(
        state=ScanState.SCANNING,
        purpose="The camera is live. Frames are preprocessed and decoded; no database "
                "request is made until a barcode has been decoded and validated.",
        message="Scanning...\nPosition the ID inside the box.",
        label="Scanning",
        phase=ScanPhase.DETECTING,
        entry="Show the live feed, the detection rectangle and the scan guide.",
        exit="Wait for a symbol to be found, or report a camera failure.",
        actions=frozenset({ScanAction.STOP_CAMERA, ScanAction.SCAN_AGAIN}),
        error_handling="A read failure goes to CAMERA_ERROR and stops the frame loop.",
    ),
    ScanState.BARCODE_DETECTED: _d(
        state=ScanState.BARCODE_DETECTED,
        purpose="A candidate symbol is in frame. Its geometry is captured so the "
                "overlay stays locked on it while the payload is extracted.",
        message="Barcode detected...",
        label="Barcode detected",
        phase=ScanPhase.DETECTING,
        entry="Freeze the detected corner points and stop issuing new lookups.",
        exit="Hand the symbol to the decoder.",
        busy=True,
    ),
    ScanState.DECODING: _d(
        state=ScanState.DECODING,
        purpose="The decoder extracts the encoded value from the symbol region.",
        message="Decoding barcode...",
        label="Decoding",
        phase=ScanPhase.PROCESSING,
        entry="Run the configured symbology set over the symbol.",
        exit="Pass the payload to validation, or report a decode failure.",
        busy=True,
        error_handling="An empty or unreadable payload goes to BARCODE_INVALID; it is "
                       "never sent to the database.",
    ),
    ScanState.VALIDATING: _d(
        state=ScanState.VALIDATING,
        purpose="Reject malformed payloads before any I/O: empty, wrong pattern, "
                "wrong length, disallowed characters, disallowed symbology.",
        message="Validating barcode...",
        label="Validating",
        phase=ScanPhase.PROCESSING,
        entry="Apply the payload pattern and the symbology allow-list.",
        exit="Send a payload that passed validation to the lookup step.",
        busy=True,
        error_handling="A payload that fails validation goes to BARCODE_INVALID.",
    ),
    ScanState.LOOKING_UP_STUDENT: _d(
        state=ScanState.LOOKING_UP_STUDENT,
        purpose="Ask the backend for the student behind the barcode. The only state "
                "in which a database request is made.",
        message="Checking student record...",
        label="Looking up student",
        phase=ScanPhase.PROCESSING,
        entry="POST the validated payload to /api/scans and await the record.",
        exit="Return the record, report an unknown barcode, or report a database fault.",
        busy=True,
        error_handling="No record -> STUDENT_NOT_FOUND. Database fault -> DATABASE_ERROR. "
                       "A payload inside the cooldown window -> DUPLICATE_SCAN.",
    ),
    ScanState.STUDENT_FOUND: _d(
        state=ScanState.STUDENT_FOUND,
        purpose="A real record came back from the database and may be shown to the "
                "cashier. Nothing else can populate the student panel.",
        message="Student found.",
        label="Student found",
        phase=ScanPhase.RESOLVED,
        entry="Show the student ID, name, course, year, section and status.",
        exit="Wait for the cashier to proceed, cancel or scan another card.",
        actions=frozenset({
            ScanAction.PROCEED, ScanAction.CANCEL, ScanAction.SCAN_AGAIN,
        }),
    ),
    ScanState.READY_FOR_TRANSACTION: _d(
        state=ScanState.READY_FOR_TRANSACTION,
        purpose="The student is identified and the cashier may now choose an action. "
                "The only state in which a transaction may be submitted.",
        message="Ready to process the transaction.",
        label="Ready",
        phase=ScanPhase.ACTIONABLE,
        entry="Enable the transaction form for the student that was found.",
        exit="Submit the transaction, cancel it, or go back to scanning.",
        actions=frozenset({
            ScanAction.PROCEED, ScanAction.CANCEL, ScanAction.SCAN_AGAIN,
            ScanAction.SUBMIT_TRANSACTION,
        }),
        error_handling="A failed submission goes to TRANSACTION_ERROR, never to SUCCESS.",
    ),
    ScanState.TRANSACTION_PROCESSING: _d(
        state=ScanState.TRANSACTION_PROCESSING,
        purpose="The backend validates and saves the transaction. Cashier input is "
                "locked so the same transaction cannot be submitted twice.",
        message="Processing transaction...\nPlease wait.",
        label="Processing",
        phase=ScanPhase.PROCESSING,
        entry="Disable every action; the submission is in flight.",
        exit="Confirm the save, or report the failure.",
        busy=True,
        error_handling="Any failure goes to TRANSACTION_ERROR. A duplicate detected by "
                       "the backend goes to DUPLICATE_SCAN.",
    ),
    ScanState.SUCCESS: _d(
        state=ScanState.SUCCESS,
        purpose="The backend confirmed the row was written. Only then may a success "
                "be shown, and the previous student must be cleared before the next scan.",
        message="Transaction successful.",
        label="Success",
        phase=ScanPhase.DONE,
        entry="Show the student, the transaction type, the amount and the reference.",
        exit="Return to IDLE, clearing the student record.",
        actions=frozenset({ScanAction.ACKNOWLEDGE, ScanAction.SCAN_AGAIN}),
    ),
    ScanState.CAMERA_ERROR: _d(
        state=ScanState.CAMERA_ERROR,
        purpose="The webcam could not be opened, or was disconnected mid-session. "
                "Scanning has stopped.",
        message="Camera unavailable.\n\nPlease check the webcam connection.",
        label="Camera error",
        phase=ScanPhase.ERROR,
        entry="Stop the frame loop and show the device error code.",
        exit="Re-open the camera, or fall back to manual entry.",
        actions=frozenset({
            ScanAction.RETRY_CAMERA, ScanAction.MANUAL_SEARCH, ScanAction.CANCEL,
        }),
        error_handling="Offer Retry Camera; the raw driver message is never shown.",
    ),
    ScanState.BARCODE_INVALID: _d(
        state=ScanState.BARCODE_INVALID,
        purpose="A symbol was seen but could not be decoded, or the payload failed "
                "validation. It is not a student ID and must not be looked up.",
        message="Invalid barcode.\n\nPlease position the ID correctly and try again.",
        label="Invalid barcode",
        phase=ScanPhase.ERROR,
        entry="Record the reason and clear any partial student data.",
        exit="Go back to scanning for another card.",
        actions=frozenset({ScanAction.RESCAN, ScanAction.CANCEL, ScanAction.MANUAL_SEARCH}),
        error_handling="Offer Rescan; an invalid payload is never stored as a student.",
    ),
    ScanState.STUDENT_NOT_FOUND: _d(
        state=ScanState.STUDENT_NOT_FOUND,
        purpose="The barcode is well formed but no student record matches it.",
        message="Student record not found.\n\nPlease verify the ID and try again.",
        label="Not found",
        phase=ScanPhase.ERROR,
        entry="Clear the student panel - there is nothing to show.",
        exit="Scan again, or look the student up by hand.",
        actions=frozenset({
            ScanAction.SCAN_AGAIN, ScanAction.MANUAL_SEARCH, ScanAction.CANCEL,
        }),
        error_handling="Offer Scan Again and Manual ID Search. A well-formed barcode "
                       "with no record is a registry gap, not a scanner fault, so it "
                       "is logged for the registrar rather than retried blindly.",
    ),
    ScanState.DATABASE_ERROR: _d(
        state=ScanState.DATABASE_ERROR,
        purpose="The backend could not reach the database. No student may be shown, "
                "and no temporary record may be invented.",
        message="Unable to access student records.\n\n"
                "Please try again or contact the administrator.",
        label="Database error",
        phase=ScanPhase.ERROR,
        entry="Clear the student panel and keep the barcode for a retry.",
        exit="Repeat the lookup, or return to idle.",
        actions=frozenset({ScanAction.RETRY_LOOKUP, ScanAction.CANCEL}),
        error_handling="The driver message is logged server-side only; the cashier sees "
                       "the generic text above.",
    ),
    ScanState.TRANSACTION_ERROR: _d(
        state=ScanState.TRANSACTION_ERROR,
        purpose="A transaction could not be completed. Nothing was recorded, and it "
                "must not be presented as a success.",
        message="Transaction failed.\n\nNo transaction was recorded.\nPlease try again.",
        label="Transaction failed",
        phase=ScanPhase.ERROR,
        entry="Clear any success presentation and state plainly that nothing was saved.",
        exit="Return to the transaction form, or cancel.",
        actions=frozenset({ScanAction.RETRY_TRANSACTION, ScanAction.CANCEL}),
        error_handling="Offer Retry against the same form. The student stays loaded so "
                       "the cashier does not have to re-scan the card, and no success "
                       "text is left on screen.",
    ),
    ScanState.DUPLICATE_SCAN: _d(
        state=ScanState.DUPLICATE_SCAN,
        purpose="The same barcode arrived inside the cooldown window, or the backend "
                "detected a duplicate transaction.",
        message="This ID was already scanned.\n\n"
                "Please wait or verify the transaction history.",
        label="Duplicate",
        phase=ScanPhase.ERROR,
        entry="Keep the previously found student visible and count the suppression.",
        exit="Return to scanning once the cooldown expires.",
        actions=frozenset({
            ScanAction.SCAN_AGAIN, ScanAction.VIEW_HISTORY, ScanAction.CANCEL,
        }),
        error_handling="Suppressed by the cooldown, so nothing was written. The countdown "
                       "to the next allowed scan is reported alongside the message; one "
                       "DUPLICATE_SCAN row per cooldown window keeps it auditable without "
                       "flooding scan_logs.",
    ),
}


#: The transition table.  A state may only move to what is listed here; the
#: self-transition is always legal so a repeated frame cannot be an error.
#:
#: Two edges are not in the brief's diagram, both because a legitimate path
#: arrives at a state the diagram did not draw:
#:
#: * ``IDLE -> VALIDATING`` and ``SCANNING -> VALIDATING``.  A payload
#:   submitted through the API - manual ID entry, a replay, or the C++ scanner
#:   posting on the operator's behalf - never came from a frame, so there is
#:   nothing to detect and nothing to decode.  Forcing those submissions through
#:   ``BARCODE_DETECTED`` would mean claiming a camera event that never
#:   happened, so validation is entered directly instead.  ``DECODING`` is
#:   entered by the pipeline as soon as a real frame is involved.
#: * ``DECODING -> DUPLICATE_SCAN``.  The duplicate cooldown is checked the
#:   moment a payload is in hand, before any lookup, so that is where a
#:   duplicate is discovered.  The C++ scanner suppresses duplicates locally at
#:   exactly this point, which is why the edge exists.
#: * ``CAMERA_ERROR -> VALIDATING``.  The state offers *Manual ID search*, so it
#:   has to accept one.  A typed payload did not come from a frame, so it enters
#:   validation directly for the same reason as the edge above - and rule 9.5.10
#:   requires that the cashier always has a way through an error.
TRANSITIONS: dict[ScanState, frozenset[ScanState]] = {
    ScanState.IDLE: frozenset({ScanState.CAMERA_INITIALIZING, ScanState.VALIDATING}),
    ScanState.CAMERA_INITIALIZING: frozenset({ScanState.SCANNING, ScanState.CAMERA_ERROR}),
    ScanState.SCANNING: frozenset({ScanState.BARCODE_DETECTED, ScanState.CAMERA_ERROR,
                                    ScanState.VALIDATING, ScanState.IDLE}),
    ScanState.BARCODE_DETECTED: frozenset({ScanState.DECODING, ScanState.BARCODE_INVALID,
                                            ScanState.SCANNING}),
    ScanState.DECODING: frozenset({ScanState.VALIDATING, ScanState.BARCODE_INVALID,
                                    ScanState.DUPLICATE_SCAN}),
    ScanState.VALIDATING: frozenset({ScanState.LOOKING_UP_STUDENT, ScanState.BARCODE_INVALID}),
    ScanState.LOOKING_UP_STUDENT: frozenset({
        ScanState.STUDENT_FOUND, ScanState.STUDENT_NOT_FOUND,
        ScanState.DATABASE_ERROR, ScanState.BARCODE_INVALID, ScanState.DUPLICATE_SCAN,
    }),
    ScanState.STUDENT_FOUND: frozenset({
        ScanState.READY_FOR_TRANSACTION, ScanState.SCANNING, ScanState.IDLE,
    }),
    ScanState.READY_FOR_TRANSACTION: frozenset({
        ScanState.TRANSACTION_PROCESSING, ScanState.SCANNING, ScanState.IDLE,
        ScanState.TRANSACTION_ERROR,
    }),
    ScanState.TRANSACTION_PROCESSING: frozenset({
        ScanState.SUCCESS, ScanState.TRANSACTION_ERROR, ScanState.DUPLICATE_SCAN,
    }),
    ScanState.SUCCESS: frozenset({ScanState.IDLE, ScanState.SCANNING}),
    ScanState.CAMERA_ERROR: frozenset({ScanState.CAMERA_INITIALIZING, ScanState.VALIDATING,
                                        ScanState.IDLE}),
    ScanState.BARCODE_INVALID: frozenset({ScanState.SCANNING, ScanState.IDLE}),
    ScanState.STUDENT_NOT_FOUND: frozenset({ScanState.SCANNING, ScanState.IDLE}),
    ScanState.DATABASE_ERROR: frozenset({ScanState.LOOKING_UP_STUDENT, ScanState.IDLE}),
    ScanState.TRANSACTION_ERROR: frozenset({ScanState.READY_FOR_TRANSACTION, ScanState.IDLE}),
    ScanState.DUPLICATE_SCAN: frozenset({ScanState.SCANNING, ScanState.IDLE}),
}


#: States in which the camera is expected to deliver frames.
CAMERA_STATES = frozenset({
    ScanState.CAMERA_INITIALIZING, ScanState.SCANNING, ScanState.BARCODE_DETECTED,
    ScanState.DECODING, ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
})

#: States in which a student record is on screen and may be charged.
STUDENT_STATES = frozenset({
    ScanState.STUDENT_FOUND, ScanState.READY_FOR_TRANSACTION,
    ScanState.TRANSACTION_PROCESSING, ScanState.SUCCESS, ScanState.DUPLICATE_SCAN,
})

#: States that end a scan attempt, success or failure.
TERMINAL_STATES = frozenset({
    ScanState.SUCCESS, ScanState.CAMERA_ERROR, ScanState.BARCODE_INVALID,
    ScanState.STUDENT_NOT_FOUND, ScanState.DATABASE_ERROR, ScanState.TRANSACTION_ERROR,
    ScanState.DUPLICATE_SCAN,
})

#: States where a frame may still arrive; used to decide whether a camera
#: failure is a real CAMERA_ERROR or a late report after stopping.
LIVE_STATES = CAMERA_STATES | frozenset({ScanState.STUDENT_FOUND,
                                         ScanState.READY_FOR_TRANSACTION,
                                         ScanState.TRANSACTION_PROCESSING,
                                         ScanState.SUCCESS,
                                         ScanState.BARCODE_INVALID,
                                         ScanState.STUDENT_NOT_FOUND,
                                         ScanState.DUPLICATE_SCAN,
                                         ScanState.DATABASE_ERROR,
                                         ScanState.TRANSACTION_ERROR})

#: Older scanner builds reported a shorter set of names.  They are still accepted
#: on the wire and on the C++ side so a stale client does not look like a bug.
LEGACY_STATE_ALIASES: dict[str, ScanState] = {
    "INITIALIZING": ScanState.CAMERA_INITIALIZING,
    "CAMERA_READY": ScanState.SCANNING,
    "VERIFYING": ScanState.LOOKING_UP_STUDENT,
    "VERIFIED": ScanState.STUDENT_FOUND,
    "UNKNOWN_ID": ScanState.STUDENT_NOT_FOUND,
    "ERROR": ScanState.DATABASE_ERROR,
    "CAMERA_DISCONNECTED": ScanState.CAMERA_ERROR,
    "STOPPED": ScanState.IDLE,
    # Scan *results* that the state machine also names.
    "INVALID_BARCODE": ScanState.BARCODE_INVALID,
    "TRANSACTION_COMPLETED": ScanState.SUCCESS,
}


def coerce_state(value: str | ScanState | None) -> ScanState | None:
    """Accept a canonical name, a legacy name, or ``None``."""
    if value is None:
        return None
    if isinstance(value, ScanState):
        return value
    text = str(value).strip().upper()
    if not text:
        return None
    if text in ScanState.__members__:
        return ScanState[text]
    return LEGACY_STATE_ALIASES.get(text)


def definition(state: ScanState | str) -> ScanDefinition:
    resolved = coerce_state(state)
    if resolved is None:
        raise ValueError(f"unknown scan state: {state!r}")
    return DEFINITIONS[resolved]


def is_allowed(from_state: ScanState, to_state: ScanState) -> bool:
    """Static legality check.  A self-transition is always allowed."""
    if from_state == to_state:
        return True
    return to_state in TRANSITIONS.get(from_state, frozenset())


def actions_for(state: ScanState | str) -> list[str]:
    """The actions the cashier may take in ``state``, in a stable order."""
    resolved = coerce_state(state)
    if resolved is None:
        return []
    return sorted(str(action) for action in DEFINITIONS[resolved].actions)


def next_states(state: ScanState | str) -> list[str]:
    resolved = coerce_state(state)
    if resolved is None:
        return []
    return sorted(str(nxt) for nxt in TRANSITIONS[resolved])


@dataclass(slots=True)
class TransitionRecord:
    """One entry of the workflow history, for the UI and for debugging."""

    from_state: str
    to_state: str
    reason: str
    at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "at": self.at,
        }


@dataclass(slots=True)
class ScanStateMachine:
    """A thread-safe machine that owns exactly one :class:`ScanState`.

    ``transition`` is the *only* way to change state.  An illegal transition is
    logged at error level, counted, and rejected - the previous state is kept so
    a logic error degrades instead of corrupting the workflow.
    """

    state: ScanState = ScanState.IDLE
    #: Log every accepted transition.  On by default during development
    #: (brief, rule 9.5.9); silenced for a kiosk build.
    trace: bool = True
    history_limit: int = 40
    history: list[TransitionRecord] = field(default_factory=list)
    visits: dict[str, int] = field(default_factory=dict)
    rejected: int = 0
    last_reason: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        self.visits = {str(state): 0 for state in DEFINITIONS}
        self.visits[str(self.state)] = 1

    # -- queries ------------------------------------------------------------
    @property
    def current(self) -> ScanState:
        with self._lock:
            return self.state

    def is_in(self, *states: ScanState) -> bool:
        with self._lock:
            return self.state in states

    def allows(self, *states: ScanState) -> bool:
        """Whether ``states`` may be entered from here (or we are already there)."""
        with self._lock:
            return any(is_allowed(self.state, candidate) for candidate in states)

    def may(self, action: ScanAction | str) -> bool:
        """Whether the cashier may trigger ``action`` right now."""
        with self._lock:
            resolved = coerce_state(action)
            definition_ = DEFINITIONS[self.state]
            if definition_.busy:
                return False
            if resolved is None:
                return str(action) in {str(item) for item in definition_.actions}
            return resolved in definition_.actions

    def description(self) -> ScanDefinition:
        return DEFINITIONS[self.current]

    # -- the only mutator ---------------------------------------------------
    def transition(
        self,
        next_state: ScanState | str,
        reason: str = "",
        *,
        force: bool = False,
    ) -> bool:
        """Move to ``next_state``.

        Returns ``True`` when the state changed (or was re-entered), ``False``
        when the transition was rejected.  ``force`` is for teardown paths that
        must always land, e.g. stopping the pipeline.
        """
        target = coerce_state(next_state)
        if target is None:
            self.rejected += 1
            logger.error("rejected transition to unknown state %r (%s)", next_state, reason)
            return False

        with self._lock:
            previous = self.state
            if not force and not is_allowed(previous, target):
                self.rejected += 1
                logger.error(
                    "illegal transition %s -> %s rejected (%s); keeping %s",
                    previous, target, reason or "no reason given", previous,
                )
                return False
            if previous == target:
                # A repeated frame or a retried action is not an error; refresh
                # the reason so the UI still reflects why we are here.
                self.last_reason = reason
                return True
            self.state = target
            self.last_reason = reason
            self.visits[str(target)] = self.visits.get(str(target), 0) + 1
            self.history.append(TransitionRecord(
                from_state=str(previous), to_state=str(target),
                reason=reason, at=time.time(),
            ))
            if len(self.history) > self.history_limit:
                del self.history[: len(self.history) - self.history_limit]

        if self.trace:
            logger.info("scan state %s -> %s (%s)", previous, target, reason or "-")
        return True

    # -- reporting ----------------------------------------------------------
    def history_as_dicts(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = self.history[-limit:] if limit else list(self.history)
            return [record.as_dict() for record in items]

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": str(self.state),
                "label": DEFINITIONS[self.state].label,
                "message": DEFINITIONS[self.state].message,
                "phase": str(DEFINITIONS[self.state].phase),
                "busy": DEFINITIONS[self.state].busy,
                "actions": sorted(str(a) for a in DEFINITIONS[self.state].actions),
                "next_states": sorted(str(s) for s in TRANSITIONS[self.state]),
                "last_reason": self.last_reason,
                "visits": dict(self.visits),
                "rejected_transitions": self.rejected,
            }


def describe_states() -> list[dict[str, Any]]:
    """The whole table, machine-readable.  Served by the API so the cashier UI
    never has to hard-code a transition rule."""
    return [
        {
            "state": str(state),
            "label": item.label,
            "phase": str(item.phase),
            "purpose": item.purpose,
            "message": item.message,
            "entry": item.entry,
            "exit": item.exit,
            "error_handling": item.error_handling,
            "busy": item.busy,
            "actions": sorted(str(action) for action in item.actions),
            "next_states": sorted(str(nxt) for nxt in TRANSITIONS[state]),
        }
        for state, item in DEFINITIONS.items()
    ]


def assert_table_is_consistent() -> None:
    """Self-check: every state has a definition and every edge points at a real
    state.  Called by the test suite; cheap enough to run at import time in a
    debug build, which is why it lives here rather than in the tests."""
    missing = [state for state in ScanState if state not in DEFINITIONS]
    if missing:
        raise AssertionError(f"states without a definition: {missing}")
    for state, targets in TRANSITIONS.items():
        unknown = [target for target in targets if target not in DEFINITIONS]
        if unknown:
            raise AssertionError(f"{state} transitions to undefined states: {unknown}")
    for state, item in DEFINITIONS.items():
        if state not in TRANSITIONS:
            raise AssertionError(f"{state} is missing from the transition table")
        if not item.purpose or not item.message or not item.label:
            raise AssertionError(f"{state} is missing purpose, message or label")
        if not item.entry or not item.exit:
            raise AssertionError(f"{state} is missing entry/exit behaviour")
        if item.phase is ScanPhase.ERROR and not item.error_handling:
            raise AssertionError(f"{state} is an error state with no error handling")
        if item.busy and item.actions:
            raise AssertionError(f"{state} is busy yet offers actions")


def state_names() -> Iterable[str]:
    return (str(state) for state in ScanState)


__all__ = [
    "CAMERA_STATES",
    "DEFINITIONS",
    "LEGACY_STATE_ALIASES",
    "LIVE_STATES",
    "STUDENT_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ScanAction",
    "ScanDefinition",
    "ScanPhase",
    "ScanState",
    "ScanStateMachine",
    "TransitionRecord",
    "actions_for",
    "assert_table_is_consistent",
    "coerce_state",
    "definition",
    "describe_states",
    "is_allowed",
    "next_states",
    "state_names",
]
