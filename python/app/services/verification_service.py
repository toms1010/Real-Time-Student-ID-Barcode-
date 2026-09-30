"""Verification: the business decision behind a decoded barcode.

The rules, in evaluation order:

1. **Empty payload** -> ``INVALID_BARCODE`` (rejected before any I/O).
2. **Payload pattern** (``scanner.payload_pattern``) -> ``INVALID_BARCODE``.
3. **Symbology allow-list** (``scanner.allowed_formats``) -> ``UNSUPPORTED_FORMAT``.
   A PDF417/QR poster on the counter must not resolve to a student.
4. **Confidence floor** (``scanner.confidence_threshold``) -> ``INVALID_BARCODE``.
5. **Student lookup** -> ``UNKNOWN_ID`` when the barcode is not registered, or
   ``ERROR`` / ``DATABASE_ERROR`` when the database itself is unreachable (an
   outage must never be reported as "no such student").
6. **Student status** - the scan is ``VERIFIED`` (the card *is* valid) but
   ``can_transact`` is ``False`` and a warning is returned, so the cashier sees
   exactly why the student cannot be served.

This service performs **no** scan logging and **no** cooldown handling; that is
:class:`app.services.scan_service.ScanService`'s job.  Keeping them separate is
what lets the API re-verify a payload without creating a log row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.database.models import Student
from app.services.student_service import StudentService
from app.utils.config import AppConfig, get_config
from app.utils.errors import DatabaseError, ErrorCode
from app.utils.logger import get_logger
from app.utils.security import validate_barcode_payload
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)

#: Synbology name -> zxing-cpp / ZXing-C++ format name.  The API speaks the
#: ZXing-C++ spelling because that is what the C++ scanner reports.
FORMAT_ALIASES: dict[str, str] = {
    "CODE128": "Code128",
    "CODE39": "Code39",
    "CODE93": "Code93",
    "CODABAR": "Codabar",
    "ITF": "ITF",
    "EAN13": "EAN13",
    "EAN8": "EAN8",
    "UPCA": "UPCA",
    "UPCE": "UPCE",
    "QRCODE": "QRCode",
    "QR": "QRCode",
    "DATAMATRIX": "DataMatrix",
    "DATABAR": "DataBar",
    "AZTEC": "Aztec",
    "PDF417": "PDF417",
    "MICROQRCODE": "MicroQRCode",
    "RMQRCODE": "RMQRCode",
    "MAXICODE": "MaxiCode",
    "DATABAROMNI": "DataBarOmni",
    "DATABARLTD": "DataBarLtd",
    "DATABAREXP": "DataBarExp",
    "CODE32": "Code32",
    "PZN": "PZN",
    "TELEPEN": "Telepen",
}


def normalize_format(value: str | None) -> str | None:
    """Map any accepted spelling of a symbology to its canonical name."""
    if not value:
        return None
    key = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    return FORMAT_ALIASES.get(key, str(value).strip() or None)


@dataclass(slots=True)
class VerificationOutcome:
    """Result of verifying one decoded payload."""

    result: str
    message: str
    barcode: str
    barcode_type: str | None = None
    confidence: float | None = None
    student: Student | None = None
    can_transact: bool = False
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)
    database_time_ms: float = 0.0
    verified_at: str = field(default_factory=utc_now_iso)

    @property
    def success(self) -> bool:
        return self.result == "VERIFIED"

    @property
    def student_pk(self) -> int | None:
        return self.student.id if self.student else None


class VerificationService:
    def __init__(
        self,
        students: StudentService | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.students = students or StudentService(config=config)
        self.config = config or get_config()

    @property
    def allowed_formats(self) -> set[str]:
        return {normalize_format(f) or f for f in self.config.scanner.allowed_formats}

    def verify(
        self,
        barcode: str | None,
        *,
        barcode_type: str | None = None,
        confidence: float | None = None,
        enforce_confidence: bool = True,
    ) -> VerificationOutcome:
        import time

        started = time.perf_counter()

        # 1. presence ---------------------------------------------------------
        if barcode is None or not str(barcode).strip():
            return self._fail(
                barcode or "",
                "INVALID_BARCODE",
                ErrorCode.INVALID_BARCODE,
                barcode_type=barcode_type,
                confidence=confidence,
            )

        # 2. payload shape ----------------------------------------------------
        try:
            payload = validate_barcode_payload(str(barcode), self.config.scanner.payload_pattern)
        except Exception as exc:  # noqa: BLE001 - any pattern failure is INVALID_BARCODE
            logger.info("rejected payload: %s", exc)
            return self._fail(
                str(barcode), "INVALID_BARCODE", ErrorCode.INVALID_BARCODE,
                barcode_type=barcode_type, confidence=confidence,
            )

        # 3. symbology allow-list --------------------------------------------
        canonical_type = normalize_format(barcode_type)
        if canonical_type and canonical_type not in self.allowed_formats:
            logger.info("payload %s decoded as %s which is not in the allow-list",
                        payload, canonical_type)
            return self._fail(
                payload, "UNSUPPORTED_FORMAT", ErrorCode.UNSUPPORTED_FORMAT,
                barcode_type=canonical_type, confidence=confidence,
            )

        # 4. confidence floor -------------------------------------------------
        threshold = self.config.scanner.confidence_threshold
        if enforce_confidence and confidence is not None and confidence < threshold:
            return self._fail(
                payload, "INVALID_BARCODE", ErrorCode.BARCODE_DECODE_FAILED,
                barcode_type=canonical_type, confidence=confidence,
                message=(
                    f"Barcode confidence {confidence:.2f} is below the configured "
                    f"threshold {threshold:.2f}."
                ),
            )

        # 5. lookup -----------------------------------------------------------
        try:
            _, student = self.students.find_by_barcode(payload)
        except DatabaseError as exc:
            # The database is unreachable.  Report it as DATABASE_ERROR rather
            # than as an unknown barcode: an unreachable database must never
            # look like "this student does not exist", and no placeholder
            # record is invented (brief rule 9.5.7).  The driver message is
            # logged, not returned.
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.error("database unavailable while verifying %s: %s", payload, exc)
            return VerificationOutcome(
                result="ERROR",
                message=ErrorCode.DATABASE_ERROR.user_message,
                barcode=payload,
                barcode_type=canonical_type,
                confidence=confidence,
                error_code=str(ErrorCode.DATABASE_ERROR),
                database_time_ms=elapsed_ms,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if student is None:
            logger.info("unknown barcode %s (%.2f ms)", payload, elapsed_ms)
            return VerificationOutcome(
                result="UNKNOWN_ID",
                message=ErrorCode.STUDENT_NOT_FOUND.user_message,
                barcode=payload,
                barcode_type=canonical_type,
                confidence=confidence,
                error_code=str(ErrorCode.STUDENT_NOT_FOUND),
                database_time_ms=elapsed_ms,
            )

        # 6. status -----------------------------------------------------------
        warnings: list[str] = []
        can_transact = student.is_active
        if student.status == "inactive":
            warnings.append("This student's record is inactive.")
        elif student.status == "suspended":
            warnings.append("This student's account is suspended.")
        elif student.status == "graduated":
            warnings.append("This student has already graduated.")
        elif student.status == "transferred":
            warnings.append("This student has transferred out.")

        outcome = VerificationOutcome(
            result="VERIFIED",
            message=(
                f"Verified: {student.full_name}"
                if not warnings
                else f"Card is valid but cannot transact - {warnings[0]}"
            ),
            barcode=payload,
            barcode_type=canonical_type,
            confidence=confidence,
            student=student,
            can_transact=can_transact,
            warnings=warnings,
            database_time_ms=elapsed_ms,
        )
        logger.info(
            "verified %s -> %s (status=%s, %.2f ms)",
            payload, student.student_id, student.status, elapsed_ms,
        )
        return outcome

    @staticmethod
    def _fail(
        barcode: str,
        result: str,
        code: ErrorCode,
        *,
        barcode_type: str | None = None,
        confidence: float | None = None,
        message: str | None = None,
    ) -> VerificationOutcome:
        return VerificationOutcome(
            result=result,
            message=message or code.user_message,
            barcode=barcode,
            barcode_type=barcode_type,
            confidence=confidence,
            error_code=str(code),
        )

    def supports_format(self, barcode_type: str | None) -> bool:
        canonical = normalize_format(barcode_type)
        return canonical is None or canonical in self.allowed_formats

    def describe_policy(self) -> dict[str, Any]:
        """Machine-readable summary, surfaced by GET /api/health for debugging."""
        return {
            "allowed_formats": sorted(self.allowed_formats),
            "payload_pattern": self.config.scanner.payload_pattern,
            "confidence_threshold": self.config.scanner.confidence_threshold,
            "cooldown_ms": self.config.scanner.cooldown_ms,
        }


_verification_service: VerificationService | None = None


def get_verification_service() -> VerificationService:
    global _verification_service
    if _verification_service is None:
        _verification_service = VerificationService()
    return _verification_service
