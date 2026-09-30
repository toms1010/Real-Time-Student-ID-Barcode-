"""Barcode lookup.

``GET /api/barcodes/{barcode}`` is the read-only "what does this card say?"
endpoint used by the cashier UI when a card is damaged and the number has to be
typed in, and by the support checklist in the user manual.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.dependencies import current_user, get_students
from app.schemas.student import BarcodeLookupOut, StudentOut
from app.services.auth_service import AuthenticatedUser
from app.services.student_service import StudentService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/barcodes", tags=["barcodes"])


@router.get(
    "/{barcode}",
    response_model=BarcodeLookupOut,
    summary="Resolve a barcode value to a student",
)
def lookup_barcode(
    barcode: Annotated[str, Path(min_length=1, max_length=128,
                                  description="Decoded barcode payload, URL-encoded")],
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    students: Annotated[StudentService, Depends(get_students)],
) -> BarcodeLookupOut:
    """Never raises for an unknown card: the caller needs to distinguish
    "not found" from "server error", so ``found`` is returned explicitly."""
    record, student = students.find_by_barcode(barcode)
    if record is None or student is None:
        return BarcodeLookupOut(found=False, barcode_value=barcode)
    return BarcodeLookupOut(
        found=True,
        barcode_value=record.barcode_value,
        barcode_type=record.barcode_type,
        student=StudentOut.from_model(student),
    )
