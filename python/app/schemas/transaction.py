"""Cashier transaction schemas."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel
from app.schemas.student import StudentSummaryOut

TransactionType = Literal["Payment", "Verification", "Registration", "Other"]
TransactionStatus = Literal["pending", "completed", "cancelled", "refunded"]


class TransactionCreate(ApiModel):
    """POST /api/transactions - always created from a verified scan.

    ``student_id`` is optional only for a ``Verification`` record, which may be
    logged for a card that failed to resolve (a "what was attempted" audit line).
    """

    scan_id: int | None = Field(default=None, description="The scan that produced this entry")
    student_id: Annotated[str, Field(max_length=64)] | None = Field(
        default=None, description="Human readable student number"
    )
    transaction_type: TransactionType = "Payment"
    amount: Annotated[Decimal, Field(ge=0, decimal_places=2, max_digits=12)] = Decimal("0.00")
    quantity: Annotated[int, Field(ge=1, le=1000)] = 1
    reference_note: Annotated[str, Field(max_length=500)] | None = None
    status: TransactionStatus = "completed"

    @field_validator("reference_note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None


class TransactionOut(ApiModel):
    id: int
    reference_number: str
    student_id: int | None = None
    student_number: str | None = None
    student: StudentSummaryOut | None = None
    scan_id: int | None = None
    transaction_type: TransactionType
    amount: float
    quantity: int
    cashier_id: int | None = None
    cashier_username: str | None = None
    status: TransactionStatus
    reference_note: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, txn: object) -> "TransactionOut":
        student = getattr(txn, "student", None)
        return cls(
            id=txn.id,  # type: ignore[attr-defined]
            reference_number=txn.reference_number,  # type: ignore[attr-defined]
            student_id=txn.student_id,  # type: ignore[attr-defined]
            student_number=getattr(student, "student_id", None),
            student=StudentSummaryOut.from_model(student) if student is not None else None,
            scan_id=txn.scan_id,  # type: ignore[attr-defined]
            transaction_type=txn.transaction_type,  # type: ignore[attr-defined]
            amount=txn.amount,  # type: ignore[attr-defined]
            quantity=txn.quantity,  # type: ignore[attr-defined]
            cashier_id=txn.cashier_id,  # type: ignore[attr-defined]
            cashier_username=getattr(txn, "cashier_username", None),
            status=txn.status,  # type: ignore[attr-defined]
            reference_note=txn.reference_note,  # type: ignore[attr-defined]
            created_at=txn.created_at,  # type: ignore[attr-defined]
            updated_at=txn.updated_at,  # type: ignore[attr-defined]
        )


class TransactionStatusUpdate(ApiModel):
    status: TransactionStatus
    reason: Annotated[str, Field(max_length=200)] | None = None
