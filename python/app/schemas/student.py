"""Student and barcode schemas."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import EmailStr, Field, field_validator, model_validator

from app.schemas.common import ApiModel

StudentStatus = Literal["active", "inactive", "graduated", "suspended", "transferred"]

#: Student numbers are school-specific; the same expression is used for the
#: barcode payload (config `scanner.payload_pattern`).  Kept permissive so an
#: institution can use its own scheme without touching the validator.
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")

NameStr = Annotated[str, Field(min_length=1, max_length=100)]
ShortStr = Annotated[str, Field(max_length=120)]


class BarcodeOut(ApiModel):
    id: int
    barcode_value: str = Field(..., examples=["2026-000001"])
    barcode_type: str = Field(default="Code128", examples=["Code128"])
    label: str | None = None
    is_primary: bool = False
    retired_at: str | None = None
    created_at: str | None = None


class StudentBase(ApiModel):
    student_id: str = Field(
        ...,
        min_length=3,
        max_length=64,
        pattern=STUDENT_ID_RE.pattern,
        description="Human readable ID; this exact string is encoded in the barcode",
        examples=["2026-000001"],
    )
    first_name: NameStr
    last_name: NameStr
    middle_name: Annotated[str, Field(max_length=100)] | None = None
    course: ShortStr | None = Field(default=None, examples=["BS Computer Engineering"])
    year_level: Annotated[int, Field(ge=1, le=12)] | None = None
    section: Annotated[str, Field(max_length=16)] | None = Field(default=None, examples=["A"])
    school: ShortStr | None = None
    email: EmailStr | None = None
    phone: Annotated[str, Field(max_length=32)] | None = None
    status: StudentStatus = "active"
    notes: Annotated[str, Field(max_length=1000)] | None = None

    @field_validator("first_name", "last_name", "middle_name")
    @classmethod
    def _strip_names(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("name must not be empty")
        return cleaned


class StudentCreate(StudentBase):
    """POST /api/students"""

    photo_path: Annotated[str, Field(max_length=256)] | None = None
    barcodes: list["BarcodeCreate"] = Field(
        default_factory=list,
        description="Optional barcodes to register with the new student",
    )

    @model_validator(mode="after")
    def _at_most_one_primary(self) -> "StudentCreate":
        primaries = [b for b in self.barcodes if b.is_primary]
        if len(primaries) > 1:
            raise ValueError("only one barcode may be flagged as primary")
        return self


class StudentUpdate(ApiModel):
    """PUT /api/students/{student_id} - every field optional."""

    student_id: str | None = Field(default=None, min_length=3, max_length=64,
                                   pattern=STUDENT_ID_RE.pattern)
    first_name: NameStr | None = None
    last_name: NameStr | None = None
    middle_name: Annotated[str, Field(max_length=100)] | None = None
    course: ShortStr | None = None
    year_level: Annotated[int, Field(ge=1, le=12)] | None = None
    section: Annotated[str, Field(max_length=16)] | None = None
    school: ShortStr | None = None
    email: EmailStr | None = None
    phone: Annotated[str, Field(max_length=32)] | None = None
    status: StudentStatus | None = None
    notes: Annotated[str, Field(max_length=1000)] | None = None
    photo_path: Annotated[str, Field(max_length=256)] | None = None


class BarcodeCreate(ApiModel):
    barcode_value: str = Field(..., min_length=1, max_length=128,
                               pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    barcode_type: str = Field(default="Code128", max_length=32)
    label: Annotated[str, Field(max_length=80)] | None = None
    is_primary: bool = False


class BarcodeLookupOut(ApiModel):
    """GET /api/barcodes/{barcode}"""

    found: bool
    barcode_value: str
    barcode_type: str | None = None
    student: "StudentOut | None" = None


class StudentOut(ApiModel):
    id: int
    student_id: str
    first_name: str
    middle_name: str | None = None
    last_name: str
    full_name: str = Field(..., description="first [middle] last")
    course: str | None = None
    year_level: int | None = None
    section: str | None = None
    school: str | None = None
    email: str | None = None
    phone: str | None = None
    photo_path: str | None = Field(
        default=None, description="Relative path; fetch through /api/students/{id}/photo"
    )
    status: StudentStatus
    notes: str | None = None
    barcodes: list[BarcodeOut] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, student: "object") -> "StudentOut":
        return cls(
            id=student.id,  # type: ignore[attr-defined]
            student_id=student.student_id,  # type: ignore[attr-defined]
            first_name=student.first_name,  # type: ignore[attr-defined]
            middle_name=student.middle_name,  # type: ignore[attr-defined]
            last_name=student.last_name,  # type: ignore[attr-defined]
            full_name=student.full_name,  # type: ignore[attr-defined]
            course=student.course,  # type: ignore[attr-defined]
            year_level=student.year_level,  # type: ignore[attr-defined]
            section=student.section,  # type: ignore[attr-defined]
            school=student.school,  # type: ignore[attr-defined]
            email=student.email,  # type: ignore[attr-defined]
            phone=student.phone,  # type: ignore[attr-defined]
            photo_path=student.photo_path,  # type: ignore[attr-defined]
            status=student.status,  # type: ignore[attr-defined]
            notes=student.notes,  # type: ignore[attr-defined]
            barcodes=[
                BarcodeOut(
                    id=b.id,
                    barcode_value=b.barcode_value,
                    barcode_type=b.barcode_type,
                    label=b.label,
                    is_primary=b.is_primary,
                    retired_at=b.retired_at,
                    created_at=b.created_at,
                )
                for b in getattr(student, "barcodes", [])
            ],
            created_at=student.created_at,  # type: ignore[attr-defined]
            updated_at=student.updated_at,  # type: ignore[attr-defined]
        )


class StudentSummaryOut(ApiModel):
    """Condensed student block embedded in scan and transaction responses."""

    student_id: str
    name: str
    course: str | None = None
    year_level: int | None = None
    section: str | None = None
    status: str
    photo_path: str | None = None
    internal_id: int | None = Field(default=None, description="students.id, for follow-up calls")

    @classmethod
    def from_model(cls, student: "object") -> "StudentSummaryOut":
        return cls(
            student_id=student.student_id,  # type: ignore[attr-defined]
            name=student.full_name,  # type: ignore[attr-defined]
            course=student.course,  # type: ignore[attr-defined]
            year_level=student.year_level,  # type: ignore[attr-defined]
            section=student.section,  # type: ignore[attr-defined]
            status=student.status,  # type: ignore[attr-defined]
            photo_path=student.photo_path,  # type: ignore[attr-defined]
            internal_id=student.id,  # type: ignore[attr-defined]
        )


StudentCreate.model_rebuild()
BarcodeLookupOut.model_rebuild()
