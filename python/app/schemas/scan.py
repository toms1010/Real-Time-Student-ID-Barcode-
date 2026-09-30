"""Scan and verification schemas.

The verification response is the contract between the C++ scanner and the
Python service, so its shape is documented in ``docs/api-documentation.md`` and
covered by ``python/tests/test_scans.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel
from app.schemas.student import StudentSummaryOut
from app.schemas.transaction import TransactionOut

ScanResult = Literal[
    "VERIFIED",
    "UNKNOWN_ID",
    "INVALID_BARCODE",
    "DUPLICATE_SCAN",
    "UNSUPPORTED_FORMAT",
    "ERROR",
]


class ScanTiming(ApiModel):
    """Per-stage timings so the UI and the benchmark can show a breakdown."""

    detection_ms: float | None = Field(default=None, ge=0,
                                       description="capture -> detect + decode, measured by the scanner")
    decode_ms: float | None = Field(default=None, ge=0)
    database_ms: float | None = Field(default=None, ge=0,
                                      description="measured server side, lookup + validation")
    processing_ms: float | None = Field(default=None, ge=0,
                                        description="total for the frame (scanner side)")
    total_ms: float | None = Field(default=None, ge=0,
                                   description="detection + database, end to end for this barcode")


class ScanRequest(ApiModel):
    """POST /api/scans - the scanner's verification request."""

    barcode: str = Field(..., min_length=1, max_length=128,
                         description="Decoded barcode payload, e.g. '2026-000001'")
    timestamp: str | None = Field(
        default=None,
        description="ISO-8601 capture time from the scanner; the server time is used when omitted",
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    barcode_type: str | None = Field(default=None, max_length=32)
    device_name: str | None = Field(default=None, max_length=64)
    source: Literal["cpp", "python", "manual", "api"] = "cpp"
    processing_time_ms: Annotated[float, Field(ge=0.0)] | None = None
    detection_time_ms: Annotated[float, Field(ge=0.0)] | None = None
    frame_path: str | None = Field(default=None, max_length=256)
    # Manual entry path: a cashier may type the number when a card is damaged.
    operator_id: int | None = Field(default=None, description="Set by the server from the session")
    record: bool = Field(default=True, description="Store the attempt in scan_logs")
    advance_workflow: bool = Field(
        default=True,
        description=(
            "Move the scan state machine as well. The C++ scanner sets this to "
            "false when it owns the camera, so the two do not fight over the "
            "workflow; the browser leaves it on."
        ),
    )

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from app.utils.timeutil import format_iso, parse_iso

        return format_iso(parse_iso(value))  # type: ignore[return-value]


class ScanVerification(ApiModel):
    """The success shape shown on the cashier screen."""

    success: bool = Field(..., description="true only for result VERIFIED")
    result: ScanResult
    message: str = Field(..., description="Operator-facing text for the current state")
    scan_id: int | None = None
    barcode: str
    barcode_type: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    student: StudentSummaryOut | None = None
    can_transact: bool = Field(
        default=False,
        description="False for unknown, inactive, suspended or graduated students",
    )
    duplicate: bool = False
    error_code: str | None = None
    timing: ScanTiming = Field(default_factory=ScanTiming)
    scan_time: str


class ScanOut(ApiModel):
    """A row of scan_logs."""

    id: int
    student_id: int | None = None
    student_number: str | None = None
    student: StudentSummaryOut | None = None
    barcode_value: str
    scan_time: str
    result: ScanResult
    confidence: float | None = None
    barcode_type: str | None = None
    device_name: str | None = None
    source: str
    processing_time_ms: float | None = None
    detection_time_ms: float | None = None
    database_time_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    operator_id: int | None = None

    @classmethod
    def from_model(cls, scan: Any) -> "ScanOut":
        student = getattr(scan, "student", None)
        return cls(
            id=scan.id,
            student_id=scan.student_id,
            student_number=getattr(scan, "student_number", None),
            student=(
                StudentSummaryOut.from_model(student)
                if student is not None and getattr(student, "student_id", None)
                else None
            ),
            barcode_value=scan.barcode_value,
            scan_time=scan.scan_time,
            result=scan.result,
            confidence=scan.confidence,
            barcode_type=scan.barcode_type,
            device_name=scan.device_name,
            source=scan.source,
            processing_time_ms=scan.processing_time_ms,
            detection_time_ms=scan.detection_time_ms,
            database_time_ms=scan.database_time_ms,
            error_code=scan.error_code,
            error_message=scan.error_message,
            operator_id=scan.operator_id,
        )


class ScanSummary(ApiModel):
    total: int = 0
    verified: int = 0
    unknown_id: int = 0
    invalid_barcode: int = 0
    duplicate_scan: int = 0
    unsupported_format: int = 0
    errors: int = 0
    avg_processing_ms: float | None = None
    max_processing_ms: float | None = None
    avg_confidence: float | None = None


class WorkflowActionIn(ApiModel):
    """A cashier action posted to ``POST /api/scanner/workflow``.

    The value is validated against the machine, so an action that the current
    state does not allow is rejected with 409 rather than silently ignored.
    """

    action: str = Field(
        ...,
        min_length=3,
        max_length=40,
        description=(
            "One of START_CAMERA, STOP_CAMERA, RETRY_CAMERA, RESCAN, SCAN_AGAIN, "
            "MANUAL_SEARCH, PROCEED, CANCEL, RETRY_LOOKUP, RETRY_TRANSACTION, "
            "ACKNOWLEDGE or VIEW_HISTORY."
        ),
        examples=["PROCEED"],
    )


class ScanWorkflowOut(ApiModel):
    """The canonical scan workflow, as served by ``GET /api/scanner/workflow``.

    Also embedded under ``workflow`` in the ``GET /api/scanner/state`` payload,
    so the cashier screen needs a single poll.
    """

    state: str = Field(..., examples=["READY_FOR_TRANSACTION"])
    label: str = Field(..., examples=["Ready"])
    message: str = ""
    default_message: str = ""
    phase: str = Field(..., examples=["actionable"])
    busy: bool = False
    actions: list[str] = Field(default_factory=list)
    next_states: list[str] = Field(default_factory=list)
    can_transact: bool = False
    student: StudentSummaryOut | None = None
    scan_id: int | None = None
    barcode: str | None = None
    result: str | None = None
    transaction: TransactionOut | None = None
    error_code: str | None = None
    error_message: str | None = None
    cooldown_remaining_ms: int = 0
    last_reason: str = ""
    state_visits: dict[str, int] = Field(default_factory=dict)
    rejected_transitions: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: str | None = None


class WorkflowStateDefinitionOut(ApiModel):
    """One row of the state table, so the UI never hard-codes a rule."""

    state: str
    label: str
    phase: str
    purpose: str
    message: str
    entry: str
    exit: str
    error_handling: str = ""
    busy: bool = False
    actions: list[str] = Field(default_factory=list)
    next_states: list[str] = Field(default_factory=list)
