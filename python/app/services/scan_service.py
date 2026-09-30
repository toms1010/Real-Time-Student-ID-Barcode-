"""Scan orchestration: cooldown suppression, logging, and the API response.

Flow for ``POST /api/scans``::

    validate payload  ->  cooldown check  ->  verify  ->  persist  ->  respond

Two behaviours are worth calling out (ADR-008):

* **Cooldown.**  A card held in front of the lens would otherwise produce ~30
  log rows per second.  After a scan is accepted, the same payload is
  suppressed for ``scanner.cooldown_ms``.  The map is in-memory, bounded, and
  dropped on restart; the durable record is ``scan_logs``.
* **Duplicate auditing.**  At most one ``DUPLICATE_SCAN`` row is written per
  cooldown window per payload (``scanner.duplicate_log_per_cooldown``), so a
  suppressed scan stays auditable without flooding the table.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from app.database.repository import Repository
from app.schemas.scan import ScanTiming, ScanVerification
from app.schemas.student import StudentSummaryOut
from app.services.verification_service import VerificationOutcome, VerificationService
from app.utils.config import AppConfig, get_config
from app.utils.errors import ValidationError
from app.utils.logger import get_logger
from app.utils.security import validate_barcode_payload
from app.utils.timeutil import utc_now_iso

logger = get_logger(__name__)


class CooldownTracker:
    """Thread-safe payload suppressor with LRU eviction.

    Used for three different keys depending on the caller:
    ``barcode:<payload>``   - the scanner pipeline
    ``txn:<student_id>``    - transaction debounce in the cashier UI
    ``photo:<student_id>``  - upload rate limiting
    """

    def __init__(self, max_entries: int = 512) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def is_blocked(self, key: str, window_ms: int) -> bool:
        if window_ms <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            expires = self._entries.get(key)
            if expires is None:
                return False
            if expires <= now:
                del self._entries[key]
                return False
            return True

    def block(self, key: str, window_ms: int) -> None:
        if window_ms <= 0:
            return
        with self._lock:
            self._entries[key] = time.monotonic() + window_ms / 1000.0
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def remaining_ms(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            expires = self._entries.get(key)
            if expires is None or expires <= now:
                self._entries.pop(key, None)
                return 0
            return int((expires - now) * 1000)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def active_count(self) -> int:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, exp in self._entries.items() if exp <= now]:
                del self._entries[key]
            return len(self._entries)


class ScanService:
    def __init__(
        self,
        repo: Repository | None = None,
        verification: VerificationService | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.repo = repo or Repository()
        self.config = config or get_config()
        self.verification = verification or VerificationService(config=self.config)
        self.cooldowns = CooldownTracker(self.config.scanner.max_tracked_barcodes)
        self._last_scan: dict[str, Any] | None = None

    # -- public API --------------------------------------------------------
    def process_scan(self, request: dict[str, Any], *, operator_id: int | None = None) -> ScanVerification:
        """Verify a decoded payload, honour the cooldown, and record the result."""
        payload_raw = str(request.get("barcode") or "")
        started = time.perf_counter()

        # Reject garbage before it can reach the cooldown map.
        try:
            payload = validate_barcode_payload(payload_raw, self.config.scanner.payload_pattern)
        except Exception as exc:  # noqa: BLE001
            logger.info("scan rejected: %s", exc)
            return self._record_failure(
                payload=payload_raw or "(empty)",
                result="INVALID_BARCODE",
                error_code="INVALID_BARCODE",
                message=str(exc),
                request=request,
                operator_id=operator_id,
                record=bool(request.get("record", True)),
            )

        cooldown_key = f"barcode:{payload}"
        if self.cooldowns.is_blocked(cooldown_key, self.config.scanner.cooldown_ms):
            remaining = self.cooldowns.remaining_ms(cooldown_key)
            logger.debug("duplicate suppressed for %s (%d ms remaining)", payload, remaining)
            if (
                self.config.scanner.duplicate_log_per_cooldown
                and not self.cooldowns.is_blocked(f"log:{cooldown_key}", 1000)
            ):
                self.cooldowns.block(f"log:{cooldown_key}", 1000)
                self._write_scan(
                    barcode_value=payload,
                    result="DUPLICATE_SCAN",
                    request=request,
                    operator_id=operator_id,
                    error_code="DUPLICATE_SCAN",
                )
            return ScanVerification(
                success=False,
                result="DUPLICATE_SCAN",
                message=f"Duplicate scan ignored - wait {remaining} ms before scanning again.",
                barcode=payload,
                barcode_type=request.get("barcode_type"),
                confidence=request.get("confidence"),
                duplicate=True,
                error_code="DUPLICATE_SCAN",
                timing=self._timing(request, started, 0.0),
                scan_time=utc_now_iso(),
            )

        outcome = self.verification.verify(
            payload,
            barcode_type=request.get("barcode_type"),
            confidence=request.get("confidence"),
        )
        self.cooldowns.block(cooldown_key, self.config.scanner.cooldown_ms)

        scan_id: int | None = None
        if request.get("record", True):
            scan_id = self._write_scan(
                barcode_value=payload,
                result=outcome.result,
                request=request,
                operator_id=operator_id,
                student_pk=outcome.student_pk,
                error_code=outcome.error_code,
                error_message=outcome.error_code if not outcome.success else None,
            )
        else:
            logger.info("scan not recorded (record=false) for %s", payload)

        total_ms = (time.perf_counter() - started) * 1000.0
        response = ScanVerification(
            success=outcome.success,
            result=outcome.result,  # type: ignore[arg-type]
            message=outcome.message,
            scan_id=scan_id,
            barcode=payload,
            barcode_type=outcome.barcode_type,
            confidence=outcome.confidence,
            student=(
                StudentSummaryOut.from_model(outcome.student) if outcome.student else None
            ),
            can_transact=outcome.can_transact,
            duplicate=False,
            error_code=outcome.error_code,
            timing=self._timing(request, started, outcome.database_time_ms, total_ms),
            scan_time=outcome.verified_at,
        )
        self._last_scan = response.model_dump()
        logger.info(
            "scan %s -> %s (scan_id=%s, total=%.2f ms)",
            payload, outcome.result, scan_id, total_ms,
        )
        return response

    def last_scan(self) -> dict[str, Any] | None:
        return dict(self._last_scan) if self._last_scan else None

    def clear_cooldowns(self) -> int:
        count = self.cooldowns.active_count()
        self.cooldowns.clear()
        return count

    # -- history -----------------------------------------------------------
    def history(self, **kwargs: Any) -> tuple[list[Any], int]:
        return self.repo.scans.list(**kwargs)

    def get(self, scan_pk: int):
        from app.utils.errors import NotFoundError

        scan = self.repo.scans.get(scan_pk)
        if scan is None:
            raise NotFoundError(f"scan {scan_pk} not found")
        return scan

    def recent(self, limit: int = 10) -> list[Any]:
        return self.repo.scans.recent(limit)

    def summary(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        return self.repo.scans.summary(start, end)

    # -- internals ---------------------------------------------------------
    def _timing(
        self,
        request: dict[str, Any],
        started: float,
        database_ms: float = 0.0,
        total_ms: float | None = None,
    ) -> ScanTiming:
        detection = request.get("detection_time_ms")
        processing = request.get("processing_time_ms")
        return ScanTiming(
            detection_ms=detection,
            decode_ms=detection,
            database_ms=round(database_ms, 3),
            processing_ms=processing,
            total_ms=round(total_ms if total_ms is not None
                           else (detection or 0.0) + database_ms, 3),
        )

    def _write_scan(
        self,
        *,
        barcode_value: str,
        result: str,
        request: dict[str, Any],
        operator_id: int | None,
        student_pk: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> int | None:
        try:
            return self.repo.scans.record(
                barcode_value=barcode_value,
                result=result,  # type: ignore[arg-type]
                student_pk=student_pk,
                confidence=request.get("confidence"),
                barcode_type=request.get("barcode_type"),
                device_name=request.get("device_name"),
                source=str(request.get("source") or "api"),
                processing_time_ms=request.get("processing_time_ms"),
                detection_time_ms=request.get("detection_time_ms"),
                frame_path=request.get("frame_path"),
                error_code=error_code,
                error_message=error_message,
                operator_id=operator_id,
                scan_time=request.get("timestamp"),
            )
        except Exception as exc:  # noqa: BLE001 - a logging failure must not lose the scan
            logger.error("could not record scan for %s: %s", barcode_value, exc)
            return None

    def _record_failure(
        self,
        *,
        payload: str,
        result: str,
        error_code: str,
        message: str,
        request: dict[str, Any],
        operator_id: int | None,
        record: bool = True,
    ) -> ScanVerification:
        scan_id = (
            self._write_scan(
                barcode_value=payload[:128],
                result=result,
                request=request,
                operator_id=operator_id,
                error_code=error_code,
                error_message=message[:200],
            )
            if record
            else None
        )
        response = ScanVerification(
            success=False,
            result=result,  # type: ignore[arg-type]
            message=message,
            scan_id=scan_id,
            barcode=payload[:128],
            barcode_type=request.get("barcode_type"),
            confidence=request.get("confidence"),
            error_code=error_code,
            timing=self._timing(request, time.perf_counter()),
            scan_time=utc_now_iso(),
        )
        self._last_scan = response.model_dump()
        return response


_scan_service: ScanService | None = None


def get_scan_service() -> ScanService:
    global _scan_service
    if _scan_service is None:
        _scan_service = ScanService()
    return _scan_service
