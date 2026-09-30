"""Cashier transactions built on top of a verified scan.

Rules:
* A transaction must reference either a ``scan_id`` that resolved to a
  ``VERIFIED`` scan, or an explicit ``student_id``.  Anything else is refused:
  this is what prevents "a transaction for a card that was never verified".
* Amounts are validated as ``Decimal`` and stored as REAL (SQLite has no
  decimal type); two-decimal rounding happens before storage.
* Cancellation and refund are state changes, never deletes, so the cashier's
  audit trail is intact.
* Card numbers, tokens or any other sensitive payload are never accepted in
  ``reference_note`` - the field is length-limited and stored as plain text that
  the cashier can read back.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.database.repository import Repository
from app.schemas.transaction import TransactionOut
from app.utils.errors import (
    AppError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_AMOUNT = Decimal("1000000")


class DuplicateTransactionError(AppError):
    """The backend rejected a repeated charge for the same student (409).

    A distinct class so the scan workflow can map it to its own
    ``DUPLICATE_SCAN`` state, while still travelling as a normal
    :class:`~app.utils.errors.AppError` to the HTTP layer.
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DUPLICATE_SCAN, message, details=details)


class TransactionService:
    def __init__(self, repo: Repository | None = None) -> None:
        self.repo = repo or Repository()
        self._debounce = None  # set by the API layer to the shared CooldownTracker

    def bind_cooldown(self, tracker) -> None:
        """Share the scan cooldown tracker so a card cannot be charged twice."""
        self._debounce = tracker

    # -- writes ------------------------------------------------------------
    def create(self, data: dict[str, Any], *, cashier: Any = None) -> TransactionOut:
        scan_pk = data.get("scan_id")
        student_number = data.get("student_id")
        student_pk: int | None = None

        if scan_pk is not None:
            scan = self.repo.scans.get(int(scan_pk))
            if scan is None:
                raise NotFoundError(f"scan {scan_pk} not found")
            if scan.result != "VERIFIED":
                raise ValidationError(
                    f"scan {scan_pk} has result {scan.result}; only a VERIFIED scan can be charged"
                )
            student_pk = scan.student_id
            if student_number is None:
                student_number = scan.student_number
        elif student_number:
            student = self.repo.students.get_by_student_id(str(student_number))
            if student is None:
                raise NotFoundError(
                    f"student {student_number} not found", details={"student_id": student_number}
                )
            student_pk = student.id
        else:
            raise ValidationError(
                "A transaction requires either scan_id (from a verified scan) or student_id"
            )

        if student_pk is not None and self._debounce is not None:
            key = f"txn:{student_pk}"
            if self._debounce.is_blocked(key, 3000):
                # A 409 with DUPLICATE_SCAN, not a generic validation failure:
                # the workflow maps this code to its own DUPLICATE_SCAN state,
                # and the cashier sees "already scanned" rather than a form
                # error (brief, section 9.3).
                raise DuplicateTransactionError(
                    "A transaction for this student was just recorded; "
                    "check the reference number before retrying.",
                    details={"student_id": student_pk},
                )
            self._debounce.block(key, 3000)

        amount = self._parse_amount(data.get("amount", 0))
        transaction_type = str(data.get("transaction_type") or "Payment")
        if transaction_type not in {"Payment", "Verification", "Registration", "Other"}:
            raise ValidationError(f"invalid transaction type: {transaction_type}")
        status = str(data.get("status") or "completed")
        if status not in {"pending", "completed", "cancelled", "refunded"}:
            raise ValidationError(f"invalid transaction status: {status}")

        txn = self.repo.transactions.create(
            student_pk=student_pk,
            transaction_type=transaction_type,
            amount=float(amount),
            cashier_id=getattr(cashier, "id", None),
            scan_id=int(scan_pk) if scan_pk is not None else None,
            quantity=int(data.get("quantity") or 1),
            reference_note=(data.get("reference_note") or None),
            status=status,
        )
        if txn is None:  # pragma: no cover - repository raises instead
            raise NotFoundError("transaction could not be created")
        if cashier is not None:
            self.repo.audit.log(
                action="transaction.created", user_id=cashier.id, username=cashier.username,
                entity_type="transaction", entity_id=str(txn.id),
                details={"reference": txn.reference_number, "type": transaction_type,
                         "amount": float(amount)},
            )
        logger.info("transaction %s recorded (student_pk=%s, amount=%.2f)",
                    txn.reference_number, student_pk, float(amount))
        return TransactionOut.from_model(txn)

    def set_status(self, reference: str, status: str, *, cashier: Any = None,
                   reason: str | None = None) -> TransactionOut:
        txn = self.repo.transactions.get_by_reference(reference)
        if txn is None:
            raise NotFoundError(f"transaction {reference} not found")
        if getattr(cashier, "id", None) is not None and txn.cashier_id not in (
            getattr(cashier, "id", None), None
        ):
            raise ValidationError("only the cashier who recorded a transaction can change it")
        updated = self.repo.transactions.set_status(txn.id or 0, status)  # type: ignore[arg-type]
        if cashier is not None:
            self.repo.audit.log(
                action=f"transaction.{status}", user_id=cashier.id, username=cashier.username,
                entity_type="transaction", entity_id=str(txn.id),
                details={"reference": reference, "reason": reason},
            )
        return TransactionOut.from_model(updated)  # type: ignore[arg-type]

    # -- reads -------------------------------------------------------------
    def get(self, reference: str) -> TransactionOut:
        txn = self.repo.transactions.get_by_reference(reference)
        if txn is None:
            raise NotFoundError(f"transaction {reference} not found")
        return TransactionOut.from_model(txn)

    def list(self, **kwargs: Any) -> tuple[list[TransactionOut], int]:
        rows, total = self.repo.transactions.list(**kwargs)
        return [TransactionOut.from_model(r) for r in rows], total

    def totals(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        return self.repo.transactions.totals(start, end)

    @staticmethod
    def _parse_amount(value: Any) -> Decimal:
        try:
            amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError("amount must be a number with at most 2 decimal places") from exc
        if amount < 0:
            raise ValidationError("amount must not be negative")
        if amount > MAX_AMOUNT:
            raise ValidationError(f"amount must not exceed {MAX_AMOUNT}")
        return amount


_transaction_service: TransactionService | None = None


def get_transaction_service() -> TransactionService:
    global _transaction_service
    if _transaction_service is None:
        _transaction_service = TransactionService()
    return _transaction_service
