"""Cashier transaction routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies import current_user, get_transactions
from app.schemas.common import MessageResponse, PageMeta, Paged
from app.schemas.transaction import (
    TransactionCreate,
    TransactionOut,
    TransactionStatusUpdate,
)
from app.services.auth_service import AuthenticatedUser
from app.services.transaction_service import TransactionService
from app.utils.config import get_config
from app.utils.errors import AppError, ErrorCode

_CONFIG = get_config()
DEFAULT_PAGE_SIZE = _CONFIG.reports.default_page_size
MAX_PAGE_SIZE = _CONFIG.reports.max_page_size
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/transactions", tags=["transactions"])


@router.post(
    "",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a transaction against a verified scan",
    responses={
        409: {"description": "A duplicate was rejected, or the workflow is not ready"},
    },
)
def create_transaction(
    payload: TransactionCreate,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    transactions: Annotated[TransactionService, Depends(get_transactions)],
) -> TransactionOut:
    """Walk ``READY_FOR_TRANSACTION -> TRANSACTION_PROCESSING -> SUCCESS``.

    The workflow is entered *before* the insert, which is what makes a double
    click harmless: the second request finds the machine already busy and is
    rejected with 409 instead of charging twice.  Every failure path lands in
    ``TRANSACTION_ERROR`` (or ``DUPLICATE_SCAN``) and never in ``SUCCESS`` -
    a failed transaction must not be displayed as a successful one.
    """
    user.require("transaction:create")

    from app.services.scan_workflow_service import get_workflow
    from app.services.transaction_service import DuplicateTransactionError

    workflow = get_workflow()
    # Raises 409 when the machine is busy or is not in a state that allows a
    # charge (e.g. no student has been found yet).
    workflow.begin_transaction()

    try:
        transaction = transactions.create(payload.model_dump(), cashier=user)
    except DuplicateTransactionError as exc:
        workflow.duplicate_transaction(exc.code, exc.message)
        raise
    except AppError as exc:
        workflow.transaction_failed(exc.code, exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 - never leave the machine busy
        logger.exception("transaction failed unexpectedly")
        workflow.transaction_failed(ErrorCode.UNKNOWN_ERROR)
        raise AppError(ErrorCode.UNKNOWN_ERROR, str(exc)) from exc

    workflow.transaction_succeeded(transaction)
    return transaction


@router.get("", response_model=Paged[TransactionOut], summary="Transaction history")
def list_transactions(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    transactions: Annotated[TransactionService, Depends(get_transactions)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    transaction_type: str | None = Query(default=None,
                                         pattern="^(Payment|Verification|Registration|Other)$"),
    status_filter: str | None = Query(default=None, alias="status",
                                      pattern="^(pending|completed|cancelled|refunded)$"),
    student_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> Paged[TransactionOut]:
    rows, total = transactions.list(
        start=start, end=end, transaction_type=transaction_type, status=status_filter,
        student_id=student_id, limit=limit, offset=offset,
    )
    return Paged[TransactionOut](
        items=rows,
        page=PageMeta(total=total, limit=limit, offset=offset,
                      has_more=offset + len(rows) < total),
    )


@router.get("/totals", summary="Revenue totals for a period")
def totals(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    transactions: Annotated[TransactionService, Depends(get_transactions)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    return transactions.totals(start, end)


@router.get(
    "/{reference}",
    response_model=TransactionOut,
    responses={404: {"description": "Unknown reference number"}},
    summary="Fetch one transaction",
)
def get_transaction(
    reference: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    transactions: Annotated[TransactionService, Depends(get_transactions)],
) -> TransactionOut:
    return transactions.get(reference)


@router.put(
    "/{reference}/status",
    response_model=TransactionOut,
    summary="Cancel or refund a transaction",
)
def set_status(
    reference: str,
    payload: TransactionStatusUpdate,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    transactions: Annotated[TransactionService, Depends(get_transactions)],
) -> TransactionOut:
    if payload.status in {"cancelled", "refunded"}:
        user.require("transaction:cancel")
    return transactions.set_status(reference, payload.status, cashier=user,
                                   reason=payload.reason)


@router.delete(
    "/{reference}",
    response_model=MessageResponse,
    summary="Cancel a transaction",
)
def cancel_transaction(
    reference: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    transactions: Annotated[TransactionService, Depends(get_transactions)],
) -> MessageResponse:
    user.require("transaction:cancel")
    transactions.set_status(reference, "cancelled", cashier=user, reason="deleted by cashier")
    return MessageResponse(message=f"Transaction {reference} cancelled")
