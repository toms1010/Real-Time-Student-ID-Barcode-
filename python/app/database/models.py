"""Row models and table metadata.

The service does not use an ORM.  Each table has a frozen dataclass describing
its columns, a ``from_row`` constructor, and an ``as_dict`` serializer.  The
repository returns these objects; Pydantic schemas in ``app/schemas`` convert
them to the wire format.  Keeping the three layers separate means a column
change touches the model, the repository SQL and the schema - not route code.

``COLUMNS`` is also used by ``python -m app.cli db-info`` to print a
human-readable data dictionary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

Row = dict[str, Any]


# ---------------------------------------------------------------------------
# students
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Student:
    id: int | None = None
    student_id: str = ""
    first_name: str = ""
    middle_name: str | None = None
    last_name: str = ""
    course: str | None = None
    year_level: int | None = None
    section: str | None = None
    school: str | None = None
    email: str | None = None
    phone: str | None = None
    photo_path: str | None = None
    status: str = "active"
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    #: Populated by the repository when barcodes are joined in.
    barcodes: list["Barcode"] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        middle = f" {self.middle_name}" if self.middle_name else ""
        return f"{self.first_name}{middle} {self.last_name}".strip()

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_row(cls, row: Row | None) -> "Student | None":
        if row is None:
            return None
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in row.items() if k in known}
        data.pop("barcodes", None)
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["barcodes"] = [b.as_dict() for b in self.barcodes]
        payload["full_name"] = self.full_name
        return payload


# ---------------------------------------------------------------------------
# student_barcodes
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Barcode:
    id: int | None = None
    student_id: int | None = None
    barcode_value: str = ""
    barcode_type: str = "Code128"
    label: str | None = None
    is_primary: bool = False
    created_at: str | None = None
    retired_at: str | None = None

    @classmethod
    def from_row(cls, row: Row | None) -> "Barcode | None":
        if row is None:
            return None
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in row.items() if k in known}
        if "is_primary" in data:
            data["is_primary"] = bool(data["is_primary"])
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# scan_logs
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScanLog:
    id: int | None = None
    student_id: int | None = None
    barcode_value: str = ""
    scan_time: str = ""
    result: str = "ERROR"
    confidence: float | None = None
    barcode_type: str | None = None
    device_name: str | None = None
    source: str = "cpp"
    processing_time_ms: float | None = None
    detection_time_ms: float | None = None
    database_time_ms: float | None = None
    frame_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    operator_id: int | None = None
    #: Human readable student number (students.student_id), NULL when unknown.
    student_number: str | None = None
    student: Student | None = None

    @classmethod
    def from_row(cls, row: Row | None) -> "ScanLog | None":
        if row is None:
            return None
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in row.items() if k in known}
        student_fields = {f.name for f in fields(Student)}
        nested = {k: v for k, v in data.items() if k not in known and k in student_fields}
        if nested:
            data["student"] = Student(**nested)
        else:
            data.pop("student", None)
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["student"] = self.student.as_dict() if self.student else None
        return payload


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Transaction:
    id: int | None = None
    reference_number: str = ""
    student_id: int | None = None
    scan_id: int | None = None
    transaction_type: str = "Payment"
    amount: float = 0.0
    quantity: int = 1
    cashier_id: int | None = None
    status: str = "completed"
    reference_note: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    student: Student | None = None
    cashier_username: str | None = None

    @classmethod
    def from_row(cls, row: Row | None) -> "Transaction | None":
        if row is None:
            return None
        known = {f.name for f in fields(cls)}
        student_fields = {f.name for f in fields(Student)}
        data: dict[str, Any] = {}
        student_data: dict[str, Any] = {}
        for key, value in row.items():
            if key in known:
                data[key] = value
            elif key in student_fields:
                student_data[key] = value
        if student_data:
            data["student"] = Student(**student_data)
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["student"] = self.student.as_dict() if self.student else None
        return payload


# ---------------------------------------------------------------------------
# users / sessions / audit
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class User:
    id: int | None = None
    username: str = ""
    password_hash: str = ""
    full_name: str | None = None
    role: str = "operator"
    is_active: bool = True
    must_change_password: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    last_login_at: str | None = None

    @classmethod
    def from_row(cls, row: Row | None) -> "User | None":
        if row is None:
            return None
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in row.items() if k in known}
        for flag in ("is_active", "must_change_password"):
            if flag in data:
                data[flag] = bool(data[flag])
        return cls(**data)

    def as_public_dict(self) -> dict[str, Any]:
        """Serialise for the API.  ``password_hash`` is never included."""
        payload = asdict(self)
        payload.pop("password_hash", None)
        return payload


@dataclass(slots=True)
class Session:
    id: int | None = None
    user_id: int = 0
    token_hash: str = ""
    created_at: str | None = None
    expires_at: str = ""
    revoked_at: str | None = None
    user_agent: str | None = None
    client_ip: str | None = None


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------
#: Data dictionary used by the CLI and the documentation generator.
TABLES: dict[str, dict[str, Any]] = {
    "students": {
        "model": Student,
        "description": "One row per enrolled student.",
        "columns": {
            "id": "INTEGER PK - internal surrogate key, never shown to students",
            "student_id": "TEXT UNIQUE - human readable ID printed on the card and encoded in the barcode (ADR-002)",
            "first_name": "TEXT NOT NULL",
            "middle_name": "TEXT - optional",
            "last_name": "TEXT NOT NULL",
            "course": "TEXT - programme, e.g. 'BS Computer Engineering'",
            "year_level": "INTEGER 1-12, nullable",
            "section": "TEXT - class section, e.g. 'A'",
            "school": "TEXT - college/school the student belongs to",
            "email": "TEXT - optional contact",
            "phone": "TEXT - optional contact",
            "photo_path": "TEXT - relative path under uploads/photos, nullable (privacy, ADR-009)",
            "status": "TEXT - active | inactive | graduated | suspended | transferred",
            "notes": "TEXT - free text, not shown on the cashier screen",
            "created_at": "TEXT - ISO-8601 UTC",
            "updated_at": "TEXT - ISO-8601 UTC",
        },
    },
    "student_barcodes": {
        "model": Barcode,
        "description": "Barcodes owned by a student (1:N).",
        "columns": {
            "id": "INTEGER PK",
            "student_id": "INTEGER FK -> students.id ON DELETE CASCADE",
            "barcode_value": "TEXT UNIQUE - decoded payload used for lookup",
            "barcode_type": "TEXT - symbology recorded when the card was issued",
            "label": "TEXT - e.g. 'Primary card' / 'Legacy card'",
            "is_primary": "INTEGER 0/1 - unique per student",
            "created_at": "TEXT - ISO-8601 UTC",
            "retired_at": "TEXT - set when a card is replaced",
        },
    },
    "scan_logs": {
        "model": ScanLog,
        "description": "Every scan attempt that reached the decoder, successful or not.",
        "columns": {
            "id": "INTEGER PK",
            "student_id": "INTEGER FK -> students.id ON DELETE SET NULL, NULL when unknown",
            "barcode_value": "TEXT NOT NULL - raw decoded payload",
            "scan_time": "TEXT - ISO-8601 UTC",
            "result": "TEXT - VERIFIED | UNKNOWN_ID | INVALID_BARCODE | DUPLICATE_SCAN | UNSUPPORTED_FORMAT | ERROR",
            "confidence": "REAL 0..1 - decoder score",
            "barcode_type": "TEXT - symbology the decoder reported",
            "device_name": "TEXT - camera/scanner identifier",
            "source": "TEXT - cpp | python | manual | api",
            "processing_time_ms": "REAL - total pipeline time for the frame",
            "detection_time_ms": "REAL - detect + decode time",
            "database_time_ms": "REAL - lookup time",
            "frame_path": "TEXT - optional stored frame, off by default (privacy)",
            "error_code": "TEXT - see app/utils/errors.py ErrorCode",
            "error_message": "TEXT - operator-facing text",
            "operator_id": "INTEGER FK -> users.id ON DELETE SET NULL",
        },
    },
    "transactions": {
        "model": Transaction,
        "description": "Cashier transactions created from a verified scan.",
        "columns": {
            "id": "INTEGER PK",
            "reference_number": "TEXT UNIQUE - e.g. TXN-20260930-3F9A2B",
            "student_id": "INTEGER FK -> students.id ON DELETE SET NULL",
            "scan_id": "INTEGER FK -> scan_logs.id ON DELETE SET NULL",
            "transaction_type": "TEXT - Payment | Verification | Registration | Other",
            "amount": "REAL >= 0",
            "quantity": "INTEGER >= 1",
            "cashier_id": "INTEGER FK -> users.id ON DELETE SET NULL",
            "status": "TEXT - pending | completed | cancelled | refunded",
            "reference_note": "TEXT - free text, never card numbers",
            "created_at": "TEXT - ISO-8601 UTC",
            "updated_at": "TEXT - ISO-8601 UTC",
        },
    },
    "users": {
        "model": User,
        "description": "Administrators, cashiers and staff.",
        "columns": {
            "id": "INTEGER PK",
            "username": "TEXT UNIQUE",
            "password_hash": "TEXT - Argon2id, never reversible",
            "full_name": "TEXT",
            "role": "TEXT - admin | cashier | staff | operator | viewer",
            "is_active": "INTEGER 0/1",
            "must_change_password": "INTEGER 0/1 - blocks all use except password change",
            "created_at": "TEXT",
            "updated_at": "TEXT",
            "last_login_at": "TEXT",
        },
    },
}
