"""Error taxonomy and the single JSON error shape used by the API.

The machine-readable codes are exactly those required by the brief plus a small
number of additions that the implemented behaviour needs (see
``ErrorCode``).  Every failure path in the C++ scanner and in the Python service
uses the same code strings, so a single log line can be traced from the camera
thread to the browser.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, documented error identifiers."""

    # --- camera / capture ---------------------------------------------------
    CAMERA_NOT_FOUND = "CAMERA_NOT_FOUND"
    CAMERA_PERMISSION_DENIED = "CAMERA_PERMISSION_DENIED"
    CAMERA_DISCONNECTED = "CAMERA_DISCONNECTED"
    CAMERA_IN_USE = "CAMERA_IN_USE"
    FRAME_CAPTURE_FAILED = "FRAME_CAPTURE_FAILED"

    # --- barcode -----------------------------------------------------------
    BARCODE_NOT_DETECTED = "BARCODE_NOT_DETECTED"
    BARCODE_DECODE_FAILED = "BARCODE_DECODE_FAILED"
    INVALID_BARCODE = "INVALID_BARCODE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"

    # --- lookup ------------------------------------------------------------
    STUDENT_NOT_FOUND = "STUDENT_NOT_FOUND"
    DUPLICATE_SCAN = "DUPLICATE_SCAN"

    # --- workflow ----------------------------------------------------------
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"

    # --- infrastructure ----------------------------------------------------
    DATABASE_ERROR = "DATABASE_ERROR"
    API_UNAVAILABLE = "API_UNAVAILABLE"
    FILE_ERROR = "FILE_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"

    # --- auth --------------------------------------------------------------
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    FORBIDDEN = "FORBIDDEN"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"

    # --- catch-all ---------------------------------------------------------
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS.get(self, 500)

    @property
    def user_message(self) -> str:
        """Operator-facing text.  Never contains SQL, paths or credentials."""
        return _USER_MESSAGES.get(self, "An unexpected error occurred.")


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.CAMERA_NOT_FOUND: 503,
    ErrorCode.CAMERA_PERMISSION_DENIED: 403,
    ErrorCode.CAMERA_DISCONNECTED: 503,
    ErrorCode.CAMERA_IN_USE: 409,
    ErrorCode.FRAME_CAPTURE_FAILED: 503,
    ErrorCode.BARCODE_NOT_DETECTED: 404,
    ErrorCode.BARCODE_DECODE_FAILED: 422,
    ErrorCode.INVALID_BARCODE: 422,
    ErrorCode.UNSUPPORTED_FORMAT: 422,
    ErrorCode.STUDENT_NOT_FOUND: 404,
    ErrorCode.DUPLICATE_SCAN: 409,
    ErrorCode.DATABASE_ERROR: 503,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.API_UNAVAILABLE: 503,
    ErrorCode.FILE_ERROR: 500,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_FAILED: 401,
    ErrorCode.AUTH_EXPIRED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.ACCOUNT_LOCKED: 429,
    ErrorCode.PASSWORD_CHANGE_REQUIRED: 403,
    ErrorCode.UNKNOWN_ERROR: 500,
}

_USER_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.CAMERA_NOT_FOUND: "Camera unavailable. Please check the webcam connection.",
    ErrorCode.CAMERA_PERMISSION_DENIED: "Camera access denied. Check the device permissions.",
    ErrorCode.CAMERA_DISCONNECTED: "Camera disconnected. Reconnecting...",
    ErrorCode.CAMERA_IN_USE: "The camera is already in use by another process.",
    ErrorCode.FRAME_CAPTURE_FAILED: "Frame capture failed. Please try again.",
    ErrorCode.BARCODE_NOT_DETECTED: "No barcode detected. Please position the ID inside the scanning area.",
    ErrorCode.BARCODE_DECODE_FAILED: "Invalid barcode. Please try scanning again.",
    ErrorCode.INVALID_BARCODE: "Invalid barcode. Please try scanning again.",
    ErrorCode.UNSUPPORTED_FORMAT: "Unsupported barcode format. Please use a Code 128 ID card.",
    ErrorCode.STUDENT_NOT_FOUND: "Student record not found. Please verify the ID.",
    ErrorCode.DUPLICATE_SCAN: "Duplicate scan suppressed by the cooldown period.",
    ErrorCode.DATABASE_ERROR: "Database connection failed. Please contact the system administrator.",
    ErrorCode.INVALID_STATE_TRANSITION: "The scanner is not ready for that action.",
    ErrorCode.API_UNAVAILABLE: "The local service is unavailable. Please contact the system administrator.",
    ErrorCode.FILE_ERROR: "File operation failed. Please contact the system administrator.",
    ErrorCode.VALIDATION_ERROR: "The submitted data is invalid.",
    ErrorCode.NOT_FOUND: "The requested record was not found.",
    ErrorCode.AUTH_REQUIRED: "Authentication required.",
    ErrorCode.AUTH_FAILED: "Invalid username or password.",
    ErrorCode.AUTH_EXPIRED: "Your session has expired. Please sign in again.",
    ErrorCode.FORBIDDEN: "You do not have permission to perform this action.",
    ErrorCode.ACCOUNT_LOCKED: "Too many failed attempts. Try again later.",
    ErrorCode.PASSWORD_CHANGE_REQUIRED: "You must change the initial password before continuing.",
    ErrorCode.UNKNOWN_ERROR: "An unexpected error occurred.",
}


class AppError(Exception):
    """Domain error carrying a stable :class:`ErrorCode`.

    Route handlers translate it into the uniform error body; the C++ client
    parses the same body.  ``details`` must never contain credentials, SQL text
    or filesystem paths.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = ErrorCode(code) if not isinstance(code, ErrorCode) else code
        self.message = message or self.code.user_message
        self.details = details or {}
        self.http_status = http_status or self.code.http_status
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": {
                "code": str(self.code),
                "message": self.message,
                "details": self.details,
            },
        }


class ValidationError(AppError):
    """Input validation failure (Pydantic-shaped, 422)."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, details=details)


class NotFoundError(AppError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, details=details)


class AuthError(AppError):
    def __init__(self, message: str, *, code: ErrorCode | str | None = None) -> None:
        super().__init__(code or ErrorCode.AUTH_FAILED, message)


class ForbiddenError(AppError):
    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.FORBIDDEN, message or ErrorCode.FORBIDDEN.user_message)


class DatabaseError(AppError):
    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DATABASE_ERROR, message, details=details)


class WorkflowConflictError(AppError):
    """An action was attempted from a state that does not allow it (409).

    Raised by :class:`app.services.scan_workflow_service.ScanWorkflowService`
    rather than a generic 500, because it is an expected outcome: a double
    click on *Confirm transaction*, or a confirm that arrives after the
    workflow already moved on.
    """

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_STATE_TRANSITION, message, details=details)


class FileError(AppError):
    """Upload / export file problem (wrong type, too large, not writable)."""

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.FILE_ERROR, message, details=details)
